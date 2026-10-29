"""Bounded, resumable reads of received QQ forwards; signatures remain external data."""

import json
import math
import re

from .coordinator import WorkPaused
from .video_links import video_references

FORWARD_VERSION = 'images-batched-v2'
FORWARD_IMAGE_LIMIT = 128


def _handle(value):
    return str(value) if type(value) in (str, int) and re.fullmatch(r'[!-~]{1,512}', str(value)) else None


def image_segments(message):
    yield from enumerate(message.segments)
    for node in (message.forwarded or {}).get('nodes', []):
        for position, segment in enumerate(node['images']):
            yield 'forward:' + '.'.join(map(str,node['path'])) + f':{position}', segment


def _nodes(response):
    rows = response.get('messages', response.get('message')) if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise TypeError('invalid forward response')
    budget, limited = {'nodes':100, 'chars':16000, 'images':FORWARD_IMAGE_LIMIT}, False

    def record(raw, depth=0):
        nonlocal limited
        if not isinstance(raw, dict) or depth > 3 or budget['nodes'] <= 0:
            limited = True
            return None
        budget['nodes'] -= 1
        data = raw.get('data', {}) if raw.get('type') == 'node' else raw
        if not isinstance(data, dict):
            limited = True
            return None
        sender = data.get('sender') if isinstance(data.get('sender'), dict) else {}
        stamp = data.get('time')
        try:
            stamp = float(stamp) if type(stamp) in (str, int, float) else None
            if stamp is not None and not math.isfinite(stamp):
                stamp = None
        except ValueError:
            stamp = None
        parts = data.get('content', data.get('message', []))
        texts, images, children, urls = [], [], [], []
        if isinstance(parts, str):
            texts.append(parts)
            limited |= '[CQ:' in parts
            parts = []
        if not isinstance(parts, list):
            limited = True
            parts = []
        limited |= len(parts) > 256
        for part in parts[:256]:
            if not isinstance(part, dict) or not isinstance(part.get('data'), dict):
                limited = True
                continue
            kind, item = part.get('type'), part['data']
            if kind == 'text':
                text = str(item.get('text', ''))
                limited |= len(text) > 4000
                texts.append(text[:4000])
            elif kind == 'image':
                if budget['images'] and any(item.get(key) for key in ('file','url')):
                    images.append({'type':'image','data':{key:str(item[key])[:2048]
                                  for key in ('file','url') if key in item}})
                    budget['images'] -= 1
                else:
                    limited = True
            elif kind in {'json', 'share'}:
                body = item.get('data', '') if kind == 'json' else item
                body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                urls.extend(video_references(body))
                limited |= len(body) > 1000
                texts.append('[分享卡片] ' + body[:1000])
            elif kind == 'forward':
                identifier = _handle(item.get('id'))
                if identifier:
                    children.append({'forward_id':identifier})
                else:
                    limited = True
            elif kind == 'node':
                child = record(part, depth+1)
                if child:
                    children.append({'inline':child})
            elif kind in {'at','face','reply'}:
                texts.append(f'[{kind}: {str(item.get("qq",item.get("id","")))[:80]}]')
            else:
                texts.append(f'[未读取的附件：{str(kind)[:40]}]')
                limited = True
        text = '\n'.join(texts)
        length = min(4000, budget['chars'])
        limited |= len(text) > length
        text = text[:length]
        budget['chars'] -= len(text)
        return {'author_id':str(sender.get('user_id', data.get('uin', data.get('user_id',''))))[:80],
                'author_name':str(sender.get('card') or sender.get('nickname') or data.get('name') or '未署名')[:100],
                'time':stamp,'text':text,'images':images,'children':children,
                'source_video_urls':list(dict.fromkeys([*video_references(text), *urls]))[:20]}

    nodes = []
    for raw in rows[:100]:
        item = record(raw)
        if item:
            nodes.append(item)
    return {'nodes':nodes,'limited':limited or len(rows)>100}


async def read_forward(message, fetch, *, cache=None, checkpoint=None, allowed=None):
    cache = cache if cache is not None else {}
    if cache.get('source_id', message.id) != message.id:
        raise ValueError('forward checkpoint belongs to another source')
    if cache.get('version') != FORWARD_VERSION:
        cache.clear()
        cache['version'] = FORWARD_VERSION
    cache['source_id'] = message.id
    sources = cache.setdefault('sources', {})
    if not isinstance(sources, dict):
        raise TypeError('invalid forward checkpoint')
    result = {'version':FORWARD_VERSION,'source_id':message.id,'trust':'external_data','identity_verified':False,
              'nodes':[],'complete':True,'limitations':[]}
    remaining = {'chars':16000, 'images':FORWARD_IMAGE_LIMIT, 'bytes':512000}

    def limited(reason):
        result['complete'] = False
        if reason not in result['limitations']:
            result['limitations'].append(reason)

    def check():
        if allowed is not None and not allowed():
            raise WorkPaused('转发读取等待允许运行')

    async def visit_node(item, depth, path, parent, ancestors):
        if len(result['nodes']) >= 100 or remaining['chars'] <= 0:
            limited('已达到单次材料预算，未按完整读取处理')
            return
        node = {key:value for key,value in item.items() if key != 'children'}
        node.update(path=list(path), parent_path=list(parent))
        if len(node['text']) > remaining['chars'] or len(node['images']) > remaining['images']:
            limited('已达到单次材料预算，未按完整读取处理')
        node['text'] = node['text'][:remaining['chars']]
        node['images'] = node['images'][:remaining['images']]
        size = len(json.dumps(node, ensure_ascii=False).encode())
        if size > remaining['bytes']:
            limited('已达到单次材料预算，未按完整读取处理')
            return
        remaining['bytes'] -= size
        remaining['chars'] -= len(node['text'])
        remaining['images'] -= len(node['images'])
        result['nodes'].append(node)
        for index, child in enumerate(item['children']):
            if depth >= 3:
                limited('已达到嵌套层级上限')
                break
            if 'inline' in child:
                await visit_node(child['inline'],depth+1,(*path,index),path,ancestors)
            else:
                await visit_forward(child['forward_id'],depth+1,(*path,index),path,ancestors)

    async def visit_forward(identifier, depth, path, parent, ancestors):
        check()
        if identifier in ancestors:
            limited('转发引用存在循环，循环部分未读取')
            return
        if identifier not in sources:
            if len(sources) >= 8:
                limited('已达到单次转发读取数量上限')
                return
            sources[identifier] = _nodes(await fetch(identifier))
            if checkpoint:
                checkpoint()
        check()
        material = sources[identifier]
        if material['limited']:
            limited('来源记录超出预算或包含尚未读取的附件')
        for index,item in enumerate(material['nodes']):
            await visit_node(item,depth,(*path,index),parent,(*ancestors,identifier))

    check()
    for index,part in enumerate(message.segments):
        if isinstance(part,dict) and part.get('type') == 'forward':
            data = part.get('data')
            identifier = _handle(data.get('id')) if isinstance(data, dict) else None
            if not identifier:
                raise ValueError('invalid received forward identifier')
            await visit_forward(identifier,0,(index,),(),())
    if not result['nodes']:
        limited('未取得可展开的转发节点')
    return result
