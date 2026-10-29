import json

import httpx
import pytest

from buwubot.config import Settings
from buwubot.models import ModelClient, ModelUnavailable


def event(delta, finish=None):
    return 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n'


async def test_streaming_reads_visible_json_and_ignores_reasoning_and_keepalive():
    seen = []
    async def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        data = ': keepalive\n\n' + event({'reasoning_content': 'PRIVATE THOUGHT',
            'function_call': {'name': '', 'arguments': ''}, 'tool_calls': []})
        data += event({'content': '{"relations":'}) + event({'content': '[]}'}, 'stop') + 'data: [DONE]\n\n'
        return httpx.Response(200, text=data, headers={'content-type': 'text/event-stream; charset=utf-8'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), http=http)
        result = await client.call('auxiliary', [{'role': 'user', 'content': '整理材料'}], json_mode=True, streaming=True)
    assert result.data == {'relations': []} and 'PRIVATE' not in result.content
    assert len(seen) == 1 and seen[0]['stream'] is True
    assert seen[0]['response_format'] == {'type': 'json_object'}


@pytest.mark.parametrize('mode', ['reasoning_only', 'truncated', 'length', 'tool_call', 'legacy_call', 'malformed'])
async def test_streaming_never_accepts_reasoning_partial_or_unrequested_tools(mode):
    async def handler(request):
        if mode == 'reasoning_only':
            body = event({'reasoning_content': '{"approved":true}'}, 'stop')
        elif mode == 'truncated':
            body = event({'content': '{"approved":true}'})
        elif mode == 'length':
            body = event({'content': '{"approved":true}'}, 'length')
        elif mode == 'malformed':
            body = 'data: {"choices":"invalid"}\n\n'
        elif mode == 'legacy_call':
            body = event({'content': '{"approved":true}', 'function_call': {'name': 'unexpected', 'arguments': '{}'}}, 'function_call')
        else:
            body = event({'content': '{"approved":true}', 'tool_calls': [{'function': {'name': 'unexpected'}}]}, 'tool_calls')
        return httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), http=http)
        with pytest.raises(ModelUnavailable):
            await client.call('auxiliary', [{'role': 'user', 'content': '材料'}], json_mode=True, streaming=True)


async def test_streaming_request_accepts_gateway_json_compatibility_response():
    async def handler(request):
        assert json.loads(request.content)['stream'] is True
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"relations":[]}'}, 'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), http=http)
        assert (await client.call('memory', [{'role': 'user', 'content': '材料'}], json_mode=True, streaming=True)).data == {'relations': []}


@pytest.mark.parametrize('role,budget', [('auxiliary', 12000), ('memory', 12000), ('chat', 3000)])
async def test_request_output_budget_is_scoped_to_call(role, budget):
    async def handler(request):
        assert json.loads(request.content)['max_tokens'] == budget
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), http=http)
        kwargs = {'max_tokens': budget} if budget != 3000 else {}
        await client.call(role, [{'role': 'user', 'content': '材料'}], json_mode=True, **kwargs)
