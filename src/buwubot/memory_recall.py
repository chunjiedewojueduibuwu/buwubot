"""Read-only recall signals and source-bound detail projection; no invented or rewritten evidence."""

import math
import re

from .memory_dynamics import recall_score

_STOP={'什么','怎么','为何','为什么','之前','现在','今天','昨天','事情','记得','记住',
       '一下','这个','那个','可以','请问','是否','时候','情况','怎样','上次'}


def terms(text,aliases=()):
    text=text.casefold()
    for alias in aliases:
        if alias:
            text=text.replace(alias.casefold(),' ')
    result=set(re.findall(r'[a-z0-9_]+',text))
    for run in re.findall(r'[\u4e00-\u9fff]+',text):
        result.update(run[i:i+2] for i in range(len(run)-1))
        if len(run)==1 and run not in '我你他她它的和吗呢':
            result.add(run)
    return result-_STOP


def text_cue(query_terms,text,weights=None):
    if not query_terms:
        return 0.
    weights=weights or {term:1. for term in query_terms}
    hits=sum(weights[term] for term in query_terms if term in text.casefold())
    return .55+.45*hits/sum(weights[term] for term in query_terms) if hits else 0.


def fragment_budget(count,detail):
    if (isinstance(count,bool) or not isinstance(count,int) or count<0 or isinstance(detail,bool)
            or not isinstance(detail,(int,float)) or not math.isfinite(detail) or not 0<=detail<=1):
        raise ValueError('invalid fragment count or detail integrity')
    # Nominal budget only; relevant high-quality or necessary evidence can exceed this count.
    return min(count,math.floor(count*detail))


def wants_details(query):
    return bool(re.search(r'原话|原文|详细|全部细节|完整经过|逐字|逐条|具体经过',query))


class MemoryRecall:
    def __init__(self,memory,*,now):
        self.memory,self.now=memory,now

    def annotate(self,items,*,query,lookup,vector_hits):
        original=terms(query,self.memory.settings.aliases)
        expanded=terms(lookup,self.memory.settings.aliases)
        texts=[item['content']+' '+ ' '.join(item.get('tags',[])) for item in items]
        weights={term:math.log1p(len(items)/max(1,sum(term in text.casefold() for text in texts)))
                 for term in original|expanded}
        cues={}
        for item in items:
            text=item['content']+' '+ ' '.join(item.get('tags',[]))
            cue=max(text_cue(original,text,weights),.85*text_cue(expanded,text,weights))
            if item['id'] in vector_hits:
                score=vector_hits[item['id']]
                cue=max(cue,.65 if score is None else score)
            if item.get('entity_recall') or item.get('entity_path'):
                cue=max(cue,.8)
            cues[item['id']]=cue
        for item in items:
            path=item.get('relation_recall')
            if path:
                cues[item['id']]=max(cues[item['id']],cues.get(path['seed_id'],0)*.8**len(path['steps']))
        result=[]
        for item in items:
            policy=getattr(self.memory.settings.memory_policies,item.get('tier','long_term')).curve()
            competitors={r['target_id'] if r['source_id']==item['id'] else r['source_id']
                         for r in item.get('relations',[]) if r['kind']=='correction'}
            interference=max(item.get('interference',0),len(competitors)/(1+len(competitors)))
            basis=item.get('event_time') if item.get('event_time') is not None else item.get('created_at',self.now)
            recency=2**-min(1075,max(0,self.now-basis)/policy.base_half_life)
            inputs={'significance':item.get('meaning_strength',.5),
                'emotion':item.get('emotional_residue',item.get('feeling_level',0)),
                'cue':cues[item['id']],'recency':recency,'recall':item.get('consolidation',0),
                'interference':interference}
            score=recall_score(**inputs,policy=policy)
            result.append(item|{'recall_signals':inputs|{'accessibility':score,
                'triggered':score>=policy.recall_threshold,'threshold':policy.recall_threshold}})
        return sorted(result,key=lambda item:-item['recall_signals']['accessibility'])

    def _linked(self,item,kind):
        linked=[]
        for ref in self.memory.lifecycle.lineage(self.memory,item)[kind]:
            other=self.memory.get(ref['memory_id']) if ref['current'] else None
            if (other and not self.memory.has_recalled_source(other['source_message_ids'])
                    and self.memory.with_reading_policy(other,now=self.now).get('memory_remaining',1)>0):
                linked.append(other)
        return linked

    def project(self,item,*,query,detail=False,fragment_chars=4000):
        if item.get('kind')=='learning_experience':
            return item
        current=self.memory.get(item['id'])
        if not current or current['revision']!=item['revision']:
            return None
        view=item|self.memory.with_reading_policy(current,now=self.now)
        view.pop('recall_fragments',None)
        detail=detail or wants_details(query)
        if not detail:
            view['relations']=[{key:value for key,value in edge.items()
                if key not in {'source_content','target_content'}} for edge in view.get('relations',[])]
        originals=self._linked(current,'distillation_sources')
        source=current
        mode='stored_detail' if detail else 'stored_record'
        evidence_kind='stored_feeling' if current['tier']=='feeling' else 'stored_fact'
        outline=None
        if current['tier']=='recent' and not detail:
            outlines=[other for other in self._linked(current,'distillation_targets')
                      if other['tier']=='long_term']
            if outlines:
                query_terms=terms(query,self.memory.settings.aliases)
                outline=max(outlines,key=lambda other:text_cue(query_terms,other['content']))
                # A concrete surviving clue recovers the original stored details, never generated ones.
                specific=any(term in current['content'].casefold() and term not in outline['content'].casefold()
                             for term in query_terms)
                if specific:
                    mode='stored_detail'
                else:
                    source=outline;mode='verified_outline';evidence_kind='reviewed_distillation'
                    originals=self._linked(outline,'distillation_sources')
        # A durable outline must not give its source episodes the outline's longer detail half-life.
        strengths={parent['id']:self.memory.with_reading_policy(parent,now=self.now).get('detail_integrity',1)
                   for parent in originals}
        detail_strength=sum(strengths.values())/len(originals) if originals else view.get('detail_integrity',1)
        nominal=fragment_budget(len(originals),detail_strength)
        query_terms=terms(query,self.memory.settings.aliases)
        def needed(parent):
            text=parent['content'].casefold()
            specific=any(term in text and term not in source['content'].casefold() for term in query_terms)
            return specific
        ranked=sorted(originals,key=lambda other:(not needed(other),
            -text_cue(query_terms,other['content'])*strengths[other['id']]))
        fragments=[];used=0
        for parent in ranked:
            cost=len(parent['content'])
            if not detail and (used+cost>fragment_chars or
                    (not needed(parent) and cost>fragment_chars*strengths[parent['id']]**2)):
                continue
            fragments.append({'memory_id':parent['id'],'revision':parent['revision'],
                'subject_id':parent['subject_id'],'event_time':parent['event_time'],
                'content':parent['content'],'evidence_kind':'stored_fact',
                'detail_priority':text_cue(query_terms,parent['content'])*strengths[parent['id']],
                'detail_integrity':strengths[parent['id']]})
            used+=cost
        view.update(content=source['content'],confidence=min(current['confidence'],source['confidence']),
            recall_representation={'mode':mode,'evidence_kind':evidence_kind,
                'evidence_refs':[{'memory_id':source['id'],'revision':source['revision']}],
                'outline_id':outline['id'] if mode=='verified_outline' else None,
                'outline_revision':outline['revision'] if mode=='verified_outline' else None,
                'available_fragments':len(originals),'fragment_budget':len(fragments),
                'nominal_fragment_budget':nominal,'fragment_selection':'necessary_then_continuous_detail_allowance',
                'provided_fragments':len(fragments),'detail_available':bool(originals or mode=='verified_outline'),
                'fragment_rounding':'nominal_floor_only','fragment_strength_basis':'source_detail_integrity_mean'})
        if fragments:
            view['recall_fragments']=fragments
        return view
