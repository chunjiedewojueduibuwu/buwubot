import json

from test_reply_batches import setup_reply


async def test_reply_receives_growth_as_separate_scoped_material(tmp_path):
    store,engine,_,jid,models=setup_reply(tmp_path)
    marker=[{'id':'growth1','kind':'interest','content':'助手喜欢研究轨道知识。','revision':1}]
    engine.memory.growth_context=lambda **kwargs:marker
    original=models.call
    seen=[]
    async def call(role,payload,**kwargs):
        if role=='chat':
            blocks=payload[-1]['content']
            data=json.loads(blocks[0]['text'])
            seen.append(data['persona_growth'])
        return await original(role,payload,**kwargs)
    models.call=call
    try:
        await engine.process(engine.queue.get(jid))
        assert seen==[marker]
        assert not store.rows("SELECT id FROM memories WHERE tier='core'")
    finally:store.close()
