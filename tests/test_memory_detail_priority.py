import pytest
from test_memory_v2_recall import remembered

from buwubot.memory_recall import MemoryRecall


@pytest.mark.parametrize('detail',[.49,.51])
async def test_nearby_detail_levels_keep_gist_and_relevant_units(tmp_path,monkeypatch,detail):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    actual=memory.with_reading_policy
    def view(item,**kwargs):
        result=actual(item,**kwargs)
        return result|{'detail_integrity':detail} if item['tier']=='recent' else result
    monkeypatch.setattr(memory,'with_reading_policy',view)
    result=MemoryRecall(memory,now=100+60*86400).project(memory.get(original),query='服务')
    assert result['content']==memory.get(summary)['content']
    assert result['recall_representation']['mode']=='verified_outline'
    assert original in {part['memory_id'] for part in result['recall_fragments']}
    assert result['recall_representation']['provided_fragments']==2
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()


async def test_detail_allowance_uses_each_source_not_the_outline_or_average(tmp_path,monkeypatch):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    actual=memory.with_reading_policy
    def view(item,**kwargs):
        result=actual(item,**kwargs)
        return result|{'detail_integrity':.00001 if item['id']==original else .9}
    monkeypatch.setattr(memory,'with_reading_policy',view)
    result=MemoryRecall(memory,now=100+60*86400).project(memory.get(summary),query='服务')
    assert original not in {part['memory_id'] for part in result.get('recall_fragments',[])}
    assert result['recall_representation']['provided_fragments']==1
    detailed=MemoryRecall(memory,now=100+60*86400).project(memory.get(summary),query='原话和全部细节')
    assert original in {part['memory_id'] for part in detailed['recall_fragments']}
    store.close()


async def test_rare_joint_cues_outrank_equally_many_common_matches(tmp_path,monkeypatch):
    store,memory,_,_,_=await remembered(tmp_path,monkeypatch)
    common=[{'id':f'common-{i}','content':'service normal','tier':'long_term','relations':[],
             'event_time':100,'meaning_strength':.5} for i in range(200)]
    distinctive={'id':'rare','content':'8123 localhost','tier':'long_term','relations':[],
                 'event_time':100,'meaning_strength':.5}
    unrelated={'id':'unrelated','content':'tea coffee','tier':'long_term','relations':[],
               'event_time':100,'meaning_strength':.99}
    results=MemoryRecall(memory,now=100).annotate(common+[distinctive,unrelated],
        query='service normal 8123 localhost',lookup='service normal 8123 localhost',vector_hits={})
    by_id={item['id']:item for item in results}
    assert by_id['rare']['recall_signals']['cue']>by_id['common-0']['recall_signals']['cue']
    assert results[0]['id']=='rare'
    assert by_id['unrelated']['recall_signals']['cue']==0
    store.close()


async def test_whole_unit_selection_is_monotone_over_detail_range_and_keeps_cap(tmp_path,monkeypatch):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    actual=memory.with_reading_policy
    level=[0.]
    def view(item,**kwargs):
        result=actual(item,**kwargs)
        return result|{'detail_integrity':level[0]} if item['tier']=='recent' else result
    monkeypatch.setattr(memory,'with_reading_policy',view)
    previous=set()
    for detail in [0.,.001,.01,.05,.1,.2,.49,.5,.51,.8,.99,1.]:
        level[0]=detail
        result=MemoryRecall(memory,now=100+60*86400).project(memory.get(original),query='服务',fragment_chars=100)
        parts=result.get('recall_fragments',[])
        selected={part['memory_id'] for part in parts}
        assert previous<=selected
        assert sum(len(part['content']) for part in parts)<=100
        assert result['content']==memory.get(summary)['content']
        previous=selected
    assert not store.rows('SELECT * FROM memory_signal_events')
    store.close()
