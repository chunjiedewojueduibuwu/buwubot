"""Ephemeral recollection hypotheses bound to the evidence actually returned to the role."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

RECOLLECTION_PROMPT = """
若问题在回顾经历且需要串联零散材料，可额外返回 recollection:
{"interpretations":[{"text":"简短的可能解释或大意","source_refs":[{"memory_id":"实际来源id","revision":1}]}],
"unknowns":["具体还缺什么"],"unknown_refs":[{"memory_id":"支撑这些缺口的实际来源id","revision":1}]}。
直接查数字、原话或现成事实时省略，不为每次检索凑重建。
结合实际片段、context_memory中的当前情境、候选里的相关人物信息、时间和已审核关联，
区分原件、要点和推测。关系连通或同名不是新事实；不能把猜测写成真实经历或补出原话。
source_refs必须来自已选条目recall_representation.evidence_refs或其实际recall_fragments的memory_id/revision。
verified_outline的正文来自摘要，不能拿原件id冒充该摘要的依据；未提供的候选不能引用。
请求detail_ids不代表已经读到原件，不能先用还没提供的原件支撑重建。unknowns也必须由unknown_refs绑定，不能夹带失效或未选来源的细节。
每条简短概括或假设最多600字，最多6条、每条最多32个引用；unknowns最多6条、每条200字。
这些是临时参考，不是正式记忆、权限、工具结果或思维链；没有依据时省略，不强求原因。
检索不到或原件缺失不证明从未发生。可以表达记得大概，缺失部分作为推测，不做虚假逐字回忆。
"""


class SourceReference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    memory_id: str=Field(min_length=1,max_length=200)
    revision: int=Field(ge=1,strict=True)


class Interpretation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text: str=Field(min_length=1,max_length=600)
    source_refs: list[SourceReference]=Field(min_length=1,max_length=32)


class Recollection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    interpretations: list[Interpretation]=Field(default_factory=list,max_length=6)
    unknowns: list[Annotated[str,Field(min_length=1,max_length=200)]]=Field(default_factory=list,max_length=6)
    unknown_refs: list[SourceReference]=Field(default_factory=list,max_length=32)


def _provided_refs(provided):
    available=set()
    for item in provided:
        refs=item.get('recall_representation',{}).get('evidence_refs',[])
        refs=refs+[{'memory_id':part['memory_id'],'revision':part['revision']}
                   for part in item.get('recall_fragments',[])]
        available.update((ref['memory_id'],ref['revision']) for ref in refs)
    return available


def bind_recollection(raw,provided,*,seen):
    if raw is None:return None
    try:draft=Recollection.model_validate(raw)
    except ValueError:return None
    available=_provided_refs(provided)&_provided_refs(seen)
    def valid(refs):return bool(refs) and all((ref.memory_id,ref.revision) in available for ref in refs)
    interpretations=[item.model_dump()|{'status':'inferred'} for item in draft.interpretations if valid(item.source_refs)]
    unknowns=draft.unknowns if valid(draft.unknown_refs) else []
    return {'kind':'reconstructed_reference','interpretations':interpretations,'unknowns':unknowns,
            'unknown_refs':[ref.model_dump() for ref in draft.unknown_refs] if unknowns else []} if interpretations or unknowns else None
