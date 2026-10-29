"""Validate complete model-selected chat batches before the first native send."""


def reply_batches(data, stickers, media, *, allow_empty=False):
    if 'messages' in data:
        if data.get('text') or data.get('sticker_id'):
            raise ValueError('ambiguous legacy and multipart reply')
        items = data['messages']
        if not isinstance(items,list) or len(items) > 3 or (not items and not allow_empty):
            raise ValueError('reply must contain one to three messages')
    else:
        items = [data]
    known = {item['id'] for item in stickers}
    batches, length = [], 0
    for item in items:
        if not isinstance(item,dict):
            raise TypeError('reply message must be an object')
        text = item.get('text','')
        if not isinstance(text,str):
            raise TypeError('reply text must be a string')
        text = text.strip()
        length += len(text)
        if length > 6000:
            raise ValueError('reply text budget exceeded')
        parts = [{'type':'text','data':{'text':text}}] if text else []
        sticker = item.get('sticker_id')
        if sticker:
            if not isinstance(sticker,str) or sticker not in known:
                raise ValueError('model selected an unknown sticker')
            parts.append({'type':'image','data':{'file':media.path(sticker).as_uri()}})
        if not parts:
            raise ValueError('empty reply is not a completed task')
        batches.append(parts)
    return batches
