import pytest

from buwubot.tools import ToolRegistry


async def test_skill_registration_lifecycle_and_structured_result():
    tools = ToolRegistry()
    calls = []
    async def search(payload):
        calls.append(payload)
        return {'state':'succeeded', 'message':'找到一个来源',
                'data':{'results':[{'title':'示例','url':'https://example.com/'}]}}
    schema = {'type':'object','properties':{'query':{'type':'string'}},'required':['query']}
    tools.register('web_search', search, description='搜索网页', input_schema=schema)
    schema['required'].append('should_not_leak')
    spec = next(item for item in tools.definitions() if item['name'] == 'web_search')
    assert spec['input']['required'] == ['query'] and spec['enabled']
    spec['input']['required'].append('should_not_leak_either')
    assert tools.definitions()[-1]['input']['required'] == ['query']
    result = await tools.invoke('web_search', {'query':'示例'}, request_id='job-1', operation_id='op-1')
    assert result['data']['results'][0]['url'] == 'https://example.com/'
    assert calls == [{'query':'示例','request_id':'job-1','operation_id':'op-1'}]
    tools.disable('web_search')
    assert not tools.definitions()[-1]['enabled']
    assert (await tools.invoke('web_search', {}))['state'] == 'unavailable'
    assert len(calls) == 1
    tools.enable('web_search')
    await tools.invoke('web_search', {'query':'再次'})
    assert len(calls) == 2
    tools.unregister('web_search')
    assert all(item['name'] != 'web_search' for item in tools.definitions())
    assert '画图' not in (await tools.invoke('web_search', {}))['message']


@pytest.mark.parametrize('name', ['../escape', 'bad name', '', 'image_generate'])
def test_plugin_tool_name_cannot_replace_reserved_or_invalid_name(name):
    tools = ToolRegistry()
    async def handler(payload):
        return {'state':'succeeded','message':'ok'}
    with pytest.raises(ValueError):
        tools.register(name, handler, description='test', input_schema={'type':'object'})


async def test_removing_skill_does_not_interrupt_already_started_call():
    import asyncio
    tools = ToolRegistry()
    started, finish = asyncio.Event(), asyncio.Event()
    async def handler(payload):
        started.set()
        await finish.wait()
        return {'state':'succeeded','message':'原调用完成'}
    tools.register('sample', handler, description='测试', input_schema={'type':'object'})
    with pytest.raises(ValueError):
        tools.register('sample', handler, description='重复', input_schema={'type':'object'})
    task = asyncio.create_task(tools.invoke('sample', {}, request_id='one'))
    await started.wait()
    tools.unregister('sample')
    assert (await tools.invoke('sample', {}))['state'] == 'unavailable'
    finish.set()
    assert (await task)['message'] == '原调用完成'


async def test_structured_skill_data_must_be_json_and_bounded():
    tools = ToolRegistry()
    async def oversized(payload):
        return {'state':'succeeded','data':{'body':'x' * 65537}}
    tools.register('oversized', oversized, description='test', input_schema={'type':'object'})
    with pytest.raises(ValueError):
        await tools.invoke('oversized', {})
    async def invalid(payload):
        return {'state':'succeeded','data':{'not_json':object()}}
    tools.register('invalid', invalid, description='test', input_schema={'type':'object'})
    with pytest.raises(ValueError):
        await tools.invoke('invalid', {})


async def test_new_skill_schema_and_data_reach_primary_before_sending(tmp_path):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import ModelsFixture, build

    from buwubot.models import Completion

    now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path, lambda:now)
    async def search(payload):
        assert not qq.sent
        assert payload['query'] == '例子' and payload['request_id']
        return {'state':'succeeded', 'data':{'results':[{'title':'示例','url':'https://example.com/'}]}}
    engine.tools.register('web_search', search, description='搜索网页',
        input_schema={'type':'object','properties':{'query':{'type':'string'}}})
    calls = []
    class Models(ModelsFixture):
        async def call(self, role, contents, **kwargs):
            if role != 'chat':
                return await super().call(role, contents, **kwargs)
            calls.append(role)
            if isinstance(contents[-1]['content'], list):
                data = json.loads(contents[-1]['content'][0]['text'])
                spec = next(item for item in data['tools'] if item['name'] == 'web_search')
                assert spec['enabled'] and 'query' in spec['input']['properties']
                return Completion('', 'ds', {'tool':'web_search','tool_args':{'query':'例子'}})
            data = json.loads(contents[-1]['content'])
            assert data['result']['data']['results'][0]['title'] == '示例'
            return Completion('', 'ds', {'text':'找到这个来源：https://example.com/'})
    engine.models = Models()
    engine.memory.models = engine.models
    jid = engine.queue.enqueue('reply', {'group_id':'123','messages':[]},
        dedupe_key='new-skill', priority=0, source_time=1)
    await engine.process(engine.queue.get(jid))
    assert not qq.sent
    task = engine.queue.claim(now.timestamp(), kinds=['tool'])
    await engine.process(task)
    assert calls == ['chat', 'chat'] and len(qq.sent) == 1
    assert engine.queue.get(task['id'])['state'] == 'succeeded'
    store.close()


async def test_drawing_is_explicitly_unavailable_and_does_not_create_work():
    tools = ToolRegistry()
    assert {item["name"] for item in tools.definitions()} == {"image_generate", "image_edit"}
    assert all(not item["enabled"] for item in tools.definitions())
    result = await tools.invoke("image_generate", {"prompt": "一只猫"})
    assert result == {"state": "unavailable", "message": "暂时还不会画图"}


async def test_tool_contract_preserves_idempotency_and_checks_pending_and_assets():
    tools = ToolRegistry()
    calls = []
    async def handler(payload):
        calls.append(payload)
        return {"state":"pending", "operation_id":"remote-1", "message":"处理中"}
    tools.handlers["image_generate"] = handler
    result = await tools.invoke("image_generate", {"prompt":"猫"}, request_id="durable-1")
    assert result["operation_id"] == "remote-1"
    assert calls[0]["request_id"] == "durable-1"
    async def invalid(payload):
        return {"state":"succeeded", "images":["https://untrusted.example/file"]}
    tools.handlers["image_generate"] = invalid
    with pytest.raises(ValueError):
        await tools.invoke("image_generate", {}, request_id="durable-1")
    async def missing_operation(payload):
        return {"state":"pending"}
    tools.handlers["image_generate"] = missing_operation
    with pytest.raises(ValueError):
        await tools.invoke("image_generate", {}, request_id="durable-1")
