import json

import pytest
from test_memory import proposal, setup_memory

from buwubot.memory import MemoryProposal, MemoryService


@pytest.mark.parametrize('scores',[{'happy':float('nan')},{'angry':True},{'unknown':.5},{'sad':1.1}])
def test_invalid_emotion_scores_are_rejected(scores):
    with pytest.raises(ValueError):
        MemoryProposal(**(proposal().model_dump()|{'feeling_emotions':scores}))


def test_valid_emotion_scores_are_supported_without_sum_normalization():
    p=MemoryProposal(**(proposal().model_dump()|{'feeling_emotions':{'happy':.7,'sad':.6},'feeling_arousal':.1}))
    assert p.feeling_emotions=={'happy':.7,'sad':.6} and p.feeling_arousal==.1


def test_background_serialization_preserves_omitted_arousal_and_distinct_clear_key(tmp_path):
    store,memory,_=setup_memory(tmp_path)
    omitted=proposal()
    restored=MemoryProposal.model_validate_json(omitted.model_dump_json())
    assert 'feeling_arousal' not in restored.model_fields_set
    explicit=MemoryProposal(**(omitted.model_dump()|{'feeling_arousal':None}))
    assert 'feeling_arousal' in MemoryProposal.model_validate(explicit.model_dump()).model_fields_set
    assert memory.proposal_key(explicit)!=memory.proposal_key(omitted)
    store.close()


async def test_mixed_components_and_independent_arousal_decay_without_extra_stimuli(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    await coordinator.commit(lambda:memory.mood.adjust(valence=0,intensity=.8,operation_id='one',now=100,
        emotions={'happy':.7,'sad':.6},arousal=.1))
    view=memory.mood.view(now=100)
    assert view['emotions']=={'happy':.7,'sad':.6} and view['arousal']==pytest.approx(.1)
    assert view['intensity']==pytest.approx(.8)
    later=memory.mood.view(now=3700)
    assert later['emotions']['happy']==pytest.approx(.35) and later['arousal']==pytest.approx(.05)
    assert len(store.rows('SELECT * FROM mood_events'))==1
    assert MemoryService(store,memory.settings).mood.view(now=3700)==later
    await coordinator.commit(lambda:memory.mood.reconfigure(120,now=3700))
    assert memory.mood.view(now=3700)['emotions']==later['emotions']
    assert memory.mood.view(now=10900)['emotions']['sad']==pytest.approx(.15)
    store.close()


async def test_old_direction_is_not_reclassified_into_a_specific_emotion(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    await coordinator.commit(lambda:memory.mood.adjust(valence=-.8,intensity=.8,operation_id='old',now=100))
    view=memory.mood.view(now=100)
    assert view['emotions']=={} and view['arousal'] is None
    assert memory.mood.view(now=100)['intensity']>.7
    store.close()


@pytest.mark.parametrize('count,strength',[(10,.1),(50,.05)])
async def test_many_weak_events_do_not_make_current_emotions_strong(tmp_path,count,strength):
    store,memory,coordinator=setup_memory(tmp_path)
    for index in range(count):
        await coordinator.commit(lambda index=index:memory.mood.adjust(valence=1.,intensity=strength,
            emotions={'happy':strength},arousal=strength,operation_id=f'weak:{index}',now=100))
    before=[tuple(row) for row in store.rows('SELECT * FROM mood_events')]
    current=memory.mood.view(now=100)
    assert strength<=current['intensity']<=.25
    assert current['emotions']['happy']<=.25 and current['arousal']<=.25
    values=[memory.mood.view(now=100+offset)['intensity'] for offset in (0,1800,3600,21600,86400)]
    assert values==sorted(values,reverse=True) and values[-1]==0
    assert [tuple(row) for row in store.rows('SELECT * FROM mood_events')]==before
    await coordinator.commit(lambda:memory.mood.adjust(valence=-1.,intensity=.8,
        emotions={'sad':.8},arousal=.7,operation_id='strong',now=100))
    combined=memory.mood.view(now=100)
    assert combined['intensity']>=.8 and combined['emotions']['sad']==pytest.approx(.8)
    assert combined['positive']>0 and combined['negative']>=.8
    store.close()


async def test_canonical_feeling_profile_survives_text_edit_recall_and_source_revocation(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    p=proposal().model_copy(update={'tier':'feeling','subject_id':'','persona_id':memory.settings.persona_id,
        'memory_schema_version':2,'significance':.5,'feeling_strength':.8,'feeling_valence':0.,
        'feeling_emotions':{'happy':.7,'sad':.6},'feeling_arousal':.1,'content':'开心又有点舍不得。'})
    mid=await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    before=memory.with_reading_policy(memory.get(mid),now=1900)
    assert before['feeling_emotions']['happy']>before['feeling_emotions']['sad']>0
    assert before['feeling_arousal']<before['feeling_level']
    monkeypatch.setattr('buwubot.memory.time.time',lambda:1900)
    await coordinator.commit(lambda:memory.edit(mid,content='高兴，也有些不舍。',expected_revision=1))
    after=memory.with_reading_policy(memory.get(mid),now=1900)
    assert after['feeling_emotions']==before['feeling_emotions'] and after['feeling_arousal']==before['feeling_arousal']
    assert len(store.rows('SELECT * FROM mood_events'))==1
    await coordinator.commit(lambda:memory.recall_feeling(mid,operation_id='recall',expected_revision=2,now=1900))
    recalled=memory.with_reading_policy(memory.get(mid),now=1900)
    assert recalled['feeling_emotions']['happy']>before['feeling_emotions']['happy']
    assert len(store.rows('SELECT * FROM mood_events'))==2
    assert memory.mood.view(now=1900)['emotions']['sad']>0
    assert 'reason' not in json.dumps(memory.mood.view(now=1900))
    store.connection.execute('UPDATE messages SET recalled=1')
    assert memory.mood.view(now=1900)['emotions']=={}
    store.close()


def test_management_supports_profiles_with_validations_and_old_clients(tmp_path):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        cfg=client.get('/api/config').json()
        cfg=client.patch('/api/config',json={'revision':cfg['revision'],'changes':{'group_id':'123','runtime_enabled':False}}).json()
        client.app.state.store.set_state('memory_schema_version',2)
        body={'revision':cfg['revision'],'operation_id':'mixed','valence':0.,'intensity':.8,
              'emotions':{'happy':.7,'sad':.6},'arousal':.1}
        response=client.post('/api/mood',json=body)
        assert response.status_code==200,response.text
        assert response.json()['emotions']['sad']>.59 and response.json()['arousal']<.11
        for changes in [{'emotions':{'wrong':.3}},{'emotions':{'happy':True}},
                        {'emotions':{'happy':.9}},{'arousal':2}]:
            assert client.post('/api/mood',json=body|changes).status_code==422
        record=client.post('/api/memories',json={'config_revision':cfg['revision'],'tier':'feeling',
            'content':'高兴又不舍。','feeling_strength':.8,'feeling_valence':0.,'significance':.5,
            'feeling_emotions':{'happy':.7,'sad':.6},'feeling_arousal':.1})
        assert record.status_code==200,record.text
        data=client.get('/api/memories?tier=feeling').json()['items'][0]
        assert data['feeling_emotions']['sad']>.59 and data['feeling_arousal']<.11
        cleared=client.patch('/api/memories/'+data['id'],json={'revision':data['revision'],
            'content':data['content'],'feeling_arousal':None})
        assert cleared.status_code==200,cleared.text
        assert client.get('/api/memories?tier=feeling').json()['items'][0]['feeling_arousal'] is None
        old=client.post('/api/mood',json={'revision':cfg['revision'],'operation_id':'old','valence':-.5,'intensity':.5})
        assert old.status_code==200 and old.json()['emotions']=={} and old.json()['arousal'] is None
