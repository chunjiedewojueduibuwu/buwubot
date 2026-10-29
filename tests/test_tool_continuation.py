import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_engine import ModelsFixture, build

from buwubot.coordinator import WorkPaused
from buwubot.engine import BotEngine
from buwubot.models import Completion
from buwubot.tools import ToolRegistry, ToolResult

NOW = datetime(2026, 10, 24, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


@pytest.mark.parametrize('small', [False, True])
def test_source_context_bounds_serialized_bytes_preserving_full_durable_sources(small):
    sources = ([{} for _ in range(10000)] if small else
        [{'page': number, 'url': f'https://example.com/{number}', 'title': '\0' * 300}
         for number in range(1000)])
    original = json.dumps(sources)
    view = BotEngine._tool_source_context(sources)
    assert len(json.dumps(view, ensure_ascii=False).encode()) <= 24000
    assert len(view) < len(sources)
    if not small:
        assert view[0]['page'] == 0 and view[-1]['page'] == 999
        assert any(400 < row['page'] < 600 for row in view)
    assert json.dumps(sources) == original


async def test_continuation_requires_explicit_registered_permission_and_valid_result():
    tools = ToolRegistry()
    async def handler(_):
        return {'state': 'succeeded', 'next_arguments': {'page': 2}}
    tools.register('read', handler, description='read', input_schema={'type': 'object'})
    with pytest.raises(ValueError):
        await tools.invoke('read', {})
    tools.unregister('read')
    tools.register('read', handler, description='read', input_schema={'type': 'object'}, continuation=True)
    assert (await tools.invoke('read', {}))['next_arguments'] == {'page': 2}
    for result in ({'state': 'pending', 'operation_id': 'saved', 'next_arguments': {}},
                   {'state': 'succeeded', 'next_arguments': {'request_id': 'forged'}},
                   {'state': 'succeeded', 'next_arguments': {'value': '\0' * 4000}}):
        with pytest.raises(ValueError):
            ToolResult.model_validate(result)


def register(engine, calls):
    async def read(arguments):
        calls.append(arguments)
        page = arguments.get('page', 1)
        result = {'state': 'succeeded', 'data': {'text': 'first' if page == 1 else 'second',
            'source': {'page': page, 'coverage': 'actual_text'}}}
        if page == 1:
            result['next_arguments'] = {'page': 2}
        return result
    engine.tools.register('read', read, description='read', continuation=True,
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False})


def enqueue(engine):
    return engine.queue.enqueue('tool', {'group_id': '123', 'messages': [], 'reply_id': '100',
        'persona_id': 'assistant', 'tool_name': 'read', 'tool_args': {}},
        dedupe_key='continuation', priority=1, source_time=1)


class SequenceModels(ModelsFixture):
    async def call(self, role, payload, **kwargs):
        if role != 'chat':
            return await super().call(role, payload, **kwargs)
        data = json.loads(payload[-1]['content'])
        if data.get('task') == 'tool_sequence_notes':
            return Completion('', 'cn:deepseek-v4.1-flash',
                {'summary': (data['previous_notes'] + ' ' + data['result']['data']['text']).strip()})
        assert data['sequence_notes'] == 'first second'
        assert [item['page'] for item in data['sequence_sources']] == [1, 2]
        return Completion('', 'cn:deepseek-v4.1-flash', {'text': '两个片段合并后的结果'})


async def test_sequence_checkpoints_yields_resumes_and_sends_once(tmp_path):
    store, engine, qq, _ = build(tmp_path, lambda: NOW)
    calls = []
    register(engine, calls)
    engine.models = SequenceModels()
    jid = enqueue(engine)
    await engine.process(engine.queue.get(jid))
    saved = engine.queue.get(jid)
    assert saved['state'] == 'pending' and saved['next_attempt'] > NOW.timestamp()
    assert saved['payload']['sequence_notes'] == 'first' and not qq.sent
    store.close()
    store, engine, qq, _ = build(tmp_path, lambda: NOW)
    register(engine, calls)
    engine.models = SequenceModels()
    await engine.process(engine.queue.get(jid))
    await engine.process(engine.queue.get(jid))
    assert len(calls) == 2 and calls[0]['request_id'] != calls[1]['request_id']
    assert len(qq.sent) == 1 and engine.queue.get(jid)['state'] == 'succeeded'
    store.close()


async def test_main_failure_reuses_saved_current_material_after_restart(tmp_path):
    store, engine, qq, _ = build(tmp_path, lambda: NOW)
    calls = []
    register(engine, calls)
    class Failed(SequenceModels):
        async def call(self, *args, **kwargs):
            raise OSError('temporary model failure')
    engine.models = Failed()
    jid = enqueue(engine)
    with pytest.raises(OSError):
        await engine.process(engine.queue.get(jid))
    assert len(calls) == 1 and not qq.sent
    assert engine.queue.get(jid)['payload']['tool_result']['data']['text'] == 'first'
    store.close()


    store, engine, qq, _ = build(tmp_path, lambda: NOW)
    register(engine, calls)
    engine.models = SequenceModels()
    await engine.process(engine.queue.get(jid))
    assert len(calls) == 1 and engine.queue.get(jid)['payload']['sequence_notes'] == 'first'
    store.close()


async def test_successful_progress_resets_failure_backoff_for_next_segment(tmp_path):
    store, engine, _, _ = build(tmp_path, lambda: NOW)
    register(engine, [])
    engine.models = SequenceModels()
    jid = enqueue(engine)
    with store.transaction() as database:
        database.execute('UPDATE jobs SET attempts=20 WHERE id=?', (jid,))
    await engine.process(engine.queue.get(jid))
    assert engine.queue.get(jid)['attempts'] == 0
    store.close()


async def test_sequence_rejects_repeated_arguments_without_reply(tmp_path):
    store, engine, qq, _ = build(tmp_path, lambda: NOW)
    async def loop(_):
        return {'state': 'succeeded', 'next_arguments': {}}
    engine.tools.register('read', loop, description='read', continuation=True,
        input_schema={'type': 'object'})
    jid = enqueue(engine)
    with pytest.raises(ValueError):
        await engine.process(engine.queue.get(jid))
    assert not qq.sent
    store.close()


async def test_persona_edit_restarts_notes_with_original_arguments(tmp_path):
    store, engine, qq, _ = build(tmp_path, lambda: NOW)
    calls = []
    register(engine, calls)
    engine.models = SequenceModels()
    jid = enqueue(engine)
    await engine.process(engine.queue.get(jid))
    cfg, revision = store.settings()
    changed = cfg.model_copy(update={'persona': cfg.persona + ' 调整表达习惯'})
    store.save_settings(changed, revision)
    engine.settings = changed
    await engine.process(engine.queue.get(jid))
    assert len(calls) == 2 and all('page' not in item for item in calls)
    assert engine.queue.get(jid)['payload']['sequence_notes'] == 'first' and not qq.sent
    store.close()


async def test_adjacent_timeline_sources_remain_compact_with_sampling_count(tmp_path):
    store, engine, qq, _ = build(tmp_path, lambda: NOW)
    calls = []
    register(engine, calls)
    old = engine.tools.handlers['read']
    async def read(arguments):
        result = await old(arguments)
        page = arguments.get('page', 1)
        result['data']['source'] = {'url': 'https://example.com/video', 'part': 1,
            'start_seconds': (page - 1) * 60, 'end_seconds': page * 60,
            'coverage': 'sampled_frames', 'sampled_frame_count': 3, 'audio_analyzed': False}
        return result
    engine.tools.handlers['read'] = read
    class Models(SequenceModels):
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]['content'])
            if data.get('task') == 'tool_sequence_notes':
                return await super().call(role, payload, **kwargs)
            assert data['sequence_notes'] == 'first second'
            assert len(data['sequence_sources']) == 1
            source = data['sequence_sources'][0]
            assert source['start_seconds'] == 0 and source['end_seconds'] == 120
            assert source['sampled_frame_count'] == 6 and not source['audio_analyzed']
            return Completion('', 'cn:deepseek-v4.1-flash', {'text': '依据六个抽样画面总结，未分析声音'})
    engine.models = Models()
    jid = enqueue(engine)
    await engine.process(engine.queue.get(jid))
    await engine.process(engine.queue.get(jid))
    assert len(qq.sent) == 1
    store.close()


@pytest.mark.parametrize('reason', ['called', 'routine', 'parent_cancelled'])
async def test_long_tool_read_can_be_interrupted_without_losing_task(tmp_path, reason):
    now = NOW
    store, engine, qq, _ = build(tmp_path, lambda: now)
    started, stopped = asyncio.Event(), asyncio.Event()
    async def read(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    engine.tools.register('read', read, description='read', continuation=True,
                          input_schema={'type': 'object'})
    jid = enqueue(engine)
    task = asyncio.create_task(engine.process(engine.queue.get(jid)))
    supervisor = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        if reason == 'called':
            engine.ingest({'post_type':'message', 'message_type':'private', 'self_id':11,
                'user_id':10001, 'message_id':2, 'time':now.timestamp(),
                'sender':{'nickname':'owner'}, 'message':[{'type':'text','data':{'text':'助手在吗'}}]})
        elif reason == 'routine':
            now = now.replace(hour=23)
            supervisor = asyncio.create_task(engine._supervise())
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if reason == 'parent_cancelled' else WorkPaused):
            await asyncio.wait_for(task, .8)
        assert stopped.is_set() and not qq.sent
        assert engine.queue.get(jid)['payload']['tool_args'] == {}
    finally:
        if not task.done():
            task.cancel()
        if supervisor:
            supervisor.cancel()
        await asyncio.gather(*[item for item in (task, supervisor) if item], return_exceptions=True)
        store.close()
