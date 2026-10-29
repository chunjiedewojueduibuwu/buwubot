import json

import pytest

from buwubot.coordinator import WorkPaused
from buwubot.learning import LearningSegment, PluginLearningSource, SourceUnavailable
from buwubot.tools import ToolRegistry

URL = 'https://www.bilibili.com/video/BV1234567890/'


async def test_plugin_learning_requires_enabled_provider_without_fallback_network():
    source = PluginLearningSource(ToolRegistry(), media=None)
    with pytest.raises(WorkPaused):
        await source.read_segment(URL, 1, 0, 60)
    with pytest.raises(WorkPaused):
        await source.discover()


async def test_learning_uses_registered_video_result_and_stable_read_identifier():
    tools = ToolRegistry()
    calls = []
    async def read(arguments):
        calls.append(arguments)
        if len(calls) == 1:
            return {'state': 'pending', 'operation_id': 'checkpoint'}
        return {'state': 'succeeded', 'data': {'segment': LearningSegment(url=URL,
            video_id='BV1234567890', title='actual', uploader='author', part=1,
            start_seconds=0, end_seconds=60, duration=120,
            captions=[{'start': 0, 'end': 5, 'text': 'actual subtitle'}]).model_dump()}}
    tools.register('video_read', read, description='read', input_schema={'type': 'object'})
    source = PluginLearningSource(tools, media=None)
    with pytest.raises(OSError):
        await source.read_segment(URL, 1, 0, 60)
    actual = await source.read_segment(URL, 1, 0, 60)
    assert actual.captions[0]['text'] == 'actual subtitle'
    assert calls[0]['request_id'] == calls[1]['request_id']
    assert calls[1]['operation_id'] == 'checkpoint'
    tools.disable('video_read')
    with pytest.raises(WorkPaused):
        await source.read_segment(URL, 1, 0, 60)
    await source.close()


@pytest.mark.parametrize('change', ['url', 'part', 'start', 'end', 'coverage', 'asset'])
async def test_mismatched_video_material_cannot_enter_learning(change):
    tools = ToolRegistry()
    segment = {'url': URL, 'video_id': 'BV1234567890', 'title': 'actual', 'uploader': '',
        'part': 1, 'start_seconds': 0, 'end_seconds': 60, 'duration': 120, 'captions': []}
    if change == 'url':
        segment['url'] = 'https://www.bilibili.com/video/BV9999999999/'
    elif change == 'part':
        segment['part'] = 2
    elif change == 'start':
        segment['start_seconds'] = 30
    elif change == 'end':
        segment['end_seconds'] = 100
    elif change == 'coverage':
        segment['coverage'] = 'sampled_frames'
    elif change == 'asset':
        segment.update(coverage='sampled_frames', frames=[{'timestamp': 10, 'asset_hash': '0' * 64}])
    async def read(_):
        return {'state': 'succeeded', 'data': {'segment': segment}}
    tools.register('video_read', read, description='read', input_schema={'type': 'object'})
    with pytest.raises((SourceUnavailable, ValueError)):
        await PluginLearningSource(tools, media=None).read_segment(URL, 1, 0, 60)


def test_video_package_contract_exists_and_does_not_offer_comments():
    from pathlib import Path
    root = Path(__file__).parents[1] / 'skill_packages' / 'bilibili_video'
    manifest = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))
    assert {tool['name'] for tool in manifest['tools']} == {'video_read', 'video_discover', 'video_summarize'}
    assert next(tool for tool in manifest['tools'] if tool['name'] == 'video_summarize')['continuation']
    assert (root / manifest['entrypoint']).is_file()
