import json

from fastapi.testclient import TestClient

from buwubot import admin
from buwubot.app import create_app
from buwubot.memory import MemoryProposal, MemoryService
from buwubot.models import Completion


def prepare(client):
    current=client.get('/api/config').json()
    cfg=client.patch('/api/config',json={'revision':current['revision'],
        'changes':{'group_id':'123','self_id':'11','runtime_enabled':False}}).json()
    store=client.app.state.store
    settings,_=store.settings()
    memory=MemoryService(store,settings)
    source=memory.archive.accept({'time':100,'post_type':'message','message_type':'group',
        'group_id':123,'user_id':22,'self_id':11,'message_id':1,
        'sender':{'nickname':'甲'},'message':[{'type':'text','data':{'text':'我叫阿甲'}}]},settings)
    async def seed():
        return await client.app.state.coordinator.commit(lambda:memory.commit(MemoryProposal(
            group_id='123',subject_id='22',content='甲的昵称是阿甲',tier='secondary',source_message_ids=[source.id])))
    mid=client.portal.call(seed)
    return {'config_revision':cfg['revision'],'persona_id':settings.persona_id,'changes':[
        {'memory_id':mid,'expected_revision':1,'content':'甲的昵称是阿甲','tier':'important','tags':['称呼']}]},mid


def test_management_preview_review_and_explicit_apply_preserve_pause(monkeypatch,tmp_path):
    calls=[]
    class Models:
        def __init__(self,*args,**kwargs):pass
        async def call(self,role,messages,**kwargs):
            calls.append(role)
            items=json.loads(messages[-1]['content'])['changes']
            return Completion('','independent-helper',{'checks':[
                {'memory_id':item['before']['id'],'approved':True,'reason':'昵称属于稳定称呼'} for item in items]})
        async def close(self):pass
    monkeypatch.setattr(admin,'ModelClient',Models,raising=False)
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        plan,mid=prepare(client)
        preview=client.post('/api/memory-maintenance/preview',json=plan)
        assert preview.status_code==200
        token=preview.json()['token']
        assert not calls and client.app.state.memory.get(mid)['revision']==1
        assert client.post('/api/memory-maintenance/apply',json={'token':token,'confirm':True}).status_code==409
        review=client.post('/api/memory-maintenance/review',json=plan)
        assert review.status_code==200 and review.json()['status']=='ready'
        assert calls==['review']
        assert client.get('/api/memory-maintenance/'+token).json()['status']=='ready'
        assert client.post('/api/memory-maintenance/apply',json={'token':token,'confirm':False}).status_code==422
        applied=client.post('/api/memory-maintenance/apply',json={'token':token,'confirm':True})
        assert applied.status_code==200
        assert client.post('/api/memory-maintenance/apply',json={'token':token,'confirm':True}).json()==applied.json()
        memory=client.app.state.memory
        assert memory.get(mid)['tier']=='important' and memory.get(mid)['revision']==2
        assert not memory.lifecycle.state(memory.get(mid))['protected']
        assert not client.get('/api/config').json()['settings']['runtime_enabled']
        assert client.post('/api/memory-maintenance/review',json=plan,
            headers={'Origin':'https://evil.invalid'}).status_code==403
        assert client.post('/api/memory-maintenance/preview',json=plan|{'reviewed':True}).status_code==422


def test_maintenance_preview_is_refused_while_runtime_enabled(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        plan,_=prepare(client)
        cfg=client.get('/api/config').json()
        enabled=client.patch('/api/config',json={'revision':cfg['revision'],
            'changes':{'runtime_enabled':True}}).json()
        plan['config_revision']=enabled['revision']
        assert client.post('/api/memory-maintenance/preview',json=plan).status_code==409
