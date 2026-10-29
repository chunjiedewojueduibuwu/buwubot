import copy

from buwubot.conversations import message_context


def test_message_material_exposes_actual_mentions_without_rewriting_speaker_or_quote():
    raw={'id':'g:1','self_id':'11','user_id':'22','time':100,
         'text':'这句是对他说的','segments':[{'type':'at','data':{'qq':'33'}},
             {'type':'at','data':{'qq':'33'}},{'type':'at','data':{'qq':'all'}},
             {'type':'reply','data':{'id':'older'}}]}
    before=copy.deepcopy(raw)
    material=message_context(raw)
    assert material['addressing']=={'mentioned_user_ids':['33'],'mentions_self':False,
                                    'mentions_other_users':True,'mentions_everyone':True}
    assert material['user_id']=='22' and material['segments']==raw['segments']
    assert raw==before and 'addressing' not in raw


def test_mentions_self_does_not_change_who_said_i():
    material=message_context({'self_id':'11','user_id':'22','segments':[
        {'type':'at','data':{'qq':11}},{'type':'at','data':{'qq':'33'}}]})
    assert material['addressing']['mentions_self'] is True
    assert material['addressing']['mentioned_user_ids']==['11','33']
    assert material['user_id']=='22'
