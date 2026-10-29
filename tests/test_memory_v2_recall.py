import json

import pytest
from test_memory import proposal, setup_memory

from buwubot.memory import MemoryProposal
from buwubot.memory_recall import fragment_budget
from buwubot.models import Completion

DAY=86400


@pytest.mark.parametrize('kind', ['empty', 'deleted', 'other_group', 'other_persona', 'core'])
async def test_empty_retrievable_scope_avoids_model_roundtrips(tmp_path, kind):
    from buwubot.memory import core_scope

    store, memory, coordinator = setup_memory(tmp_path)
    if kind != 'empty':
        mid = await coordinator.commit(lambda:memory.commit(proposal()))
        changes = {
            'deleted':('deleted', 1), 'other_group':('group_id', '999'),
            'other_persona':('persona_id', 'another'),
            'core':('group_id', core_scope(memory.settings.persona_id)),
        }
        column, value = changes[kind]
        with store.transaction() as db:
            db.execute(f'UPDATE memories SET {column}=? WHERE id=?', (value, mid))
            if kind == 'core':
                db.execute("UPDATE memories SET tier='core',persona_id=? WHERE id=?",
                           (memory.settings.persona_id, mid))
    store.set_state('memory_schema_version', 2)
    class Models:
        async def call(self, *args, **kwargs):
            raise AssertionError('no eligible ordinary or learning memory exists')
    memory.models = Models()
    try:
        assert await memory.retrieve('早安') == []
        if kind == 'core':
            assert memory.core_context() == ['甲喜欢茶，不喜欢咖啡']
    finally:
        store.close()


@pytest.mark.parametrize('scope', ['123', 'knowledge:public'])
async def test_any_visible_memory_preserves_model_expansion_even_without_a_text_hit(tmp_path, scope):
    store, memory, coordinator = setup_memory(tmp_path)
    from copy import copy
    writer = copy(memory)
    writer.settings = memory.settings.model_copy(update={'group_id':scope})
    candidate = proposal() if scope == '123' else MemoryProposal(group_id=scope,
        content='公开知识', source_message_ids=['video-source'])
    await coordinator.commit(lambda:writer.commit(candidate,
        source_snapshots=[{'id':'video-source','group_id':scope}] if scope != '123' else None))
    class Models:
        async def call(self, role, *args, **kwargs):
            assert role == 'retrieval'
            raise RuntimeError('expansion reached')
    memory.models = Models()
    try:
        with pytest.raises(RuntimeError, match='expansion reached'):
            await memory.retrieve('completely unrelated query')
    finally:
        store.close()


async def test_learning_experience_alone_preserves_model_expansion(tmp_path, monkeypatch):
    store, memory, _ = setup_memory(tmp_path)
    monkeypatch.setattr(memory, 'learning_experiences', lambda query:[{'kind':'learning_experience'}])
    class Models:
        async def call(self, *args, **kwargs):
            raise RuntimeError('expansion reached')
    memory.models = Models()
    try:
        with pytest.raises(RuntimeError, match='expansion reached'):
            await memory.retrieve('学习过什么')
    finally:
        store.close()


async def remembered(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    p=proposal().model_copy(update={'tier':'recent','significance':.5,'memory_schema_version':2,
        'content':'甲排查服务时，端口是8123；未开启代理，只有本机能访问。随后确认本机访问正常。',
        'tags':['服务排查'],'event_time':100})
    original=await coordinator.commit(lambda:memory.commit(p))
    second=await coordinator.commit(lambda:memory.commit(p.model_copy(update={
        'content':'甲本机检查使用8124，未开放外网。'})))
    gist=MemoryProposal(group_id='123',subject_id='22',tier='long_term',significance=.8,memory_schema_version=2,
        content='甲的服务仅供本机访问，需保留访问范围约束。',source_message_ids=['123:id:1'],
        tags=['服务排查'],event_time=100,snapshot_epoch=memory.epoch(),
        distilled_from=[{'memory_id':original,'expected_revision':1},{'memory_id':second,'expected_revision':1}])
    summary=await coordinator.commit(lambda:memory.commit(gist,reviewed=True))
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100+60*DAY)
    return store,memory,coordinator,original,summary


def test_fragment_budget_is_bounded_monotone_and_uses_complete_units():
    assert fragment_budget(10,1)==10
    assert fragment_budget(10,.18)==1  # Chosen floor for the screenshot's integer brackets.
    assert fragment_budget(10,0)==0
    assert [fragment_budget(4,d) for d in [0,.2,.5,.9,1]]==[0,0,2,3,4]
    with pytest.raises(ValueError):fragment_budget(2,float('nan'))
    with pytest.raises(ValueError):fragment_budget(-1,.5)


@pytest.mark.parametrize('elapsed,query',[(60,'服务本机访问'),(60*DAY,'端口8123')])
async def test_needed_single_source_is_not_suppressed_by_fractional_fragment_budget(tmp_path,monkeypatch,elapsed,query):
    from buwubot.memory_recall import MemoryRecall

    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    p=proposal().model_copy(update={'tier':'recent','significance':.5,'memory_schema_version':2,
        'content':'甲的服务端口为8123，未开代理，只允许本机访问。','event_time':100})
    original=await coordinator.commit(lambda:memory.commit(p))
    outline=MemoryProposal(group_id='123',subject_id='22',tier='long_term',significance=.8,memory_schema_version=2,
        content='甲的服务只供本机访问。',source_message_ids=['123:id:1'],snapshot_epoch=memory.epoch(),
        distilled_from=[{'memory_id':original,'expected_revision':1}])
    summary=await coordinator.commit(lambda:memory.commit(outline,reviewed=True))
    reader=MemoryRecall(memory,now=100+elapsed)
    result=reader.project(memory.get(summary),query=query)
    assert result['recall_fragments'][0]['memory_id']==original
    assert result['recall_fragments'][0]['content']==p.content
    assert not reader.project(memory.get(summary),query=query,fragment_chars=1).get('recall_fragments')
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()


@pytest.mark.parametrize('query,expected',[
    ('服务端口是多少','service'),('服务本机访问限制','service'),
    ('茶还是咖啡','drink'),('喝茶偏好','drink'),
    ('游戏安装进度','game'),('游戏下载完成了吗','game')])
async def test_fixed_recall_scenarios_keep_other_topics_out_even_when_they_are_important(tmp_path,monkeypatch,query,expected):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    ids={}
    for key,content in [('service','甲的服务端口为8123，只有本机能访问，不允许公网访问。'),
                        ('drink','甲喝茶，不喝咖啡。'),('game','甲已经完成游戏下载和安装。')]:
        p=proposal().model_copy(update={'tier':'long_term','significance':.95,'memory_schema_version':2,
            'content':content,'event_time':100})
        ids[key]=await coordinator.commit(lambda p=p:memory.commit(p,reviewed=True))
    before=[tuple(row) for row in store.rows('SELECT * FROM memory_signals ORDER BY memory_id')]
    results=await memory.retrieve(query,subjects=['22'])
    assert {item['id'] for item in results}=={ids[expected]}
    assert [tuple(row) for row in store.rows('SELECT * FROM memory_signals ORDER BY memory_id')]==before
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()


async def test_old_meaningful_record_uses_verified_outline_but_explicit_detail_recovers_original(tmp_path,monkeypatch):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    before=[tuple(row) for row in store.rows('SELECT * FROM memory_signals ORDER BY memory_id')]
    generic=await memory.retrieve('服务的访问范围',subjects=['22'])
    view=next(item for item in generic if item['id']==original)
    assert view['content']==memory.get(summary)['content']
    assert view['recall_representation']['mode']=='verified_outline'
    assert view['recall_representation']['outline_id']==summary
    assert '8123' not in json.dumps(view,ensure_ascii=False)
    assert all(port not in json.dumps(generic,ensure_ascii=False) for port in ['8123','8124'])
    detailed=await memory.retrieve('原话和全部细节，端口8123，当时有没有开代理',subjects=['22'])
    view=next(item for item in detailed if item['id']==original)
    assert view['content']==memory.get(original)['content']
    assert view['recall_representation']['mode']=='stored_detail'
    assert '未开启代理' in view['content'] and '只有本机' in view['content']
    assert [tuple(row) for row in store.rows('SELECT * FROM memory_signals ORDER BY memory_id')]==before
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()


async def test_model_can_request_stored_detail_without_promoting_inference_to_fact(tmp_path,monkeypatch):
    store,memory,_,original,_=await remembered(tmp_path,monkeypatch)
    packets=[]
    class Models:
        async def call(self,role,messages,**kwargs):
            data=json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('','helper',{'terms':['服务']})
            packets.append(data)
            return Completion('','helper',{'ids':[original],'detail_ids':[original,'not-visible-id']})
    memory.models=Models()
    selected=await memory.retrieve('服务情况',subjects=['22'])
    assert packets[0]['candidates'][0].get('recall_signals') is not None
    assert selected[0]['content']==memory.get(original)['content']
    assert selected[0]['recall_representation']['evidence_kind']=='stored_fact'
    assert len(store.rows('SELECT * FROM memories'))==3
    store.close()


async def test_changed_outline_and_recalled_source_are_not_exposed_after_model_wait(tmp_path,monkeypatch):
    store,memory,coordinator,original,summary=await remembered(tmp_path,monkeypatch)
    class Models:
        async def call(self,role,messages,**kwargs):
            data=json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('','helper',{'terms':['服务']})
            await coordinator.commit(lambda:memory.edit(summary,content='人工已修订要点',expected_revision=1))
            return Completion('','helper',{'ids':[original]})
    memory.models=Models()
    result=await memory.retrieve('服务情况',subjects=['22'])
    assert result[0]['content']==memory.get(original)['content']
    assert result[0]['recall_representation']['mode']=='stored_record'
    store.connection.execute('UPDATE messages SET recalled=1')
    memory.models=None
    assert await memory.retrieve('服务',subjects=['22'])==[]
    store.close()


async def test_direct_cue_recalls_old_fact_instead_of_recent_unrelated_person_fact(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    old=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={
        'tier':'long_term','significance':.8,'memory_schema_version':2,'content':'甲的仓库地址在西湖区。'}),reviewed=True))
    now=100+365*DAY
    monkeypatch.setattr('buwubot.memory.time.time',lambda:now)
    await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={
        'tier':'recent','significance':.95,'memory_schema_version':2,'content':'甲刚买了三明治。'})))
    before=[tuple(row) for row in store.rows('SELECT * FROM memory_signals ORDER BY memory_id')]
    result=await memory.retrieve('仓库地址',subjects=['22'])
    assert [item['id'] for item in result]==[old]
    signals=result[0]['recall_signals']
    assert signals['triggered'] and signals['recency']<.5 and signals['cue']>.5
    assert result[0]['confidence']==.9
    assert [tuple(row) for row in store.rows('SELECT * FROM memory_signals ORDER BY memory_id')]==before
    store.close()


async def test_reviewed_correction_adds_competition_without_erasing_historical_fact(tmp_path,monkeypatch):
    from buwubot.consolidation import MemoryConsolidation

    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    ids=[]
    for content in ['甲以前喝茶。','甲现在不再喝茶，改喝咖啡。']:
        p=proposal().model_copy(update={'tier':'long_term','significance':.7,'memory_schema_version':2,
                                       'content':content,'tags':['饮食']})
        ids.append(await coordinator.commit(lambda p=p:memory.commit(p,reviewed=True)))
    before={item['id']:item for item in await memory.retrieve('喝茶',subjects=['22'])}
    service=MemoryConsolidation(memory)
    snapshot=service.snapshot(mutation_since=0)
    await coordinator.commit(lambda:service.commit(snapshot,{'relations':[{
        'source_id':ids[0],'target_id':ids[1],'kind':'correction','summary':'后来改变饮品，旧状态保留为历史'}]},reviewed=True))
    after={item['id']:item for item in await memory.retrieve('喝茶',subjects=['22'])}
    assert set(after)==set(ids)
    assert after[ids[0]]['recall_signals']['interference']==.5
    assert after[ids[0]]['recall_signals']['accessibility']<before[ids[0]]['recall_signals']['accessibility']
    assert after[ids[1]]['content']=='甲现在不再喝茶，改喝咖啡。'
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()


async def test_v2_recall_has_no_final_twelve_record_cap(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    for number in range(16):
        p=proposal().model_copy(update={'tier':'long_term','significance':.5,'memory_schema_version':2,
                                       'content':f'项目参数{number}需要保留。'})
        await coordinator.commit(lambda p=p:memory.commit(p,reviewed=True))
    result=await memory.retrieve('项目参数',subjects=['22'])
    assert len(result)==16
    store.close()


async def test_fresh_source_fragments_keep_whole_conditions_and_do_not_modify_storage(tmp_path,monkeypatch):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    canonical=[memory.get(row['id'])['content'] for row in store.rows("SELECT id FROM memories WHERE tier='recent'")]
    result=await memory.retrieve('服务',subjects=['22'])
    view=next(item for item in result if item['id']==summary)
    assert view['recall_representation']['fragment_budget']==2
    assert {unit['content'] for unit in view['recall_fragments']}==set(canonical)
    assert all(unit['evidence_kind']=='stored_fact' for unit in view['recall_fragments'])
    assert '未开启代理，只有本机能访问' in memory.get(original)['content']
    store.close()


@pytest.mark.parametrize('stage',['expansion','selection'])
async def test_v2_retrieval_rechecks_live_conversation_permission_after_await(tmp_path,monkeypatch,stage):
    store,memory,_,_,_=await remembered(tmp_path,monkeypatch)
    class Models:
        async def call(self,role,messages,**kwargs):
            packet=json.loads(messages[-1]['content'])
            selecting='candidates' in packet
            if selecting==(stage=='selection'):
                cfg,revision=store.settings()
                store.save_settings(cfg.model_copy(update={'group_id':'999'}),revision)
            return Completion('','helper',{'ids':[item['id'] for item in packet.get('candidates',[])],
                                           'terms':['服务']})
    memory.models=Models()
    assert await memory.retrieve('服务')==[]
    store.close()


async def test_v2_vector_fallback_rejects_stale_foreign_and_other_persona_hits(tmp_path,monkeypatch):
    store,memory,_,original,_=await remembered(tmp_path,monkeypatch)
    class Index:
        fail=False
        async def search(self,query,scope):
            if self.fail:raise RuntimeError('test encoder unavailable')
            return [{'app_id':original,'revision':0,'similarity_score':1},
                    {'app_id':'missing','revision':1,'similarity_score':1}]
    memory.index=Index()
    assert await memory.retrieve('无关键词',subjects=['22'])==[]
    memory.index.fail=True
    assert original in {item['id'] for item in await memory.retrieve('服务',subjects=['22'])}
    memory.index.fail=False
    async def search(query,scope):
        return [{'app_id':original,'revision':1,'similarity_score':.99}]
    memory.index.search=search
    for column,value in [('group_id','999'),('persona_id','another')]:
        store.connection.execute(f'UPDATE memories SET {column}=? WHERE id=?',(value,original))
        assert await memory.retrieve('无关键词')==[]
        store.connection.execute(f'UPDATE memories SET {column}=? WHERE id=?',
                                 ('123' if column=='group_id' else None,original))
    store.close()


async def test_v2_duplicate_text_vector_and_entity_hits_preserve_one_candidate_and_all_cues(tmp_path,monkeypatch):
    from buwubot.memory_graph import MemoryGraph
    store,memory,_,original,_=await remembered(tmp_path,monkeypatch)
    class Index:
        async def search(self,query,scope):
            return [{'app_id':original,'revision':1,'similarity_score':.99}]
    memory.index=Index()
    monkeypatch.setattr(MemoryGraph,'recall',lambda self,query:[memory.get(original)|{'entity_recall':{'mode':'search'}}])
    found=await memory.retrieve('服务未知线索',subjects=['22'])
    matching=[item for item in found if item['id']==original]
    assert len(matching)==1 and matching[0]['entity_recall']=={'mode':'search'}
    assert matching[0]['recall_signals']['cue']==.99
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()


async def test_v2_compound_chinese_query_recalls_after_restart_without_helper_or_speaker_hint(tmp_path,monkeypatch):
    from buwubot.memory import MemoryService
    store,memory,_,original,_=await remembered(tmp_path,monkeypatch)
    restored=MemoryService(store,memory.settings)
    found=await restored.retrieve('服务本机端口8123访问限制')
    assert original in {item['id'] for item in found}
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()


async def test_v2_speaker_hint_does_not_hide_older_lexical_hit_behind_recent_filler(tmp_path,monkeypatch):
    store,memory,coordinator,original,_=await remembered(tmp_path,monkeypatch)
    for index in range(25):
        p=proposal().model_copy(update={'tier':'recent','significance':.5,'memory_schema_version':2,
                                       'content':f'甲今天买了三明治{index}。'})
        await coordinator.commit(lambda p=p:memory.commit(p))
    found=await memory.retrieve('服务本机端口8123访问限制',subjects=['22'])
    assert original in {item['id'] for item in found}
    store.close()
