import math

import pytest
from test_memory import proposal, setup_memory

from buwubot.config import FeelingPolicy
from buwubot.emotion import feeling_view
from buwubot.memory_dynamics import DAY, MemoryAnchor
from buwubot.mood import Mood


def test_v2_feeling_has_one_residue_and_forgetting_starts_after_it_calms():
    policy=FeelingPolicy(half_life_minutes=30)
    anchor=MemoryAnchor(significance=.5,emotion=.8,valence=0,since=100)
    initial=feeling_view(anchor,policy,now=100)
    active=feeling_view(anchor,policy,now=100+1800)
    assert initial['emotion']==.8 and active['emotion']>0 and active['remaining']==1
    assert active['valence']==0  # Neutral/mixed is still a strong feeling.
    later=feeling_view(anchor,policy,now=100+36500*DAY)
    assert later['emotion']==0 and later['remaining']==0
    strong=feeling_view(MemoryAnchor(significance=.9,emotion=.9,since=100),policy,now=100+100*DAY)
    weak=feeling_view(MemoryAnchor(significance=.1,emotion=.1,since=100),policy,now=100+100*DAY)
    assert strong['remaining']>weak['remaining']
    assert feeling_view(anchor,policy,now=-100)==initial
    with pytest.raises(ValueError):feeling_view(anchor,policy,now=math.nan)


def test_feeling_relaxation_is_not_clamped_to_day_scale_retention():
    anchor=MemoryAnchor(significance=.5,emotion=.8,since=100)
    levels=[feeling_view(anchor,FeelingPolicy(half_life_minutes=value),now=1900)['emotion'] for value in [1,30,60]]
    assert levels[0]<levels[1]<levels[2]
    expected=.8*2**(-30/(30*(1+8*.5*.8)))
    assert levels[1]==pytest.approx(expected)


@pytest.mark.parametrize('sets',[[[1,2],[1]],[[1],[1,2]]])
async def test_overlapping_feeling_sources_stimulate_only_new_canonical_events(tmp_path,monkeypatch,sets):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    memory.archive.accept({'time':101,'post_type':'message','message_type':'group','group_id':123,'self_id':11,
        'user_id':22,'message_id':2,'sender':{'nickname':'甲'},'message':[{'type':'text','data':{'text':'新的互动'}}]},memory.settings)
    for index,sources in enumerate(sets):
        p=proposal().model_copy(update={'tier':'feeling','subject_id':'','persona_id':memory.settings.persona_id,
            'memory_schema_version':2,'significance':.5,'feeling_strength':.8,'feeling_valence':0.,
            'content':f'互动感受{index}','source_message_ids':[f'123:id:{number}' for number in sources]})
        await coordinator.commit(lambda p=p:memory.commit(p,reviewed=True))
    assert len(store.rows('SELECT * FROM mood_events'))==(1 if sets[0]==[1,2] else 2)
    store.close()


async def test_mood_is_persistent_idempotent_mixed_and_read_only(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    mood=Mood(memory)
    await coordinator.commit(lambda:mood.adjust(valence=1,intensity=.8,operation_id='one',now=100))
    await coordinator.commit(lambda:mood.adjust(valence=-1,intensity=.8,operation_id='two',now=100))
    before=store.connection.total_changes
    view=mood.view(now=100)
    assert view['valence']==pytest.approx(0) and view['intensity']>.8
    assert view['positive']>0 and view['negative']>0
    assert Mood(memory).view(now=100)==view
    assert store.connection.total_changes==before
    await coordinator.commit(lambda:mood.adjust(valence=-1,intensity=.8,operation_id='two',now=100))
    assert mood.view(now=100)==view
    assert mood.view(now=100+3600)['intensity']<view['intensity']
    with pytest.raises(RuntimeError):mood.adjust(valence=0,intensity=0,operation_id='bad',now=100)
    await coordinator.commit(lambda:mood.reset(now=200))
    assert mood.view(now=200)['intensity']==0
    store.close()


async def test_v2_feeling_commit_recall_and_edit_drive_mood_only_for_real_events(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    p=proposal().model_copy(update={'tier':'feeling','subject_id':'','persona_id':memory.settings.persona_id,
        'memory_schema_version':2,'significance':.6,'feeling_strength':.8,'feeling_valence':-.7,
        'content':'这次聊天让我有点失落。'})
    mid=await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    mood=Mood(memory)
    initial=mood.view(now=100)
    assert initial['valence']<0 and initial['intensity']>0
    await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    assert mood.view(now=100)==initial
    view=memory.with_reading_policy(memory.get(mid),now=100)
    assert view['emotional_residue']==view['feeling_level']==.8 and view['feeling_valence']==-.7
    await coordinator.commit(lambda:memory.edit(mid,content='聊天时我有些失落。',expected_revision=1))
    assert mood.view(now=100)==initial  # Editing history is not another stimulus.
    await coordinator.commit(lambda:memory.recall_feeling(mid,operation_id='recall',expected_revision=2,now=200))
    recalled=mood.view(now=200)
    assert recalled['intensity']>initial['intensity']
    assert mood.changes(now=200)[0]['memory_id']==mid
    assert mood.changes(now=200)[0]['reason']=='聊天时我有些失落。'
    await coordinator.commit(lambda:memory.recall_feeling(mid,operation_id='recall',expected_revision=2,now=200))
    assert mood.view(now=200)==recalled
    store.connection.execute('UPDATE messages SET recalled=1')
    assert mood.view(now=200)['intensity']==0
    assert mood.changes(now=200)==[]
    store.close()


async def test_migration_preserves_old_feeling_and_does_not_invent_present_mood(tmp_path,monkeypatch):
    from buwubot.memory_migration import MemoryMigration
    store,memory,coordinator=setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    p=proposal().model_copy(update={'tier':'feeling','subject_id':'','persona_id':memory.settings.persona_id,
        'feeling_strength':.8,'content':'那次聊天很开心。','event_time':100})
    mid=await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    before=memory.with_reading_policy(memory.get(mid),now=1900)
    cfg,revision=store.settings()
    store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
    migration=MemoryMigration(memory)
    preview=await coordinator.commit(lambda:migration.preview(now=1900))
    await coordinator.commit(lambda:migration.apply(preview['token']))
    after=memory.with_reading_policy(memory.get(mid),now=1900)
    assert after['dynamics_version']==2
    assert after['feeling_level']==before['feeling_level'] and after['memory_remaining']==before['memory_remaining']
    assert after['feeling_valence']==0  # Direction was never recorded: migration cannot invent it.
    assert memory.mood.view(now=1900)['intensity']==0
    assert after['content']==before['content'] and after['revision']==before['revision']
    store.close()


async def test_feeling_policy_change_keeps_current_levels_and_restart_continuity(tmp_path,monkeypatch):
    from buwubot.memory import MemoryService
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    p=proposal().model_copy(update={'tier':'feeling','subject_id':'','persona_id':memory.settings.persona_id,
        'memory_schema_version':2,'significance':.5,'feeling_strength':.8,'feeling_valence':1.,'content':'很高兴。'})
    mid=await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    now=100+30*DAY
    before=memory.with_reading_policy(memory.get(mid),now=now)
    policy=memory.settings.memory_policies.feeling.model_copy(update={'half_life_minutes':60})
    await coordinator.commit(lambda:memory.feelings.reconfigure(policy,now=now))
    after=MemoryService(store,memory.settings).with_reading_policy(memory.get(mid),now=now)
    for key in ['feeling_level','memory_remaining','detail_integrity','meaning_strength']:
        assert after[key]==pytest.approx(before[key])
    assert not store.rows('SELECT * FROM emotion_recalls')
    store.close()


async def test_mood_half_life_change_does_not_jump_and_private_reasons_are_not_projected(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    await coordinator.commit(lambda:memory.mood.adjust(valence=-.5,intensity=.8,operation_id='test',now=100))
    before=memory.mood.view(now=1900)
    await coordinator.commit(lambda:memory.mood.reconfigure(120,now=1900))
    memory.settings=memory.settings.model_copy(update={'mood_half_life_minutes':120})
    after=memory.mood.view(now=1900)
    for key in ['valence','intensity','positive','negative','updated_at']:
        assert after[key]==pytest.approx(before[key])
    assert set(after)=={'valence','intensity','positive','negative','updated_at','half_life_minutes','emotions','arousal'}
    assert memory.mood.view(now=1900+7200)['intensity']==pytest.approx(after['intensity']/2)
    store.close()


async def test_scoped_memory_copy_binds_feeling_and_mood_to_the_selected_persona(tmp_path):
    from copy import copy
    store,memory,coordinator=setup_memory(tmp_path)
    cfg,revision=store.settings()
    cfg=cfg.model_copy(update={'persona_id':'other'})
    store.save_settings(cfg,revision)
    scoped=copy(memory)
    scoped.settings=cfg
    await coordinator.commit(lambda:scoped.mood.adjust(valence=1,intensity=.6,operation_id='other',now=100))
    assert scoped.mood.view(now=100)['intensity']==pytest.approx(.6)
    assert memory.mood.view(now=100)['intensity']==0
    assert scoped.feelings.memory is scoped and scoped.mood.memory is scoped
    store.close()
