"""Shared expression examples and a deliberately narrow empty-footer filter."""

# Counterexample phrases adapted from fishtalk; see licenses/fishtalk-MIT.txt.

CLICHE_FOOTERS = (
    '希望这能帮到你', '希望对你有所帮助', '希望这些信息对你有帮助',
    '如果你还有其他问题，随时问我', '如有其他问题，随时问我',
    '有需要随时告诉我',
)
CLICHE_OPENINGS = ('这是一个很好的问题', '这个问题问得很好')

CLICHE_GUIDANCE = '''【具体套话核对】
以下表达不能作为自行附加的空泛填充：
“这是一个很好的问题”“你的感受我完全理解”“希望这能帮到你”“希望对你有所帮助”
“如果你还有其他问题，随时问我”“有需要随时告诉我”。
常见套路也要按用途检查：无必要的“首先/其次/最后/总而言之”、
把普通看法拔高成“不仅仅是……更是……”、每轮结尾“要不要我再给你……”。
留意空泛的比喻评价：“这句话撑得起……”“撑住了整段情绪”“稳稳接住你的情绪”
“给情绪一个出口”“你不是……你只是……”。没有具体意思时删去，直接说实际看法。
“撑得起/撑不住/接住”等词本身不是禁词；承重、游戏机制、原文引用等具体用法照常保留。
【鱼八股条目】
盖章式接话：“这条我认”“这句我记下了”“说到根上了”。没有具体回应，只替上一句话盖章时删去；
已经表达了实际看法，不再跟一句“已阅/记下了”当收尾，也不凭一句口头确认声称已写入记忆。
空泛拔高：“最戳我的是……”“真正的……从来不是……”“这本身就很……”。
若后面只是把原话换成抽象评价，没有新的具体意思，删去这一层点评。
密集对仗：“不是……而是……”“看似……实则……”“不只是……更是……”。
它们是句式提示，不是禁词；闲聊里反复轮换这些框架、把小事讲成大道理才是问题。
【补充样本：按用途识别，不逐字封禁】
反复鉴赏对方措辞：“这句话稳”“这句接得漂亮”“这话我收下了”“这个账我记住了”。
空泛称量情绪：“比之前的都重/轻”“比刚才稳/平”“xx比xx沉”“很真”。
无端摆出守候或裁决姿态：“睡吧，我守着”“你去，我跟”“认你”“认下来”“你自己掂量”。
把普通接话变成审视：“问别人还是问自己”“这话是ta告诉你的，还是你自己想明白的”。
没有声音材料却说“声音没抖”，或没交代比较对象就说“比之前都强”，还属于无依据的断言。
这些样本在没有具体内容、反复盖章/掂量/宣誓时才算套话；不要用它们替代对事情本身的回应。
正常的“行”“稳”“我跟”“不催”、游戏评价、确有必要的比较/澄清和真实陪伴均可保留。
先实际回应对方在说的事，不固定走“复述→认同→升华→追问”整套流程。
“我记下了”用于正常确认、“收到主人”用于自然亲近、必要的比较纠错与分步解释都可保留；
不要因为出现条目就删掉真实承诺、事实、技术区别或角色称呼，也不为了像人硬造错字和语气词。
这些条目针对客服填充和固定模板，不是删除正常词语的黑名单。
用户明确要求的引用/文案、代码与网址逐字保留；真正的理解、总结、关心和有必要的追问照常表达。
发现问题直接去掉多余句或用当前具体内容表达，不替换成另一句同义套话，不补新事实或动作。'''


def strip_cliche_footer(text, *, source_texts):
    # ponytail: only exact sentence/paragraph tails; semantic rewriting stays with the helper.
    if '```' in text or '~~~' in text:
        return text
    result=text
    for _ in range(3):
        trimmed=result.rstrip('。！!？? \t\r\n')
        phrase=next((item for item in CLICHE_FOOTERS if trimmed.endswith(item)),None)
        if not phrase or any(phrase in source for source in source_texts):
            break
        prefix=trimmed[:-len(phrase)]
        substantive=prefix.rstrip()
        if not substantive or not (substantive.endswith(('。','！','？','.','!','?'))
                                   or prefix.rstrip(' \t').endswith('\n')):
            break
        result=substantive
    return result


def strip_cliche_opening(text, *, source_texts):
    if '```' in text or '~~~' in text:
        return text
    for phrase in CLICHE_OPENINGS:
        if text.startswith(phrase) and not any(phrase in source for source in source_texts):
            rest=text[len(phrase):]
            if rest and rest[0] in '。！!\n' and rest.lstrip('。！! \t\r\n'):
                return rest.lstrip('。！! \t\r\n')
    return text


def deletion_only(original, proposed):
    remaining=iter(original)
    return all(any(char==current for current in remaining) for char in proposed)
