"""Comment-free public video evidence, reusing the host's tested reader library."""

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from PIL import Image

from buwubot.learning import LearningSource, SourceUnavailable
from buwubot.video_links import normalize_video_url
from buwubot.video_media import TimelineSource

CACHE_BYTES = 64 * 1024 * 1024


async def resolve_video_url(url):
    """Resolve only official short links; validate before following each hop."""
    for _ in range(3):
        try:
            url = normalize_video_url(url)
        except ValueError as exc:
            raise SourceUnavailable('需要有效的公开B站视频地址') from exc
        parsed = urlsplit(url)
        if parsed.fragment:
            raise SourceUnavailable('视频链接不能指向评论或页面片段')
        if parsed.hostname != 'b23.tv':
            if (parsed.hostname in {'www.bilibili.com', 'bilibili.com'}
                    and re.fullmatch(r'/(zh-Hans|zh-Hant)/video/BV[A-Za-z0-9]{10}/?', parsed.path)):
                url = parsed._replace(path='/' + parsed.path.split('/', 2)[-1]).geturl()
            LearningSource.identity(url)
            return url
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not re.fullmatch(r'/[A-Za-z0-9]{3,32}/?', parsed.path)):
            raise SourceUnavailable('需要有效的B站视频短链接')
        async with (httpx.AsyncClient(timeout=6, trust_env=False, follow_redirects=False) as client,
                    client.stream('GET', url) as response):
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            if response.status_code not in (301, 302, 303, 307, 308):
                raise SourceUnavailable('B站短链接未指向可读取的视频页')
            target = response.headers.get('location', '')
            if not target or len(target) > 2048:
                raise SourceUnavailable('B站短链接跳转地址无效')
            url = urljoin(url, target)
    raise SourceUnavailable('B站短链接跳转次数过多')


def cleanup(root, current):
    root, current = Path(root).resolve(), Path(current).resolve()
    rows = []
    for folder in root.iterdir():
        if (not re.fullmatch('[a-f0-9]{64}', folder.name) or folder.is_symlink()
                or not folder.is_dir() or folder.resolve().parent != root):
            continue
        size = sum(path.stat().st_size for path in folder.iterdir() if path.is_file() and not path.is_symlink())
        rows.append((folder, folder.stat().st_mtime, size))
    total = sum(row[2] for row in rows)
    count = len(rows)
    for folder, modified, size in sorted(rows, key=lambda row: row[1]):
        if folder == current:
            continue
        if time.time() - modified > 7 * 86400 or count > 64 or total > CACHE_BYTES:
            shutil.rmtree(folder)
            total -= size
            count -= 1


def clear_intermediates(folder):
    for path in folder.iterdir():
        if re.fullmatch(r'[a-f0-9]{64}\.(response|frame)|\.[a-f0-9]{32}\.tmp|'
                        r'[a-f0-9]{16}(\.(decoded|wav|speech|tmp)|-[0-9]{3}\.jpg)', path.name):
            path.unlink(missing_ok=True)


def atomic(path, content):
    temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class FrameBuffer:
    def __init__(self):
        self.content = {}

    def save(self, content):
        if len(content) > 2_000_000:
            raise SourceUnavailable('抽样画面过大')
        with Image.open(BytesIO(content)) as original:
            if original.width * original.height > 40_000_000:
                raise SourceUnavailable('抽样画面尺寸过大')
            image = original.convert('RGB')
        for size in (512, 384, 256, 160):
            image.thumbnail((size, size))
            buffer = BytesIO()
            image.save(buffer, 'JPEG', quality=55, optimize=True)
            content = buffer.getvalue()
            if len(content) <= 14000:
                digest = hashlib.sha256(content).hexdigest()
                self.content[digest] = content
                return {'hash': digest}
        raise SourceUnavailable('抽样画面无法在单次资料范围内传递')


class SheetBuffer(FrameBuffer):
    def save(self, content):
        if not 0 < len(content) <= 32000:
            raise SourceUnavailable('时序画面超过单次资料上限')
        digest = hashlib.sha256(content).hexdigest()
        self.content[digest] = content
        return {'hash':digest}


class CachedSource(TimelineSource):
    def __init__(self, folder, *, media, config=None):
        config = config or {}
        super().__init__(folder,media=media,asr_python=config.get('asr_python',''),
            asr_model_dir=config.get('asr_model_dir',''),worker=Path(__file__).with_name('asr_worker.py'),
            audio=config.get('allow_audio',True))

    async def _read(self, url, *, limit):
        path = self.folder / (hashlib.sha256(url.encode()).hexdigest() + '.response')
        # Playback URLs expire; always request a fresh address after interruption.
        cacheable = '/x/player/playurl?' not in url
        if cacheable and path.is_file() and time.time() - path.stat().st_mtime < 600:
            data = path.read_bytes()
            if len(data) <= limit:
                return data
        data = await super()._read(url, limit=limit)
        if cacheable:
            atomic(path, data)
        return data

    async def _extract_frame(self, url, timestamp, referer):
        key = hashlib.sha256(f'{referer}:{timestamp:.6f}'.encode()).hexdigest()
        path = self.folder / (key + '.frame')
        if path.is_file():
            data = path.read_bytes()
            if 0 < len(data) <= 2_000_000:
                return data
        data = await super()._extract_frame(url, timestamp, referer)
        atomic(path, data)
        return data


async def execute(request, *, root=Path('.')):
    operation = ''
    source = None
    try:
        arguments = dict(request.get('arguments', {}))
        request_id = arguments.pop('request_id', '')
        previous = arguments.pop('operation_id', '')
        tool = request['tool']
        frames = request.get('config', {}).get('allow_frames', True)
        config = request.get('config', {})
        audio = config.get('allow_audio',True)
        if type(frames) is not bool or type(audio) is not bool:
            raise ValueError('invalid frame setting')
        for setting in ('asr_python','asr_model_dir'):
            if not isinstance(config.get(setting,''),str):
                raise TypeError('invalid audio setting')
        cache_config = [2,frames,audio,config.get('asr_python',''),config.get('asr_model_dir','')]
        if tool in {'video_read', 'video_summarize'}:
            if set(arguments) - {'url', 'part', 'start_seconds', 'span_seconds'}:
                raise ValueError('unknown video argument')
            url = normalize_video_url(arguments['url'])
            if not isinstance(url, str) or len(url) > 2048 or urlsplit(url).fragment:
                raise ValueError('invalid video URL')
            part = arguments.get('part', int(parse_qs(urlsplit(url).query).get('p', ['1'])[0]))
            start, span = arguments.get('start_seconds', 0), arguments.get('span_seconds', 60)
            if (type(part) is not int or not 1 <= part <= 1000
                    or type(start) not in (int, float) or type(span) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(span) or start < 0 or not 1 <= span <= 300):
                raise ValueError('invalid video range')
            if urlsplit(url).hostname == 'b23.tv':
                # Preserve the request identity even if short-link resolution times out.
                identity = json.dumps([tool, {'url':arguments['url'], 'part':arguments.get('part'),
                    'start_seconds':start, 'span_seconds':span}, request_id, cache_config],
                    sort_keys=True, allow_nan=False)
                operation = hashlib.sha256(identity.encode()).hexdigest()
            resolved = await resolve_video_url(url)
            video = LearningSource.identity(resolved)
            if 'part' not in arguments:
                part = int(parse_qs(urlsplit(resolved).query).get('p', ['1'])[0])
                if not 1 <= part <= 1000:
                    raise ValueError('invalid resolved video part')
            arguments = {'url': f'https://www.bilibili.com/video/{video}/', 'part': part,
                         'start_seconds': start, 'span_seconds': span}
        elif tool != 'video_discover' or set(arguments) - {'batch'}:
            raise ValueError('unsupported video tool')
        elif 'batch' in arguments and (type(arguments['batch']) is not int or arguments['batch'] < 0):
            raise ValueError('invalid discovery batch')
        identity = json.dumps([tool, arguments, request_id, cache_config], sort_keys=True, allow_nan=False)
        operation = operation or hashlib.sha256(identity.encode()).hexdigest()
        if previous and previous != operation:
            raise ValueError('operation does not match request')
        folder = Path(root) / operation
        folder.mkdir(parents=True, exist_ok=True)
        os.utime(folder, None)
        cleanup(root, folder)
        saved = folder / 'result.json'
        if saved.is_file():
            return json.loads(saved.read_text(encoding='utf-8'))
        media = SheetBuffer() if frames else None
        source = CachedSource(folder, media=media,config=config)
        async with asyncio.timeout(18):
            if tool == 'video_discover':
                data = {'candidates': await source.discover(), 'coverage': 'metadata_only'}
            else:
                segment = await (source.read_segment(arguments['url'], part, start, span) if frames
                    else LearningSource.read_segment(source,arguments['url'],part,start,span))
                data = {'segment': segment.model_dump(),
                    'limitations': '画面按visual_fps的时间分辨率顺序读取，非每个原始帧都送入模型；'
                    '语音转写可能出错，不代表理解全部音乐音效；未检出语音不等于静音。'
                    '字幕模式或关闭声音时未分析声音，不能推断未覆盖片段。'}
                data['source'] = {key: getattr(segment, key) for key in ('url','title','uploader','part',
                    'start_seconds','end_seconds','duration','coverage')}
                data['source']['audio_analyzed'] = segment.audio_status in ('transcribed','no_speech_detected')
                data['source']['audio_status'] = segment.audio_status
                data['source']['audio_language'] = segment.audio_language
                data['source']['audio_language_probability'] = segment.audio_language_probability
                data['source']['visual_fps'] = segment.visual_fps
                data['source']['sampled_frame_count'] = sum(len(f.frame_times) or 1 for f in segment.frames)
        data['trust'] = 'external_data'
        if len(json.dumps(data, ensure_ascii=False).encode()) > 60000:
            raise SourceUnavailable('片段文字过多，请缩短读取范围，未按完整阅读处理')
        result = {'state': 'succeeded', 'message': '已取得公开候选资料' if tool == 'video_discover'
                  else '已取得实际视频片段资料，覆盖范围见来源', 'data': data}
        if tool == 'video_summarize' and segment.end_seconds < segment.duration:
            result['next_arguments'] = arguments | {'start_seconds': segment.end_seconds}
        if media and media.content:
            result['assets'] = [{'hash': digest, 'base64': base64.b64encode(content).decode()}
                                for digest, content in media.content.items()]
        encoded = json.dumps(result, ensure_ascii=False).encode()
        if len(encoded) > 120000:
            raise SourceUnavailable('片段资料超过单次传递范围，请缩短读取范围')
        atomic(saved, encoded)
        clear_intermediates(folder)
        return result
    except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
        return {'state': 'failed', 'message': str(exc)[:300] if isinstance(exc, SourceUnavailable)
                else '视频地址、参数或资料格式无效；未取得可确认内容'}
    except (OSError, TimeoutError, httpx.HTTPError):
        return {'state': 'pending', 'operation_id': operation,
                'message': '公开视频资料暂不可读，已保留检查点等待恢复'}
    finally:
        if source:
            await source.close()


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError('request too large')
        result = asyncio.run(execute(json.loads(raw)))
    except (ValueError, KeyError, TypeError):
        result = {'state': 'failed', 'message': '视频请求格式无效'}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
