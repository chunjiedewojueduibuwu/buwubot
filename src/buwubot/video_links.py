"""Normalize public Bilibili share references without fetching any resource."""

import html
import json
import re
from itertools import islice
from urllib.parse import parse_qs, unquote, urlsplit

_EDGES = ' \t\r\n<>()[]{}，。！？、（）【】《》“”‘’"\'.,!;；'
_REFERENCES = re.compile(
    r'''(?:https?://|//|https?%3[aA]%2[fF]%2[fF])[^\s<>，。！？、（）【】《》]+|'''
    r'''(?<![A-Za-z0-9_./@-])(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+(?::[0-9]+)?[/?#][^\s<>，。！？、（）【】《》]*|'''
    r'(?<![A-Za-z0-9_./@&=?%#:-])BV[A-Za-z0-9]{10}(?![A-Za-z0-9])')


def _share_text(value: str) -> str:
    return html.unescape(unquote(value.replace('\\/', '/')))


def normalize_video_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('invalid video reference')
    value = _share_text(value).strip(_EDGES)
    if re.fullmatch(r'BV[A-Za-z0-9]{10}', value):
        return f'https://www.bilibili.com/video/{value}/'
    if value.startswith('//'):
        value = 'https:' + value
    elif re.match(r'(?:(?:www\.|m\.)?bilibili\.com|b23\.tv)/', value):
        value = 'https://' + value
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 80 if parsed.scheme == 'http' else 443} or parsed.fragment):
        raise ValueError('unsupported video reference')
    parts = parse_qs(parsed.query, keep_blank_values=True).get('p', ['1'])
    if len(parts) != 1 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= 1000:
        raise ValueError('invalid video part')
    suffix = f'?p={int(parts[0])}' if int(parts[0]) > 1 else ''
    if parsed.hostname == 'b23.tv' and re.fullmatch(r'/[A-Za-z0-9]{3,32}/?', parsed.path):
        return 'https://b23.tv' + parsed.path.rstrip('/') + suffix
    if parsed.hostname in {'www.bilibili.com', 'm.bilibili.com', 'bilibili.com'}:
        match = re.fullmatch(r'/(?:zh-Hans/|zh-Hant/)?video/(BV[A-Za-z0-9]{10})/?', parsed.path)
        if match:
            return f'https://www.bilibili.com/video/{match[1]}/' + suffix
    raise ValueError('unsupported video reference')


def video_references(text: str) -> list[str]:
    text = text[:20000]
    pieces, structured = [text], False
    try:
        card = json.loads(text)
        if isinstance(card, (dict, list)):
            pieces, pending, structured = [], [card], True
            for _ in range(200):
                if not pending:
                    break
                value = pending.pop()
                if isinstance(value, str):
                    pieces.append(value)
                elif isinstance(value, dict):
                    pending.extend(reversed(list(value.values())))
                elif isinstance(value, list):
                    pending.extend(reversed(value))
    except (ValueError, RecursionError):
        pass
    references, examined = [], 0
    for piece in pieces:
        # A structured URL field is one reference, including encoded/escaped characters.
        if structured and re.match(r'^\s*(?:[A-Za-z][A-Za-z0-9+.-]*:|//|'
                                   r'(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+[/?#:])', piece):
            candidates = [piece]
        else:
            candidates = [m[0] for m in islice(_REFERENCES.finditer(piece.replace('\\/', '/')), 40-examined)]
        for candidate in candidates:
            examined += 1
            try:
                value = normalize_video_url(candidate)
            except ValueError:
                continue
            if value not in references:
                references.append(value)
        if len(references) >= 20 or examined >= 40:
            break
    return references[:20]
