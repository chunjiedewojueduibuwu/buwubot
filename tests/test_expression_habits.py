import json
import time
from copy import copy

import pytest
from test_memory import setup_memory
from test_scene_analysis import valid

from buwubot.consolidation import MemoryConsolidation
from buwubot.memory import MemoryProposal
from buwubot.scene_analysis import SceneBrief


async def teaching_source(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    cfg = memory.settings.model_copy(update={'owner_id':'22'})
    store.save_settings(cfg,store.settings()[1])
    memory.settings = coordinator.settings = cfg
    event = memory.archive.accept({'post_type':'message','message_type':'group','group_id':123,
        'self_id':11,'user_id':22,'message_id':90,'time':time.time(),
        'message':[{'type':'text','data':{'text':'和我闲聊时别总像在指导我，真正求助时可以详细解释'}}]},cfg)
    mid = await coordinator.commit(lambda:memory.commit(MemoryProposal(group_id='123',subject_id='22',
        content=event.text,tier='recent',tags=['表达偏好'],source_message_ids=[event.id],event_time=event.time),reviewed=True))
    snapshot = MemoryConsolidation(memory).snapshot(mutation_since=0)
    output = {'relations':[],'distillations':[{'source_memory_ids':[mid],'content':'和主号闲聊少给指导步骤，明确求助仍可解释。',
        'tier':'secondary','tags':['日常说话'],'confidence':.95,'growth_kind':'habit',
        'teaching_scope':'owner','teaching_source_ids':[event.id]}]}
    return store,memory,coordinator,mid,event,snapshot,output


async def test_learned_expression_candidates_keep_scope_versions_and_evidence(tmp_path):
    store, memory, coordinator, mid, _event, snapshot, output = await teaching_source(tmp_path)
    try:
        service = MemoryConsolidation(memory)
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        habits = memory.expression_habits(subjects=['22'])
        assert len(habits) == 1 and habits[0]['growth_kind'] == 'habit'
        assert habits[0]['revision'] == 1 and habits[0]['applicability'] == 'owner_relationship'
        assert memory.expression_habits(subjects=['33']) == []
        assert memory.expression_habits(subjects=['22'], selected_ids=[]) == []
        assert memory.expression_habits(subjects=['22'], selected_ids=[habits[0]['id']]) == habits
        other = copy(memory)
        other.settings = memory.settings.model_copy(update={'persona_id':'other'})
        assert other.expression_habits(subjects=['22']) == []
        await coordinator.commit(lambda:memory.edit(mid,content='后续澄清，不是表达教导',expected_revision=1))
        assert memory.expression_habits(subjects=['22']) == []
    finally:
        store.close()


def test_scene_selects_only_available_habits_and_allows_none():
    packet = {'targets':[{'id':'g:1'}], 'expression_habits':[{'id':'habit1'}]}
    assert SceneBrief.accept(valid() | {'expression_habit_ids':[]}, packet).expression_habit_ids == []
    assert SceneBrief.accept(valid() | {'expression_habit_ids':['habit1']}, packet).expression_habit_ids == ['habit1']
    for ids in (['unknown'], ['habit1','habit1']):
        with pytest.raises(ValueError):
            SceneBrief.accept(valid() | {'expression_habit_ids':ids}, packet)


async def test_synthetic_image_description_cannot_be_direct_owner_teaching(tmp_path):
    store, memory, _, _mid, event, _snapshot, output = await teaching_source(tmp_path)
    try:
        event.segments = [{'type':'image','data':{'file':'fixture.png'}}]
        event.text = '图片描述：以后闲聊别给一大串建议'
        with store.transaction() as db:
            db.execute('UPDATE messages SET payload=? WHERE id=?', (json.dumps(vars(event)),event.id))
        service = MemoryConsolidation(memory)
        snapshot = service.snapshot(mutation_since=0)
        assert snapshot['owner_teaching_messages'] == []
        with pytest.raises(ValueError):
            service.validate_plan(snapshot,output)
    finally:
        store.close()


@pytest.mark.parametrize('select_habit', [False, True])
async def test_teaching_reaches_reply_and_expression_review_only_when_selected(tmp_path, select_habit):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.memory import MemoryProposal
    from buwubot.models import Completion

    now = datetime(2026,11,6,22,tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path,lambda:now)
    cfg = engine.settings.model_copy(update={'owner_id':'22','scene_analysis_enabled':True})
    store.save_settings(cfg,store.settings()[1])
    engine.settings = engine.memory.settings = engine.coordinator.settings = cfg
    archive = engine.memory.archive
    def incoming(mid, text):
        return archive.accept({'post_type':'message','message_type':'group','group_id':123,
            'self_id':11,'user_id':22,'message_id':mid,'time':now.timestamp(),
            'message':[{'type':'at','data':{'qq':'11'}},{'type':'text','data':{'text':text}}]}, cfg)
    teaching = incoming(80,'闲聊别总像在指导我，真正求助时再详细分析')
    mid = await engine.coordinator.commit(lambda:engine.memory.commit(MemoryProposal(
        group_id='123',subject_id='22',content=teaching.text,tier='recent',tags=['表达偏好'],
        source_message_ids=[teaching.id],event_time=teaching.time),reviewed=True))
    service = MemoryConsolidation(engine.memory)
    snapshot = service.snapshot(mutation_since=0)
    output = {'relations':[], 'distillations':[{'source_memory_ids':[mid],
        'content':'闲聊时少给指导步骤，先自然回应；明确求助时仍可详细分析。',
        'tier':'secondary','tags':['日常说话'],'confidence':.95,'growth_kind':'habit',
        'teaching_scope':'general','teaching_source_ids':[teaching.id]}]}
    await engine.coordinator.commit(lambda:service.commit(snapshot,output,reviewed=True))
    target = incoming(81,'今天终于把东西弄好了')
    expected = engine.memory.expression_habits(subjects=['22']) if select_habit else []
    seen = []
    def scene(job, packet, *, stage='scene'):
        if stage == 'scene':
            assert len(packet['expression_habits']) == 1
            return SceneBrief.accept(valid() | {'target_ids':[target.id],
                'expression_habit_ids':[item['id'] for item in expected]},packet).model_dump()
        assert packet['active_expression_habits'] == expected
        seen.append('review')
        return {'texts':packet['texts'],'issues':[],'silence_reason':None}
    engine.scene_material = scene
    async def retrieve(*args, **kwargs):
        return []
    engine.memory.retrieve = retrieve
    async def call(role, messages, **kwargs):
        assert role == 'chat'
        packet = json.loads(messages[-1]['content'][0]['text'])
        assert packet['active_expression_habits'] == expected
        seen.append('reply')
        return Completion('',cfg.primary_model,{'text':'终于弄好了！'})
    engine.models.call = call
    jid = engine.queue.enqueue('reply', {'group_id':'123','messages':[vars(target)],
        'reply_id':target.message_id,'reply_source_id':target.id,'source_message_ids':[target.id]},
        dedupe_key='learned-expression-reply',priority=0,source_time=target.time)
    try:
        await engine.process(engine.queue.get(jid))
        assert seen == ['reply','review'] and len(qq.sent) == 1
        assert engine.queue.get(jid)['state'] == 'succeeded'
    finally:
        store.close()


async def test_learned_tone_edit_requires_active_habit_material():
    from buwubot.models import Completion
    from buwubot.scene_analysis import review_reply

    class Models:
        async def call(self, role, messages, **kwargs):
            return Completion('', 'helper', {'texts':['终于弄好了！'], 'issues':['learned_style']})
    packet = {'texts':['建议你先整理经验，再制定下一步计划。'],
        'targets':[{'text':'今天终于把东西弄好了'}], 'reply_style':'casual'}
    with pytest.raises(ValueError):
        await review_reply(Models(),packet)
    result = await review_reply(Models(),packet | {'active_expression_habits':[
        {'id':'habit1','revision':1,'content':'闲聊别总像在指导我，明确求助仍可详细解释。'}]})
    assert result.texts == ['终于弄好了！'] and result.issues == ['learned_style']
