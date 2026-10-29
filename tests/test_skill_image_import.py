import base64
import hashlib
import io
import json

import pytest
from PIL import Image
from test_skill_plugins import package, setup

from buwubot.skill_plugins import SkillPlugins
from buwubot.stickers import MediaStore


def sample():
    stream = io.BytesIO()
    Image.new('RGB', (16, 16), 'red').save(stream, 'JPEG')
    raw = stream.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    return raw, {'hash': digest, 'base64': base64.b64encode(raw).decode()}


async def invoke(tmp_path, result):
    store, coordinator, tools, _ = setup(tmp_path)
    media = MediaStore(store, tmp_path / 'media')
    manager = SkillPlugins(store, tools, tmp_path / 'image-skills', media=media)
    script = 'import json\nprint(' + repr(json.dumps(result)) + ')\n'
    item = await coordinator.commit(lambda: manager.install(package(script)))
    await coordinator.commit(lambda: manager.set_enabled('sample', True, expected_revision=item['revision']))
    return store, media, tools


async def test_subprocess_images_registered_and_encoded_bytes_not_exposed(tmp_path):
    raw, asset = sample()
    result = {'state': 'succeeded', 'data': {'coverage': 'sampled_frames',
        'frames': [{'asset_hash': asset['hash'], 'timestamp': 10}]}, 'assets': [asset]}
    store, media, tools = await invoke(tmp_path, result)
    actual = await tools.invoke('sample_read', {})
    assert actual['images'] == []
    assert actual['evidence_images'] == [asset['hash']]
    assert actual['data'] == result['data']
    assert 'base64' not in json.dumps(actual) and 'assets' not in actual
    assert media.path(asset['hash']).read_bytes() == raw
    store.close()


@pytest.mark.parametrize('change', ['hash', 'bytes', 'encoding', 'unfinished', 'shape', 'many', 'data', 'continuation'])
async def test_invalid_image_result_rejected_before_registration(tmp_path, change):
    _, asset = sample()
    result = {'state': 'succeeded', 'data': {}, 'assets': [asset]}
    if change == 'hash':
        asset['hash'] = '0' * 64
    elif change == 'bytes':
        asset['base64'] = base64.b64encode(b'not an image').decode()
        asset['hash'] = hashlib.sha256(b'not an image').hexdigest()
    elif change == 'encoding':
        asset['base64'] = 'not base64!'
    elif change == 'unfinished':
        result.update(state='pending', operation_id='saved')
    elif change == 'shape':
        asset['path'] = 'file:///private'
    elif change == 'many':
        result['assets'] = [asset] * 11
    elif change == 'data':
        result['data'] = {'text': '\0' * 12000}
    elif change == 'continuation':
        result['next_arguments'] = {'page': 2}
    store, _, tools = await invoke(tmp_path, result)
    with pytest.raises(ValueError):
        await tools.invoke('sample_read', {})
    assert not store.rows('SELECT hash FROM media')
    store.close()
