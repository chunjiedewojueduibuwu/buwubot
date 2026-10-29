"""Version-bound feeling dynamics; text edits and migration create no mood stimulus."""

import json
from dataclasses import asdict

from .affect import AffectProfile
from .config import FeelingPolicy
from .coordinator import require_coordinator
from .emotion import feeling_view, reinforce
from .memory_dynamics import MemoryAnchor, consolidation_level
from .store import Conflict


class FeelingMemory:
    def __init__(self,memory):
        self.memory,self.store=memory,memory.store
        self.store.connection.execute('''CREATE TABLE IF NOT EXISTS memory_feelings(
            memory_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,anchor TEXT NOT NULL,policy TEXT NOT NULL)''')
        if 'affect' not in {row['name'] for row in self.store.rows('PRAGMA table_info(memory_feelings)')}:
            self.store.connection.execute('ALTER TABLE memory_feelings ADD COLUMN affect TEXT')

    def state(self,item):
        rows=self.store.rows('SELECT * FROM memory_feelings WHERE memory_id=?',(item['id'],))
        if not rows:return None
        if rows[0]['revision']!=item['revision']:raise Conflict('feeling revision changed')
        return MemoryAnchor(**json.loads(rows[0]['anchor'])),FeelingPolicy(**json.loads(rows[0]['policy']))

    def save(self,item,anchor,policy,profile=None):
        require_coordinator()
        rows=self.store.rows('SELECT * FROM memories WHERE id=? AND deleted=0',(item['id'],))
        live=dict(rows[0]) if rows else None
        if (not live or live['revision']!=item['revision'] or live['tier']!='feeling'
                or live['persona_id']!=item['persona_id'] or live['group_id']!=item['group_id']):
            raise Conflict('feeling changed before dynamics update')
        self.store.connection.execute('INSERT INTO memory_feelings(memory_id,revision,anchor,policy,affect) VALUES(?,?,?,?,?) ON CONFLICT(memory_id) '
            'DO UPDATE SET revision=excluded.revision,anchor=excluded.anchor,policy=excluded.policy,affect=COALESCE(excluded.affect,memory_feelings.affect)',
            (item['id'],item['revision'],json.dumps(asdict(anchor),allow_nan=False),policy.model_dump_json(),
             profile.model_dump_json() if profile is not None else None))

    def profile(self,item,*,now):
        state=self.state(item)
        row=self.store.rows('SELECT affect FROM memory_feelings WHERE memory_id=?',(item['id'],))
        if not state or not row or not row[0]['affect']:return AffectProfile()
        anchor,policy=state
        factor=feeling_view(anchor,policy,now=now)['emotion']/anchor.emotion if anchor.emotion else 0
        return AffectProfile.model_validate_json(row[0]['affect']).scaled(min(1,factor))

    def view(self,item,*,now):
        state=self.state(item)
        if state is None:return None
        anchor,policy=state
        current=feeling_view(anchor,policy,now=now)
        profile=self.profile(item,now=now)
        return {'dynamics_version':2,'meaning_strength':current['significance'],
            'initial_meaning_strength':anchor.initial_significance,
            'detail_integrity':current['detail'],'emotional_residue':current['emotion'],
            'feeling_level':current['emotion'],'feeling_valence':current['valence'],
            'initial_feeling':anchor.initial_emotion,'memory_remaining':current['remaining'],
            'consolidation':current['consolidation'],'interference':current['interference'],
            'decay_factor':current['emotion']/anchor.initial_emotion if anchor.initial_emotion else 0,
            'decay_basis_time':anchor.since,'reading_strategy':policy.strategy,
            'feeling_emotions':profile.emotions,'feeling_arousal':profile.arousal}

    def sync(self,before,after,proposal,*,now):
        require_coordinator()
        old=self.state(before) if before and before['tier']=='feeling' else None
        policy=self.memory.settings.memory_policies.feeling
        profile=self.profile(before,now=now) if old else AffectProfile()
        if old:
            anchor,previous=old
            current=feeling_view(anchor,previous,now=now)
            anchor=MemoryAnchor(**(current|{'since':max(now,anchor.since)}))
            if proposal.feeling_strength is not None:
                if current['emotion']:
                    scale=min(1,proposal.feeling_strength/current['emotion'])
                    profile=AffectProfile(emotions={key:value*scale for key,value in profile.emotions.items()},
                                          arousal=profile.arousal)
                anchor=MemoryAnchor(**(asdict(anchor)|{'emotion':proposal.feeling_strength,
                    'initial_emotion':proposal.feeling_strength}))
            if proposal.feeling_valence is not None:
                anchor=MemoryAnchor(**(asdict(anchor)|{'valence':proposal.feeling_valence}))
            if proposal.significance is not None:
                anchor=MemoryAnchor(**(asdict(anchor)|{'initial_significance':proposal.significance,
                    'significance':proposal.significance*anchor.remaining}))
        else:
            anchor=MemoryAnchor(significance=proposal.significance if proposal.significance is not None else .5,
                emotion=proposal.feeling_strength if proposal.feeling_strength is not None else .5,
                valence=proposal.feeling_valence if proposal.feeling_valence is not None else 0,
                since=min(now,proposal.event_time) if proposal.event_time is not None else now)
        profile=AffectProfile(emotions=profile.emotions if proposal.feeling_emotions is None else proposal.feeling_emotions,
            arousal=proposal.feeling_arousal if 'feeling_arousal' in proposal.model_fields_set else profile.arousal).within(anchor.emotion)
        self.save(after,anchor,policy,profile)

    def reconfigure(self,policy,*,now):
        require_coordinator()
        for row in self.store.rows("SELECT * FROM memories WHERE tier='feeling' AND deleted=0"):
            item=self.memory.decode(row)
            state=self.state(item)
            if state:
                anchor,previous=state
                current=feeling_view(anchor,previous,now=now)
                profile=self.profile(item,now=now)
                self.save(item,MemoryAnchor(**(current|{'since':max(now,anchor.since)})),policy,profile)

    def recall(self,before,after,*,now):
        anchor,policy=self.state(before)
        profile=self.profile(before,now=now)
        current=feeling_view(anchor,policy,now=now)
        gain=policy.recall_gain
        current.update(emotion=reinforce(current['emotion'],gain),
            initial_emotion=reinforce(anchor.initial_emotion,gain),
            significance=reinforce(current['significance'],gain),
            initial_significance=reinforce(anchor.initial_significance,gain),
            detail=reinforce(current['detail'],gain),remaining=reinforce(current['remaining'],gain))
        count=self.store.rows('SELECT COUNT(*) FROM emotion_recalls WHERE memory_id=?',(before['id'],))[0][0]
        current['consolidation']=max(current['consolidation'],consolidation_level(count))
        updated=MemoryAnchor(**(current|{'since':max(now,anchor.since)}))
        profile=AffectProfile(emotions={key:reinforce(value,gain) for key,value in profile.emotions.items()},
                              arousal=profile.arousal).within(updated.emotion)
        self.save(after,updated,policy,profile)
        return updated
