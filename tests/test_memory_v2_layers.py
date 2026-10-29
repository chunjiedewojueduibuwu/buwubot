import hashlib
import json

import pytest
from pydantic import ValidationError
from test_memory import proposal, setup_memory

from buwubot.config import MemoryPolicies
from buwubot.memory import MemoryProposal, ReviewRequired
from buwubot.store import Conflict

DAY=86400


def test_active_policy_projection_has_five_layers_and_rejects_invalid_dynamics():
    policies=MemoryPolicies()
    assert set(policies.for_schema(2))=={'core','context','recent','long_term','feeling'}
    assert set(policies.for_schema(1))=={'core','context','recent','secondary','important','feeling'}
    assert policies.long_term.half_life_minutes==180*1440
    for dynamics in [{'meaning_influence':float('nan')},{'unknown_knob':1},
                     {'emotion_detail_protection':1},{'base_half_life':1},{'meaning_influence':True}]:
        with pytest.raises(ValidationError):
            MemoryPolicies.model_validate({'long_term':{'dynamics':dynamics}})
    for value in [True,float('nan'),'high']:
        with pytest.raises(ValidationError):
            MemoryProposal.model_validate(proposal().model_dump()|{'significance':value})


async def test_v2_long_term_significance_changes_retention_but_not_confidence(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    ids=[]
    for significance in [.2,.9]:
        p=proposal().model_copy(update={'tier':'long_term','significance':significance,
            'memory_schema_version':2,'content':f'甲的稳定事实{significance}'})
        # model_copy is deliberate until the schema accepts this complete proposal.
        p=MemoryProposal.model_validate(p.model_dump())
        mid=await coordinator.commit(lambda p=p:memory.commit(p,reviewed=True))
        ids.append(mid)
    low,high=[memory.with_reading_policy(memory.get(mid),now=100+180*DAY) for mid in ids]
    assert low['memory_remaining']<.5<high['memory_remaining']
    assert all(view['confidence']==.9 and view['tier']=='long_term' and view['dynamics_version']==2
               for view in [low,high])
    assert high['memory_half_life_minutes']>low['memory_half_life_minutes']
    assert set(memory.policy_context())=={'core','context','recent','long_term','feeling'}
    store.close()


async def test_v2_new_ordinary_requires_significance_and_long_term_requires_review(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    p=proposal().model_copy(update={'tier':'long_term','memory_schema_version':2})
    with pytest.raises(ValueError,match='significance'):
        await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    p=p.model_copy(update={'significance':.8})
    with pytest.raises(ReviewRequired):
        await coordinator.commit(lambda:memory.commit(p))
    assert not store.rows('SELECT * FROM memories')
    store.close()


async def test_applied_legacy_receipt_survives_schema_switch_but_uncommitted_old_candidate_cannot_write(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    old=proposal()
    mid=await coordinator.commit(lambda:memory.commit(old))
    fields=old.model_dump()
    for field in ['significance','memory_schema_version','feeling_valence','feeling_emotions','feeling_arousal']:
        fields.pop(field,None)
    # Compare to the exact pre-V2 key, including its existing compatibility omissions.
    for key in ['maintenance_epoch','persona_id','reinforcement_source_ids','distilled_from',
                'source_group_id','teaching_scope','teaching_source_ids','feeling_strength']:
        fields.pop(key)
    legacy_key=hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    assert memory.proposal_key(old)==legacy_key
    store.set_state('memory_schema_version',2)
    assert memory.was_applied(old)
    assert await coordinator.commit(lambda:memory.commit(old))==mid
    with pytest.raises(Conflict,match='schema'):
        await coordinator.commit(lambda:memory.commit(old.model_copy(update={'content':'另一个旧候选'})))
    assert len(store.rows('SELECT * FROM memories'))==1
    store.close()


def test_v2_prompt_has_one_long_term_layer_and_retains_evidence_boundaries():
    from buwubot.memory_prompts import MEMORY_ADOPTION_PROMPT, schema_prompt
    prompt=schema_prompt(MEMORY_ADOPTION_PROMPT,2)
    assert 'secondary' not in prompt and 'important' not in prompt and '两层只在' not in prompt
    for required in ['significance','confidence','引用','QQ','source_message_ids','不删除近期经过']:
        assert required in prompt
    assert schema_prompt(MEMORY_ADOPTION_PROMPT,1)==MEMORY_ADOPTION_PROMPT


async def test_schema_switch_replans_only_uncommitted_material_and_keeps_applied_facts(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    now=datetime(2026,10,28,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    memory=engine.memory
    source=memory.archive.accept({'post_type':'message','message_type':'group','self_id':11,
        'group_id':123,'user_id':22,'message_id':1,'time':now.timestamp(),
        'message':[{'type':'text','data':{'text':'我喜欢茶，也喜欢阅读'}}]},engine.settings)
    first=proposal()
    mid=await engine.coordinator.commit(lambda:memory.commit(first))
    second=proposal().model_copy(update={'content':'甲喜欢阅读'})
    store.set_state('memory_schema_version',2)
    jid=engine.queue.enqueue('memory',{'group_id':'123','messages':[vars(source)],
        'proposals':[first.model_dump(),second.model_dump()],'applied':[]},
        dedupe_key='schema-switch',priority=1,source_time=source.time)
    await engine.process(engine.queue.get(jid))
    saved=engine.queue.get(jid)
    assert saved['state']=='pending' and 'proposals' not in saved['payload']
    assert saved['payload']['review_feedback']['uncommitted_proposals']==[second.model_dump()]
    assert memory.get(mid)['content']==first.content and len(store.rows('SELECT * FROM memories'))==1
    assert not models.calls and not qq.sent
    store.close()


async def test_v2_preparation_and_adoption_use_new_contract_and_trusted_schema_version(tmp_path):
    from buwubot.models import Completion

    store,memory,_=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    prompts=[]
    class Models:
        async def call(self,role,messages,**kwargs):
            prompts.append(messages[0]['content'])
            return Completion('',memory.settings.primary_model,{'memories':[{
                'subject_id':'22','content':'甲长期喜欢喝茶','tier':'long_term',
                'significance':.8,'confidence':.9,'source_message_ids':['123:id:1'],
                'memory_schema_version':1}]})
    memory.models=Models()
    result=await memory.propose([memory.archive.get('123:id:1')])
    assert result[0].memory_schema_version==2 and result[0].significance==.8
    assert all('secondary' not in text and 'important' not in text for text in prompts)
    store.close()


async def test_meaning_reclassification_does_not_restore_details_or_count_as_recall(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    p=proposal().model_copy(update={'tier':'recent','significance':.5,'memory_schema_version':2})
    mid=await coordinator.commit(lambda:memory.commit(p))
    now=100+7*DAY
    before=memory.with_reading_policy(memory.get(mid),now=now)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:now)
    await coordinator.commit(lambda:memory.edit(mid,content=p.content,expected_revision=1,significance=.9))
    after=memory.with_reading_policy(memory.get(mid),now=now)
    assert after['detail_integrity']==pytest.approx(before['detail_integrity'])
    assert after['memory_remaining']==pytest.approx(before['memory_remaining'])
    assert after['memory_half_life_minutes']>before['memory_half_life_minutes']
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()
