"""Pure, bounded memory curves; identity, evidence and mutation belong to their callers."""

import math
from dataclasses import dataclass, fields

DAY=86400


def _number(value,name,minimum,maximum):
    if (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)
            or not minimum<=value<=maximum):
        raise ValueError(f'invalid {name}')


@dataclass(frozen=True)
class MemoryAnchor:
    significance: float = .5
    detail: float = 1.
    emotion: float = 0.
    valence: float = 0.
    consolidation: float = 0.
    interference: float = 0.
    remaining: float = 1.
    since: float = 0.
    initial_significance: float | None = None
    initial_emotion: float | None = None
    fast_remaining: float | None = None
    slow_remaining: float | None = None
    recall_meaning: float = 0.

    def __post_init__(self):
        for field in fields(self):
            if field.name in {'fast_remaining','slow_remaining'} and getattr(self,field.name) is None:
                object.__setattr__(self,field.name,self.remaining)
            if field.name.startswith('initial_') and getattr(self,field.name) is None:
                object.__setattr__(self,field.name,getattr(self,field.name.removeprefix('initial_')))
            lower,upper=(-1.,1.) if field.name=='valence' else (
                (-1e308,1e308) if field.name=='since' else (0.,1.))
            _number(getattr(self,field.name),field.name,lower,upper)


@dataclass(frozen=True)
class DynamicsPolicy:
    curve_version: float = 1.
    base_half_life: float = 7*DAY
    minimum_half_life: float = 60.
    maximum_half_life: float = 36500*DAY
    meaning_influence: float = 8.
    consolidation_influence: float = .5
    emotional_influence: float = 1.
    interference_influence: float = .5
    detail_ratio: float = .35
    emotion_detail_protection: float = .5
    consolidation_detail_protection: float = .5
    interference_detail_influence: float = 1.
    emotion_half_life: float = 3*DAY
    emotion_minimum_half_life: float = 60.
    emotion_maximum_half_life: float = 36500*DAY
    emotion_meaning_influence: float = 8.
    significance_weight: float = 1.4
    emotion_weight: float = .6
    cue_weight: float = 3.
    recency_weight: float = .5
    recall_weight: float = .4
    interference_weight: float = 1.
    recall_bias: float = 2.
    recall_threshold: float = .4

    def __post_init__(self):
        if self.curve_version not in (1,2):
            raise ValueError('invalid curve version')
        for field in fields(self):
            name=field.name
            if name in {'base_half_life','minimum_half_life','maximum_half_life','emotion_half_life',
                        'emotion_minimum_half_life','emotion_maximum_half_life'}:
                limits=(1.,365000*DAY)
            elif name in {'detail_ratio','emotion_detail_protection','consolidation_detail_protection'}:
                limits=(.001,.999) if name=='detail_ratio' else (0.,.999)
            elif name=='recall_threshold':
                limits=(0.,1.)
            elif name=='recall_bias':
                limits=(-32.,32.)
            else:
                limits=(0.,16.)
            _number(getattr(self,name),name,*limits)
        if self.minimum_half_life>self.maximum_half_life:
            raise ValueError('minimum half life exceeds maximum')
        if self.emotion_minimum_half_life>self.emotion_maximum_half_life:
            raise ValueError('minimum emotion half life exceeds maximum')


def _dual_rates(anchor,policy):
    # Fixed weights keep policy changes and recall from creating a discontinuous jump.
    s=min(1.,anchor.initial_significance+anchor.recall_meaning)-.5
    c=anchor.consolidation
    common=policy.emotional_influence*anchor.initial_emotion*.5
    interference=math.log1p(policy.interference_influence*anchor.interference)
    fast_ratio=math.log(2)/math.log(.75/(.5-.25*2**(-1/4)))
    def life(ratio,meaning,consolidation):
        value=math.log(policy.base_half_life*ratio)+math.log(2)*(
            meaning*s+consolidation*c+common)-interference
        return math.exp(min(math.log(policy.maximum_half_life),max(math.log(policy.minimum_half_life),value)))
    fast=life(fast_ratio,policy.meaning_influence*.5,policy.consolidation_influence)
    slow=life(4.,policy.meaning_influence*.1875,policy.consolidation_influence*4)
    return fast,max(fast,slow)


def _rates(anchor: MemoryAnchor,policy: DynamicsPolicy):
    """Keep the event's meaning across time-only re-anchoring; residual emotion still fades."""
    exponent=(policy.meaning_influence*(anchor.initial_significance-.5)
        +policy.consolidation_influence*anchor.consolidation+policy.emotional_influence*anchor.initial_emotion)
    log_h=(math.log(policy.base_half_life)+math.log(2)*exponent
           -math.log1p(policy.interference_influence*anchor.interference))
    meaning=math.exp(min(math.log(policy.maximum_half_life),max(math.log(policy.minimum_half_life),log_h)))
    if policy.curve_version==2:
        meaning=_dual_rates(anchor,policy)[0]
    emotion=min(policy.emotion_maximum_half_life,max(policy.emotion_minimum_half_life,policy.emotion_half_life*(
        1+policy.emotion_meaning_influence*anchor.initial_emotion*anchor.initial_significance
        +anchor.consolidation)))
    detail_rate=math.log(2)/(meaning*policy.detail_ratio)*(
        (1-policy.consolidation_detail_protection*anchor.consolidation)
        *(1+policy.interference_detail_influence*anchor.interference))
    return meaning,emotion,detail_rate


def _detail_loss(anchor,policy,elapsed,meaning,emotion,rate):
    """Integrate max(min_rate, A*(1-B*exp(-k*t))) exactly between events."""
    floor=math.log(2)/(.95*meaning)
    if rate<=floor:
        return floor*elapsed
    protection=policy.emotion_detail_protection*anchor.emotion
    k=math.log(2)/emotion
    crossing=0.
    if rate*(1-protection)<floor:
        crossing=-math.log((1-floor/rate)/protection)/k
    protected=min(elapsed,crossing)
    if elapsed<=crossing:
        return floor*elapsed
    duration=elapsed-crossing
    # expm1 preserves small intervals, including repeated re-anchoring after restart.
    integral=duration+protection*math.exp(-k*crossing)*math.expm1(-k*duration)/k
    return floor*protected+rate*max(0.,integral)


def half_lives(anchor: MemoryAnchor,policy: DynamicsPolicy) -> dict[str,float]:
    meaning,emotion,rate=_rates(anchor,policy)
    low,high=0.,.95*meaning
    for _ in range(52):
        middle=(low+high)/2
        if _detail_loss(anchor,policy,middle,meaning,emotion,rate)<math.log(2):
            low=middle
        else:
            high=middle
    result={'meaning':meaning,'detail':(low+high)/2,'emotion':emotion}
    if policy.curve_version==2:
        fast,slow=_dual_rates(anchor,policy)
        low,high=0.,slow
        total=.75*anchor.fast_remaining+.25*anchor.slow_remaining
        for _ in range(52):
            middle=(low+high)/2
            level=.75*anchor.fast_remaining*2**(-middle/fast)+.25*anchor.slow_remaining*2**(-middle/slow)
            if level>total/2:low=middle
            else:high=middle
        result.update(meaning=(low+high)/2,fast=fast,slow=slow)
    return result


def threshold_time(anchor: MemoryAnchor,policy: DynamicsPolicy,threshold: float) -> float:
    """Forecast the actual blended curve, without treating its half-life as a constant slope."""
    if anchor.remaining<=threshold:return anchor.since
    low=0.
    high=policy.maximum_half_life*max(1.,-math.log2(threshold))
    for _ in range(64):
        middle=(low+high)/2
        if advance(anchor,policy,now=anchor.since+middle)['remaining']>threshold:low=middle
        else:high=middle
    return anchor.since+(low+high)/2


def advance(anchor: MemoryAnchor,policy: DynamicsPolicy,*,now: float) -> dict[str,float]:
    _number(now,'now',-1e308,1e308)
    elapsed=max(0.,now-anchor.since)
    meaning,emotion,rate=_rates(anchor,policy)
    def decayed(initial,half_life):
        return initial*2**-min(1075.,elapsed/half_life)
    result={'significance':decayed(anchor.significance,meaning),
        'detail':anchor.detail*math.exp(-min(746.,_detail_loss(anchor,policy,elapsed,meaning,emotion,rate))),
        'emotion':decayed(anchor.emotion,emotion),'valence':anchor.valence,
        'remaining':decayed(anchor.remaining,meaning),'consolidation':anchor.consolidation,
        'interference':anchor.interference,'initial_significance':anchor.initial_significance,
        'initial_emotion':anchor.initial_emotion,'fast_remaining':decayed(anchor.remaining,meaning),
        'slow_remaining':decayed(anchor.remaining,meaning),'recall_meaning':anchor.recall_meaning}
    if policy.curve_version==2:
        fast,slow=_dual_rates(anchor,policy)
        result['fast_remaining']=decayed(anchor.fast_remaining,fast)
        result['slow_remaining']=decayed(anchor.slow_remaining,slow)
        result['remaining']=.75*result['fast_remaining']+.25*result['slow_remaining']
        total=.75*anchor.fast_remaining+.25*anchor.slow_remaining
        result['significance']=anchor.significance*(result['remaining']/total if total else 0.)
    return result


def reinforced(anchor: MemoryAnchor,policy: DynamicsPolicy,*,now: float,consolidation: float,
               significance_gain: float = .05,detail_gain: float = .05,emotion_gain: float = .1,
               event_age: float = 0.) -> MemoryAnchor:
    """Apply one proven event; callers own source validation and idempotent receipts."""
    for name,value in {'consolidation':consolidation,'significance_gain':significance_gain,
                       'detail_gain':detail_gain,'emotion_gain':emotion_gain}.items():
        _number(value,name,0.,1.)
    current=advance(anchor,policy,now=now)
    if policy.curve_version==2:
        # Partial effective-age rewind: facts keep their date, and lost detail is not invented.
        fast,slow=_dual_rates(anchor,policy)
        for key,life,gain in [('fast_remaining',fast,significance_gain),
                              ('slow_remaining',slow,significance_gain*.25)]:
            value=current[key]
            if value:
                current[key]=max(value,value**(1-gain)*2**-min(1075.,max(0.,event_age)*gain/life))
        current['remaining']=.75*current['fast_remaining']+.25*current['slow_remaining']
        current['recall_meaning']=min(.05,anchor.recall_meaning+significance_gain*.01)
        current['significance']=min(1.,anchor.initial_significance+current['recall_meaning'])*current['remaining']
        return MemoryAnchor(**(current|{'since':max(now,anchor.since),
            'consolidation':max(anchor.consolidation,consolidation)}))
    current['remaining']+=significance_gain*(1-current['remaining'])
    for name,gain in [('significance',significance_gain),('detail',detail_gain),('emotion',emotion_gain)]:
        # Emotion intensity is only reinforced if the experience actually carried a feeling.
        if name=='emotion' and anchor.initial_emotion==0:
            continue
        current[name]+=gain*(1-current[name])
        if name!='detail':
            initial='initial_'+name
            current[initial]+=gain*(1-current[initial])
    return MemoryAnchor(**(current|{'since':max(now,anchor.since),
        'consolidation':max(anchor.consolidation,consolidation)}))


def consolidation_level(evidence_count: float,*,scale: float = 3.) -> float:
    _number(evidence_count,'real evidence weight',0.,1e308)
    _number(scale,'consolidation scale',.001,1e6)
    return -math.expm1(-evidence_count/scale)


def recall_score(*,significance: float,emotion: float,cue: float,recency: float,recall: float,
                 interference: float,policy: DynamicsPolicy) -> float:
    for name,value in {'significance':significance,'emotion':emotion,'cue':cue,'recency':recency,
                       'recall':recall,'interference':interference}.items():
        _number(value,name,0.,1.)
    total=(policy.significance_weight*significance+policy.emotion_weight*emotion+policy.cue_weight*cue
        +policy.recency_weight*recency+policy.recall_weight*recall-policy.interference_weight*interference
        -policy.recall_bias)
    # This is recall accessibility, never an evidential probability or a new memory fact.
    if total>=0:
        return 1/(1+math.exp(-total))
    value=math.exp(total)
    return value/(1+value)
