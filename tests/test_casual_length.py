import pytest

from buwubot.models import Completion
from buwubot.scene_analysis import review_reply


async def test_casual_limit_is_per_message_not_total():
    packet={'reply_style':'casual','texts':['甲'*40,'乙'*40],'targets':[]}
    class Models:
        async def call(self,*args,**kwargs):return Completion('','helper',{'texts':['甲'*40,'乙'*40],'issues':[]})
    assert (await review_reply(Models(),packet)).texts==packet['texts']


async def test_each_message_must_fit_even_when_rewrite_keeps_multiple_bubbles():
    packet={'reply_style':'casual','texts':['甲'*60,'乙'*30]}
    class Models:
        async def call(self,*args,**kwargs):return Completion('','helper',{'texts':['甲'*51,'乙'*25],'issues':['casual_length']})
    with pytest.raises(ValueError,match='50'):await review_reply(Models(),packet)


async def test_shortening_can_rephrase_without_cutting_and_serious_help_is_exempt():
    class Models:
        async def call(self,*args,**kwargs):return Completion('','helper',{'texts':['感觉画面不错，不过我更在意好不好玩'],'issues':['casual_length']})
    result=await review_reply(Models(),{'reply_style':'casual','texts':['画面不错，'*20]})
    assert result.texts==['感觉画面不错，不过我更在意好不好玩']
    class Unchanged:
        async def call(self,*args,**kwargs):return Completion('','helper',{'texts':['不采用'],'issues':[]})
    for style in ['help','deliverable','auto']:
        text='认真说明'*30
        assert (await review_reply(Unchanged(),{'reply_style':style,'texts':[text]})).texts==[text]


async def test_long_casual_still_uses_durable_shortening_when_optional_scene_is_off(tmp_path):
    from test_reply_batches import setup_reply
    store,engine,_,jid,_=setup_reply(tmp_path)
    try:
        job=engine.queue.get(jid)
        job['payload'].update(reply_style='casual',persona_hash=engine.draft_fingerprint(),
                              segments=[{'type':'text','data':{'text':'闲聊内容'*20}}])
        engine.queue.payload(jid,job['payload'])
        assert engine.settings.scene_analysis_enabled is False
        assert engine.review_reply_ready(engine.queue.get(jid),engine.memory) is False
        scene=store.rows("SELECT id FROM jobs WHERE kind='scene'")
        assert len(scene)==1
        assert engine.queue.get(scene[0]['id'])['payload']['packet']['reply_style']=='casual'
    finally:store.close()


async def test_prior_messages_do_not_reduce_another_messages_limit(tmp_path):
    from test_reply_batches import setup_reply
    store,engine,_,jid,_=setup_reply(tmp_path)
    try:
        job=engine.queue.get(jid)
        job['payload'].update(reply_style='casual',persona_hash=engine.draft_fingerprint(),
            response_batches=[[{'type':'text','data':{'text':'甲'*40}}],[{'type':'text','data':{'text':'乙'*40}}]])
        engine.queue.payload(jid,job['payload'])
        assert engine.review_reply_ready(engine.queue.get(jid),engine.memory)
        assert not store.rows("SELECT id FROM jobs WHERE kind='scene'")
    finally:store.close()


async def test_multiple_text_segments_in_one_bubble_still_share_its_50_limit():
    packet={'reply_style':'casual','texts':['甲'*30,'乙'*30],'casual_bubble_groups':[[0,1]]}
    class Models:
        async def call(self,*args,**kwargs):return Completion('','helper',{'texts':packet['texts'],'issues':[]})
    with pytest.raises(ValueError,match='50'):await review_reply(Models(),packet)
