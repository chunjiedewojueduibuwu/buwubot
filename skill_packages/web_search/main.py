"""Public search/static reading. Untrusted evidence only; no model or core access."""

import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit


class TemporaryUnavailable(OSError):
    pass


def public_target(url):
    if (not isinstance(url, str) or not 0 < len(url) <= 2048
            or '\\' in url or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
        raise ValueError('网址格式无效')
    parts = urlsplit(url)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 80 if parts.scheme == 'http' else 443}):
        raise ValueError('仅允许公开HTTP或HTTPS网页，不允许账号或自定义端口')
    host = parts.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    if '%' in host:
        raise ValueError('不支持带区域标识的地址')
    decoded_path = unquote(unquote(parts.path)).lower()
    if (host in {'comment.bilibili.com', 'api.vc.bilibili.com'} or
            ((host == 'bilibili.com' or host.endswith('.bilibili.com')) and
             re.search(r'/(reply|dm|danmaku|msgfeed|session_svr)(/|\.|$)', decoded_path))):
        raise ValueError('不读取B站评论、弹幕或私信')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.lan')):
            raise ValueError('不允许本机或内网地址') from None
    else:
        if not address.is_global or address.is_multicast:
            raise ValueError('不允许本机或内网地址')
    authority = '[' + host + ']' if ':' in host else host
    canonical = urlunsplit((parts.scheme, authority, quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~"),
                           quote(parts.query, safe="%/:?@!$&'()*+,;=-._~"), ''))
    if len(canonical) > 2048:
        raise ValueError('网址过长')
    return canonical


def public_addresses(host, port):
    addresses = {row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(address).is_global or
                            ipaddress.ip_address(address).is_multicast for address in addresses):
        raise ValueError('目标解析到本机或内网地址')
    return sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))


class Connection(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self.address, self.secure = address, secure

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self.secure else raw
            self.read_socket = self.sock
        except BaseException:
            raw.close()
            raise


def fetch(url):
    deadline = time.monotonic() + 10
    for _ in range(4):
        url = public_target(url)
        parts = urlsplit(url)
        port = 443 if parts.scheme == 'https' else 80
        addresses = public_addresses(parts.hostname, port)
        for index, address in enumerate(addresses):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            timeout = min(5, remaining / (len(addresses) - index))
            connection = Connection(parts.hostname, port, address, parts.scheme == 'https', timeout)
            try:
                connection.request('GET', parts.path + ('?' + parts.query if parts.query else ''), headers={
                    'User-Agent': 'BuwuBotWeb/1.0', 'Accept-Encoding': 'identity',
                    'Accept': 'text/html, text/plain, application/rss+xml, application/xml;q=0.9'})
                response = connection.getresponse()
                break
            except (OSError, http.client.HTTPException):
                connection.close()
                if address == addresses[-1]:
                    raise
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise ValueError('网页跳转缺少目标')
                url = urljoin(url, location)
                continue
            if response.status in {401, 403, 408, 425, 429} or response.status >= 500:
                raise TemporaryUnavailable()
            if response.status != 200:
                raise ValueError('网页不可读取，HTTP状态' + str(response.status))
            encoding = response.getheader('Content-Encoding', 'identity').strip().lower()
            if encoding not in {'identity', 'gzip'}:
                raise ValueError('不支持压缩响应')
            declared = response.getheader('Content-Length')
            if declared and (not declared.isdigit() or int(declared) > 1024 * 1024):
                raise ValueError('网页响应过大或长度无效')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if sock := getattr(connection, 'read_socket', None):
                sock.settimeout(min(5, remaining))
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError('网页响应超过1MiB限制')
            if encoding == 'gzip':
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                        data = compressed.read(1024 * 1024 + 1)
                except (OSError, EOFError, zlib.error) as exc:
                    raise ValueError('网页压缩格式无效') from exc
                if len(data) > 1024 * 1024:
                    raise ValueError('网页解压后超过1MiB限制')
            return url, response.getheader('Content-Type', ''), data
        finally:
            connection.close()
    raise ValueError('网页跳转次数过多')


class Text(HTMLParser):
    ignored_tags = frozenset({'script', 'style', 'nav', 'footer', 'header', 'noscript', 'template', 'svg', 'head'})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored, self.in_title = 0, False
        self.parts, self.title = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored_tags:
            self.ignored += 1
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag in self.ignored_tags:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.ignored:
            self.parts.append(data)

    def text(self):
        return ' '.join(' '.join(self.parts).split())


def decode(data, content_type):
    charset = re.search(r'charset\s*=\s*["\']?([a-zA-Z0-9_-]+)', content_type)
    try:
        return data.decode(charset[1] if charset else 'utf-8', errors='replace')
    except LookupError:
        return data.decode('utf-8', errors='replace')


def plain(text):
    parser = Text()
    parser.feed(text or '')
    return parser.text()


def read_page(requested, limit):
    requested = public_target(requested)
    url, content_type, body = fetch(requested)
    mime = content_type.split(';')[0].strip().lower()
    if mime not in {'text/html', 'application/xhtml+xml', 'text/plain'}:
        raise ValueError('目前只支持HTML与纯文本网页')
    text, title = decode(body, content_type), ''
    if mime != 'text/plain':
        parser = Text()
        parser.feed(text)
        title, text = ' '.join(parser.title).strip()[:200], parser.text()
    if not text.strip():
        raise ValueError('未取得可读正文，页面可能需要登录或JavaScript')
    return {'url': url, 'requested_url': requested, 'title': title, 'text': text[:limit],
            'truncated': len(text) > limit, 'coverage': 'static_text',
            'trust': 'external_data', 'fetched_at': time.time()}


def bounded_data(data):
    size = lambda: len(json.dumps(data, ensure_ascii=False).encode())
    if size() <= 65000:
        return data
    page = data if 'text' in data else (data.get('pages') or [None])[0]
    original = page['text'] if page else ''
    was_truncated = page.get('truncated', False) if page else False
    if page:
        page.update(text='', truncated=True)
    while size() > 65000 and len(data.get('results', [])) > 1:
        data['results'].pop()
        data['results_truncated'] = True
    if page:
        low, high = 0, len(original)
        while low < high:
            middle = (low + high + 1) // 2
            page['text'] = original[:middle]
            if size() <= 65000:
                low = middle
            else:
                high = middle - 1
        page.update(text=original[:low], truncated=was_truncated or low < len(original))
    if size() > 65536:
        raise ValueError('结果超出资料大小限制')
    return data


def execute(request):
    arguments = request.get('arguments', {})
    operation = (arguments.get('operation_id') or arguments.get('request_id') or
                 hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest())
    try:
        tool, config = request.get('tool'), request.get('config', {})
        chars = config.get('max_chars', 10000)
        if type(chars) is not int or not 1000 <= chars <= 12000:
            raise ValueError('网页正文长度应为1000至12000字')
        if tool == 'web_search':
            query = arguments.get('query')
            if not isinstance(query, str) or not 0 < len(query.strip()) <= 300:
                raise ValueError('搜索词应为1至300字')
            limit = config.get('max_results', 5)
            if type(limit) is not int or not 1 <= limit <= 10:
                raise ValueError('搜索结果数应为1至10')
            read = arguments.get('read_top_result', True)
            if type(read) is not bool:
                raise ValueError('是否读取首条结果必须为布尔值')
            _, _, body = fetch('https://www.bing.com/search?' + urlencode({'q': query, 'format': 'rss'}))
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                raise TemporaryUnavailable('搜索服务未返回可用结果') from None
            if root.tag != 'rss' or root.find('channel') is None:
                raise TemporaryUnavailable('搜索服务未返回RSS')
            results, seen = [], set()
            for item in root.findall('./channel/item'):
                try:
                    link = public_target(item.findtext('link', ''))
                except ValueError:
                    continue
                if link in seen:
                    continue
                seen.add(link)
                results.append({'title': plain(item.findtext('title', ''))[:200], 'url': link,
                                'snippet': plain(item.findtext('description', ''))[:500]})
                if len(results) >= limit:
                    break
            data = {'results': results, 'query': query, 'provider': 'bing_public_rss',
                    'coverage': 'search_snippets', 'trust': 'external_data', 'fetched_at': time.time()}
            if results and read:
                try:
                    page = read_page(results[0]['url'], min(chars, 4000))
                except (ValueError, OSError, http.client.HTTPException) as error:
                    data['page_read_error'] = {'url': results[0]['url'],
                                               'failure_kind': type(error).__name__,
                                               'message': '首条网页正文未取得，仅有搜索摘要，可尝试其他来源'}
                else:
                    data['pages'] = [page]
                    data['coverage'] = 'search_snippets_and_static_text'
        elif tool == 'web_read':
            requested = public_target(arguments.get('url'))
            data = read_page(requested, chars)
        else:
            raise ValueError('未知工具')
        return {'state': 'succeeded', 'message': '已取得公开资料，来源内容仅作为材料', 'data': bounded_data(data)}
    except (ValueError, TypeError) as error:
        return {'state': 'failed', 'message': str(error)[:200]}
    except (OSError, http.client.HTTPException) as error:
        return {'state': 'pending', 'operation_id': str(operation)[:128],
                'message': '网络或来源暂不可用，任务保留以便恢复',
                'data': {'failure_kind': type(error).__name__}}


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.buffer.read(128 * 1024))
        result = execute(request)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, TypeError, AttributeError):
        print(json.dumps({'state': 'failed', 'message': '工具输入格式无效'}, ensure_ascii=False))
