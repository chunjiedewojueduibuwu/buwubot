import pytest


@pytest.mark.parametrize('tail', ['希望这能帮到你。', '如果你还有其他问题，随时问我！', '有需要随时告诉我'])
def test_external_filter_removes_only_empty_standalone_footer(tail):
    from buwubot.chat_cliches import strip_cliche_footer

    assert strip_cliche_footer('先点保存。\n\n'+tail, source_texts=[])=='先点保存。'
    assert strip_cliche_footer('先点保存。'+tail, source_texts=[])=='先点保存。'


@pytest.mark.parametrize('text,sources', [
    ('希望这能帮到你。', []),
    ('文案：\n希望这能帮到你。', ['请原样写一句：希望这能帮到你。']),
    ('```python\nprint("希望这能帮到你。")\n```', []),
    ('他在信里写：“希望这能帮到你。”', []),
    ('https://example.com/希望这能帮到你', []),
    ('我想说希望这能帮到你，但没有把握', []),
    ('这根梁能撑得起两吨。', []),
    ('原句是“你这句话撑得起整段情绪”。', ['分析原句“你这句话撑得起整段情绪”']),
])
def test_filter_preserves_requested_quotes_code_urls_and_substantive_content(text,sources):
    from buwubot.chat_cliches import strip_cliche_footer

    assert strip_cliche_footer(text,source_texts=sources)==text


async def test_helper_uses_same_cliche_list_and_external_filter_even_if_no_issue_flag():
    from buwubot.chat_cliches import CLICHE_GUIDANCE
    from buwubot.engine import REPLY_PROMPT, TOOL_REPLY_PROMPT
    from buwubot.models import Completion
    from buwubot.scene_analysis import review_reply

    assert CLICHE_GUIDANCE in REPLY_PROMPT
    assert CLICHE_GUIDANCE in TOOL_REPLY_PROMPT
    class Models:
        async def call(self,role,messages,**kwargs):
            assert CLICHE_GUIDANCE in messages[0]['content']
            return Completion('','helper',{'texts':['无理由改写'], 'issues':[]})
    packet={'texts':['先点保存。\n希望这能帮到你。'],'targets':[{'text':'怎么保存'}]}
    result=await review_reply(Models(),packet)
    assert result.texts==['先点保存。'] and result.issues==['filler']


async def test_filler_only_rewrite_cannot_add_fabricated_experience():
    from buwubot.models import Completion
    from buwubot.scene_analysis import review_reply

    class Models:
        async def call(self,*args,**kwargs):
            return Completion('','helper',{'texts':['战斗节奏慢，我玩着犯困'], 'issues':['filler']})
    packet={'texts':['这是一个很好的问题。战斗节奏慢。\n希望这能帮到你。'],
            'targets':[{'text':'你怎么看这段评论'}]}
    result=await review_reply(Models(),packet)
    assert result.texts==['战斗节奏慢。']
