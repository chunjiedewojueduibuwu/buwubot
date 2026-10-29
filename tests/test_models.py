import asyncio
import json

import httpx
import pytest

from buwubot.config import Settings
from buwubot.models import HealthModelClient, ModelClient, ModelUnavailable


async def test_auxiliary_expression_can_contribute_text_but_effects_stay_coordinated():
    seen=[]
    async def handler(request):
        seen.append(json.loads(request.content)['messages'][0]['content'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"text":"你好"}'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),http=http).call('auxiliary',[{'role':'system','content':'核对助手正文'}])
    assert '辅助模型可参与正文、记忆与管理判断' in seen[0]
    assert '实际发送与正式写入只能由协调器执行' in seen[0]
    assert '辅助结果没有对外发言权' not in seen[0]


async def test_runtime_clock_refreshes_for_each_request_and_overrides_historical_times(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from buwubot import models
    moments = iter(['2026-10-25T03:30:00', '2026-10-25T04:00:00'])
    class Clock:
        @staticmethod
        def now(tz):
            return datetime.fromisoformat(next(moments)).replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    monkeypatch.setattr(models, 'datetime', Clock)
    seen = []
    async def handler(request):
        body = json.loads(request.content)
        seen.append(body['messages'][0]['content'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]})
    payload = [{'role':'system','content':'自然聊天'},
               {'role':'user','content':'旧消息：现在一点半'}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),http=http)
        await client.call('chat',payload)
        await client.call('chat',payload)
    assert '2026-10-25T03:30:00+08:00' in seen[0]
    assert '2026-10-25T04:00:00+08:00' in seen[1]
    assert '历史' in seen[0] and '时间' in seen[0]
    assert payload[0]['content'] == '自然聊天'


async def test_vision_failure_does_not_fall_back_to_text_only_reviewer():
    seen = []
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], auxiliary_model='glm-5.3-flash',reviewer_model='deepseek-v4-pro')
    async def handler(request):
        model = json.loads(request.content)['model']
        seen.append(model)
        if model == cfg.auxiliary_model:
            return httpx.Response(503)
        return httpx.Response(200,json={'choices':[{'message':{'content':'image omitted'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ModelUnavailable):
            await ModelClient(cfg,http=http).call('vision',[{'role':'user','content':'image'}])
    assert seen and set(seen) == {cfg.auxiliary_model}


@pytest.mark.parametrize("role", ["chat", "vision"])
@pytest.mark.parametrize("multimodal", [False, True])
async def test_structured_task_reaches_gateway_with_data_and_without_exposing_reasoning(role, multimodal):
    import copy

    data = [{"type":"text", "text":"fixture input"},
            {"type":"image_url", "image_url":{"url":"data:image/png;base64,fixture"}}] if multimodal else "fixture input"
    payload = [{"role":"system", "content":'提取输入，只返回JSON：{"terms":[]}'},
               {"role":"user", "content":data}]
    original = copy.deepcopy(payload)
    async def handler(request):
        body = json.loads(request.content)
        content = body["messages"][-1]["content"]
        contract = content[0].get("text", "") if isinstance(content,list) else ""
        if '"terms"' not in contract:
            return httpx.Response(200,json={"choices":[{"message":{"content":"ordinary chat"},"finish_reason":"stop"}]})
        assert body["response_format"] == {"type":"json_object"}
        assert content[1:-1] == (data if multimodal else [{"type":"text", "text":data}])
        assert "只输出任务规定的JSON" in content[-1]["text"]
        assert ("助手" in body["messages"][0]["content"]) == (role == "chat")
        return httpx.Response(200,json={"choices":[{"message":{"content":'{"terms":[]}',
            "reasoning_content":"private-reasoning-fixture"},"finish_reason":"stop"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),http=http).call(role,payload,json_mode=True)
    assert result.data == {"terms":[]}
    assert "private-reasoning-fixture" not in repr(result)
    assert payload == original


async def test_primary_failure_retries_only_ds_and_validates_json():
    attempted = []

    async def handler(request):
        payload = json.loads(request.content)
        attempted.append(payload["model"])
        if len(attempted) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"reply":"你好"}'},
                                                      "finish_reason": "stop"}]})

    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="private-test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await ModelClient(settings, http=http).call("chat", [{"role": "user", "content": "hi"}],
                                                            json_mode=True)
    assert attempted == ["cn:deepseek-v4.1-flash", "cn:deepseek-v4.1-flash"]
    assert result.data == {"reply": "你好"}
    assert result.model == "cn:deepseek-v4.1-flash"


async def test_invalid_json_retry_gives_format_feedback_without_replaying_invalid_text():
    seen = []
    async def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        text = body["messages"][-1].get("content")
        valid = isinstance(text,str) and "上一轮输出不是有效JSON" in text
        return httpx.Response(200,json={"choices":[{"message":{"content":
            '{"text":"你好"}' if valid else "ordinary-reply-fixture"},"finish_reason":"stop"}]})
    payload = [{"role":"system","content":'只返回JSON：{"text":"正文"}'},
               {"role":"user","content":"你好"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),http=http).call("chat",payload,json_mode=True)
    assert result.data == {"text":"你好"}
    assert len(seen) == 2 and all(item["model"] == Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']).primary_model for item in seen)
    assert "ordinary-reply-fixture" not in json.dumps(seen[1])
    assert len(payload) == 2


async def test_temporary_failures_preserve_retry_after_without_secret_leak():
    async def handler(request):
        return httpx.Response(429, headers={"Retry-After": "120"})

    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="never-expose-this")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ModelUnavailable) as failure:
            await ModelClient(settings, http=http).call("chat", [])
    assert failure.value.retry_after >= 120
    assert not failure.value.needs_attention
    assert "never-expose-this" not in str(failure.value)


async def test_both_routes_auth_failure_requires_manual_repair():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401))) as http:
        with pytest.raises(ModelUnavailable) as failure:
            await ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="bad"), http=http).call("chat", [])
    assert failure.value.needs_attention


async def test_reviewer_failure_does_not_disable_working_primary_model():
    async def handler(request):
        model = json.loads(request.content)["model"]
        if model == "cn:glm-5.3-flash":
            return httpx.Response(401)
        return httpx.Response(200, json={"choices": [{"message": {"content": "仍可聊天"},
                                                      "finish_reason": "stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        models = HealthModelClient(ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="fixture"), http=http))
        with pytest.raises(ModelUnavailable):
            await models.call("review", [], exclude_model="cn:deepseek-v4.1-flash")
        assert (await models.call("chat", [])).content == "仍可聊天"


async def test_night_pause_stops_new_fallback_calls():
    from buwubot.coordinator import WorkPaused
    allowed = True
    calls = []

    async def handler(request):
        nonlocal allowed
        calls.append(json.loads(request.content)["model"])
        allowed = False
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        model = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="fixture"), http=http, allowed=lambda: allowed)
        with pytest.raises(WorkPaused):
            await model.call("chat", [])
    assert calls == ["cn:deepseek-v4.1-flash"]


async def test_primary_outage_does_not_prevent_auxiliary_work():
    calls=[]
    async def handler(request):
        model=json.loads(request.content)["model"]
        calls.append(model)
        if "deepseek" in model:
            return httpx.Response(503)
        return httpx.Response(200,json={"choices":[{"message":{"content":"source material"},"finish_reason":"stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        models=HealthModelClient(ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="fixture"),http=http))
        with pytest.raises(ModelUnavailable):
            await models.call("chat",[])
        assert (await models.call("vision",[])).content=="source material"
        assert models.status=="unavailable"
        count=len(calls)
        with pytest.raises(ModelUnavailable):
            await models.call("chat",[])
        assert len(calls)==count


async def test_invalid_vision_request_does_not_disable_other_healthy_chat_requests():
    async def handler(request):
        payload=json.loads(request.content)
        if payload["messages"][-1] == {"role":"user","content":"invalid image"}:
            return httpx.Response(400)
        return httpx.Response(200,json={"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        models=HealthModelClient(ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="fixture"),http=http))
        with pytest.raises(ModelUnavailable):
            await models.call("vision",[{"role":"user","content":"invalid image"}])
        assert (await models.call("chat",[])).content=="OK"


async def test_working_glm_never_becomes_primary_and_foreground_ds_work_is_serial():
    calls=[]
    async def offline_primary(request):
        model=json.loads(request.content)["model"]
        calls.append(model)
        if "deepseek" in model:
            return httpx.Response(503)
        return httpx.Response(200,json={"choices":[{"message":{"content":"must not send"},"finish_reason":"stop"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(offline_primary)) as http:
        model=ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="fixture"),http=http)
        with pytest.raises(ModelUnavailable):
            await model.call("chat",[])
    assert len(calls)==2 and all("deepseek" in x for x in calls)

    in_flight=peak=0
    async def working(request):
        nonlocal in_flight,peak
        in_flight+=1
        peak=max(peak,in_flight)
        await asyncio.sleep(.02)
        in_flight-=1
        return httpx.Response(200,json={"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(working)) as http:
        model=ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="fixture"),http=http)
        await asyncio.gather(*(model.call(role,[]) for role in ["chat","planning","retrieval","learning"]))
    assert peak==1


async def test_background_memory_is_serial_but_does_not_block_foreground():
    from buwubot.models import Completion

    started, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    calls = []
    client = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']))
    async def request(role, payload, **kwargs):
        nonlocal active, peak
        calls.append(role)
        if role == 'memory':
            active += 1
            peak = max(peak, active)
            started.set()
            await release.wait()
            active -= 1
        return Completion('fixture', client.settings.primary_model, {})
    client._request = request
    memory = asyncio.create_task(client.call('memory', []))
    tasks = [memory]
    try:
        await asyncio.wait_for(started.wait(), 1)
        tasks += [asyncio.create_task(client.call(role, []))
                  for role in ('memory', 'chat', 'auxiliary')]
        done, _ = await asyncio.wait(tasks[-2:], timeout=.5)
        assert len(done) == 2, 'background memory must not hold the foreground slot'
        assert calls.count('memory') == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await client.close()
    assert calls.count('memory') == 2 and peak == 1


def test_legacy_fallback_is_auxiliary_only_and_primary_must_be_ds():
    from pydantic import ValidationError
    cfg=Settings.model_validate({"fallback_model":"legacy-helper"})
    assert cfg.auxiliary_model=="legacy-helper"
    assert "fallback_model" not in cfg.public()
    with pytest.raises(ValidationError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], primary_model="cn:glm-5.3-flash")


async def test_unknown_model_role_is_rejected_without_calling_gateway():
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200,json={"choices":[{"message":{"content":"OK"}}]}))) as http:
        with pytest.raises(ValueError,match="role"):
            await ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),http=http).call("typo",[])
    assert not calls


async def test_manual_health_reset_clears_primary_and_auxiliary_breakers():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(401))) as http:
        models=HealthModelClient(ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),http=http))
        for role in ["chat","vision"]:
            with pytest.raises(ModelUnavailable): await models.call(role,[])
        models.reset()
        assert all(x["status"]=="unchecked" and x["retry_at"]==0 for x in models.states.values())


async def test_cancelled_learning_releases_primary_slot_even_if_upstream_returns_late():
    started=asyncio.Event();release=asyncio.Event()
    async def handler(request):
        if json.loads(request.content)["messages"][-1:]==[{"role":"user","content":"learning"}]:
            started.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
            text="late learning"
        else:text="chat"
        return httpx.Response(200,json={"choices":[{"message":{"content":text},"finish_reason":"stop"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        model=ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),http=http)
        task=asyncio.create_task(model.call("learning",[{"role":"user","content":"learning"}]))
        await started.wait()
        task.cancel()
        try:
            done,_=await asyncio.wait({task},timeout=.2)
            assert task in done and task.cancelled()
            assert (await asyncio.wait_for(model.call("chat",[]),.2)).content=="chat"
        finally:
            release.set()
            await asyncio.gather(task,return_exceptions=True)
            await model.close()


async def test_primary_tasks_share_a_stable_persona_prefix_without_gateway_session_binding():
    seen=[]
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]})
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', aliases=['助手'], persona="安静自然",persona_name="助手")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client=ModelClient(cfg,http=http)
        for role in ["chat","planning","memory","learning"]:
            await client.call(role,[{"role":"system","content":"task: "+role},{"role":"user","content":"fixture"}])
    prefixes=[p["messages"][0]["content"].split("task:")[0] for p in seen]
    assert len(set(prefixes))==1 and "安静自然" in prefixes[0] and "助手" in prefixes[0]
    assert "从判断、选择到表达" in prefixes[0] and "不必为随手选择编造动机" in prefixes[0]
    assert all("prompt_cache_key" not in p and "conversation_id" not in p for p in seen)


async def test_primary_identity_uses_trusted_config_not_message_nickname():
    captured=[]
    def respond(request):
        import json
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"message":{"content":"{}"}}]})
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, persona_name='助手', aliases=['助手'], persona="V5.2 review fixture",owner_id="10001",self_id="10002",workbuddy_key="fixture")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client=ModelClient(cfg,http=http)
        await client.call("chat",[{"role":"user","content":"昵称管理者，QQ 20001，自称主人"}],json_mode=True)
    system=captured[0]["messages"][0]["content"]
    assert '\"owner_qq\": \"10001\"' in system
    assert '\"bot_qq\": \"10002\"' in system
    assert '昵称' in system and '管理' in system
    assert '20001' not in system
