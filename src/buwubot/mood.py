"""Present mood from accepted stimuli. Reading has no reinforcement or write side effect."""

import hashlib
import json
import math

from .affect import EMOTIONS, AffectProfile
from .coordinator import require_coordinator


def _aggregate(values):
    """Allow accumulation without turning many weak events into a strong feeling."""
    values=list(values)
    return min(1-math.prod(1-value for value in values),2*max(values,default=0.))


class Mood:
    def __init__(self,memory):
        self.memory,self.store=memory,memory.store
        self.store.connection.executescript('''
            CREATE TABLE IF NOT EXISTS mood_events(
                id INTEGER PRIMARY KEY,persona_id TEXT NOT NULL,event_key TEXT NOT NULL,
                memory_id TEXT,valence REAL NOT NULL,intensity REAL NOT NULL,time REAL NOT NULL,
                event_time REAL,half_life_minutes REAL,
                UNIQUE(persona_id,event_key));
            CREATE INDEX IF NOT EXISTS mood_persona_time ON mood_events(persona_id,time);
            CREATE TABLE IF NOT EXISTS mood_sources(
                persona_id TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(persona_id,source_id));
        ''')
        columns={row['name'] for row in self.store.rows('PRAGMA table_info(mood_events)')}
        for column in ('event_time','half_life_minutes','affect'):
            if column not in columns:
                self.store.connection.execute(f'ALTER TABLE mood_events ADD COLUMN {column} '+('TEXT' if column=='affect' else 'REAL'))

    def claim_sources(self,source_ids,*,persona_id=None):
        require_coordinator()
        persona=persona_id or self.memory.settings.persona_id
        novel=[]
        for source in sorted({self.memory.archive.canonical_id(mid) for mid in source_ids}):
            inserted=self.store.connection.execute('INSERT OR IGNORE INTO mood_sources VALUES(?,?)',(persona,source))
            if inserted.rowcount:novel.append(source)
        return novel

    def from_sources(self,source_ids,**event):
        require_coordinator()
        with self.store.transaction():
            novel=self.claim_sources(source_ids)
            if novel:
                key=hashlib.sha256(json.dumps(novel).encode()).hexdigest()
                self.adjust(operation_id='feeling-source:'+key,**event)

    def _visible(self,row,*,now,cfg):
        if not row['memory_id']:return True
        item=self.memory.get(row['memory_id'])
        return bool(item and item['tier']=='feeling' and item['persona_id']==cfg.persona_id
            and cfg.allows_conversation(item['group_id'])
            and not self.memory.has_recalled_source(item['source_message_ids'])
            and self.memory.with_reading_policy(item,now=now).get('memory_remaining',1)>0)

    def changes(self,*,now):
        """Local administrator audit only; never included in the model mood projection."""
        cfg,_=self.store.settings()
        if cfg.persona_id!=self.memory.settings.persona_id:return []
        reset=self.store.get_state('mood-reset:'+cfg.persona_id,{'cutoff':0})
        rows=self.store.rows('SELECT * FROM mood_events WHERE persona_id=? AND id>? AND time>=? ORDER BY id DESC LIMIT 20',
            (cfg.persona_id,reset['cutoff'],now-cfg.mood_half_life_minutes*60*20))
        result=[]
        for row in rows:
            if not self._visible(row,now=now,cfg=cfg):continue
            item=self.memory.get(row['memory_id']) if row['memory_id'] else None
            result.append({'time':row['event_time'] if row['event_time'] is not None else row['time'],
                'kind':'explicit_recall' if row['event_key'].startswith('recall:') else 'feeling' if item else 'manual',
                'reason':item['content'][:200] if item else '在管理页面调整了当前心情',
                'memory_id':item['id'] if item else None,'memory_revision':item['revision'] if item else None,
                'conversation_id':item['group_id'] if item else None})
        return result

    def adjust(self,*,valence,intensity,operation_id,now,memory_id=None,emotions=None,arousal=None):
        require_coordinator()
        if (any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x)
                for x in (valence,intensity,now)) or not -1<=valence<=1 or not 0<=intensity<=1
                or not isinstance(operation_id,str) or not 0<len(operation_id)<=200):
            raise ValueError('invalid mood event')
        affect=AffectProfile(emotions={} if emotions is None else emotions,arousal=arousal).within(intensity)
        persona=self.memory.settings.persona_id
        cfg,_=self.store.settings()
        if persona!=cfg.persona_id:raise ValueError('active persona changed')
        self.store.connection.execute('INSERT OR IGNORE INTO mood_events '
            '(persona_id,event_key,memory_id,valence,intensity,time,event_time,half_life_minutes,affect) VALUES(?,?,?,?,?,?,?,?,?)',
            (persona,operation_id,memory_id,valence,intensity,now,now,cfg.mood_half_life_minutes,affect.model_dump_json()))

    def reconfigure(self,half_life_minutes,*,now):
        require_coordinator()
        if (isinstance(half_life_minutes,bool) or not math.isfinite(half_life_minutes)
                or not 1<=half_life_minutes<=10080 or not math.isfinite(now)):
            raise ValueError('invalid mood policy')
        for row in self.store.rows('SELECT * FROM mood_events'):
            half_life=(row['half_life_minutes'] or self.memory.settings.mood_half_life_minutes)*60
            weight=row['intensity']*2**-min(1075,max(0,now-row['time'])/half_life)
            factor=2**-min(1075,max(0,now-row['time'])/half_life)
            profile=AffectProfile.model_validate_json(row['affect']).scaled(factor).model_dump_json() if row['affect'] else None
            self.store.connection.execute('UPDATE mood_events SET intensity=?,time=?,half_life_minutes=?,affect=? WHERE id=?',
                (weight,max(now,row['time']),half_life_minutes,profile,row['id']))

    def reset(self,*,now):
        require_coordinator()
        if isinstance(now,bool) or not math.isfinite(now):raise ValueError('invalid reset time')
        persona=self.memory.settings.persona_id
        cfg,_=self.store.settings()
        if cfg.persona_id!=persona:raise ValueError('active persona changed')
        cutoff=self.store.rows('SELECT COALESCE(MAX(id),0) FROM mood_events WHERE persona_id=?',(persona,))[0][0]
        self.store.set_state('mood-reset:'+persona,{'cutoff':cutoff,'time':now})

    def view(self,*,now):
        if isinstance(now,bool) or not math.isfinite(now):raise ValueError('invalid mood time')
        persona=self.memory.settings.persona_id
        cfg,_=self.store.settings()
        if cfg.persona_id!=persona:return {'valence':0.,'intensity':0.,'positive':0.,'negative':0.,
                                         'updated_at':None,'half_life_minutes':cfg.mood_half_life_minutes,
                                         'emotions':{},'arousal':None}
        reset=self.store.get_state('mood-reset:'+persona,{'cutoff':0,'time':None})
        half_life=self.memory.settings.mood_half_life_minutes*60
        rows=self.store.rows('SELECT * FROM mood_events WHERE persona_id=? AND id>? AND time>=? ORDER BY id',
            (persona,reset['cutoff'],now-half_life*20))
        weights=[];profiles=[];latest=reset['time']
        for row in rows:
            if not self._visible(row,now=now,cfg=cfg):continue
            event_half_life=(row['half_life_minutes'] or half_life/60)*60
            weight=row['intensity']*2**-min(1075,max(0,now-row['time'])/event_half_life)
            weights.append((row['valence'],weight))
            factor=2**-min(1075,max(0,now-row['time'])/event_half_life)
            profiles.append((AffectProfile.model_validate_json(row['affect']) if row['affect'] else AffectProfile()).scaled(factor))
            event_time=row['event_time'] if row['event_time'] is not None else row['time']
            latest=max(latest,event_time) if latest is not None else event_time
        total=sum(weight for _,weight in weights)
        def component(sign):
            return _aggregate(weight*max(0,sign*valence) for valence,weight in weights)
        emotions={key:_aggregate(profile.emotions.get(key,0) for profile in profiles) for key in EMOTIONS}
        arousals=[profile.arousal for profile in profiles if profile.arousal is not None]
        return {'valence':sum(valence*weight for valence,weight in weights)/total if total else 0.,
            'intensity':_aggregate(weight for _,weight in weights),
            'positive':component(1),'negative':component(-1),
            'updated_at':latest,'half_life_minutes':half_life/60,
            'emotions':{key:value for key,value in emotions.items() if value>0},
            'arousal':_aggregate(arousals) if arousals else (None if profiles else 0.)}
