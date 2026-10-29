import json
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from test_engine import ModelsFixture, build

from buwubot.models import Completion


async def test_tool_evidence_is_understood_checkpointed_and_not_sent_as_reply_image(tmp_path):
    now = datetime(2026, 10, 24, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    buffer = BytesIO()
    Image.new('RGB', (8, 8), 'red').save(buffer, 'PNG')
    digest = engine.media.save(buffer.getvalue())['hash']
    calls = []

    async def analyze(asset, context, models, *, scope_key):
        calls.append(asset)
        assert scope_key == '123'
        return {'description': '画面是红色方块', 'visible_text': '', 'confidence': .9}

    engine.media.analyze = analyze

    async def tool(payload):
        return {'state': 'succeeded', 'evidence_images': [digest],
                'data': {'coverage': 'sampled_frames'}}

    engine.tools.register('video_read', tool, description='read', input_schema={'type': 'object'})

    class Models(ModelsFixture):
        failures = 1

        async def call(self, role, contents, **kwargs):
            if role != 'chat':
                return await super().call(role, contents, **kwargs)
            data = json.loads(contents[-1]['content'])
            assert data['evidence_analyses'][0]['asset_hash'] == digest
            assert data['evidence_analyses'][0]['analysis']['description'] == '画面是红色方块'
            if self.failures:
                self.failures -= 1
                raise OSError('temporary')
            return Completion('', 'ds', {'text': '抽样画面有个红色方块，声音还没分析'})

    engine.models = Models()
    jid = engine.queue.enqueue('tool', {'group_id': '123', 'messages': [], 'reply_id': '100',
        'persona_id': 'assistant', 'tool_name': 'video_read', 'tool_args': {}},
        dedupe_key='evidence', priority=1, source_time=1)
    with pytest.raises(OSError):
        await engine.process(engine.queue.get(jid))
    assert calls == [digest] and not qq.sent
    await engine.process(engine.queue.get(jid))
    assert calls == [digest] and len(qq.sent) == 1
    assert all(segment['type'] != 'image' for segment in qq.sent[0][1])
    store.close()
