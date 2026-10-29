"""Revision-bound memory dynamics; source/permission review remains at canonical callers."""

import json
import math
from dataclasses import asdict

from .coordinator import require_coordinator
from .memory_dynamics import DynamicsPolicy, MemoryAnchor, advance, consolidation_level, reinforced
from .store import Conflict


class MemorySignals:
    def __init__(self,store):
        self.store=store
        # Empty tables do not activate curves or migrate any existing memory.
        store.connection.executescript('''
            CREATE TABLE IF NOT EXISTS memory_signals(
                memory_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                anchor TEXT NOT NULL, policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_signal_events(
                memory_id TEXT NOT NULL, event_id TEXT NOT NULL, time REAL NOT NULL,
                PRIMARY KEY(memory_id,event_id));
        ''')
        if 'spacing_half_life' not in {row['name'] for row in store.rows('PRAGMA table_info(memory_signal_events)')}:
            store.connection.execute('ALTER TABLE memory_signal_events ADD COLUMN spacing_half_life REAL')
        if 'effective_gain' not in {row['name'] for row in store.rows('PRAGMA table_info(memory_signal_events)')}:
            store.connection.execute('ALTER TABLE memory_signal_events ADD COLUMN effective_gain REAL')

    def _current(self,item):
        current=self.store.rows('SELECT revision,deleted FROM memories WHERE id=?',(item['id'],))
        if not current or current[0]['deleted'] or current[0]['revision']!=item['revision']:
            raise Conflict('memory changed before dynamics update')

    def _read(self,item):
        rows=self.store.rows('SELECT * FROM memory_signals WHERE memory_id=?',(item['id'],))
        if not rows:
            return None
        if rows[0]['revision']!=item['revision']:
            raise Conflict('memory dynamics require revision synchronization')
        return (MemoryAnchor(**json.loads(rows[0]['anchor'])),DynamicsPolicy(**json.loads(rows[0]['policy'])))

    def _write(self,item,anchor,policy):
        self.store.connection.execute('INSERT INTO memory_signals VALUES(?,?,?,?) '
            'ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,'
            'anchor=excluded.anchor,policy=excluded.policy',
            (item['id'],item['revision'],json.dumps(asdict(anchor),allow_nan=False),
             json.dumps(asdict(policy),allow_nan=False)))

    def initialize(self,item,anchor: MemoryAnchor,policy: DynamicsPolicy):
        require_coordinator()
        with self.store.transaction():
            self._current(item)
            if self.store.rows('SELECT 1 FROM memory_signals WHERE memory_id=?',(item['id'],)):
                raise Conflict('memory already has dynamics')
            self._write(item,anchor,policy)

    def view(self,item,*,now):
        state=self._read(item)
        if state:
            self._current(item)
            return advance(*state,now=now)
        return None

    def reanchor(self,item,*,policy: DynamicsPolicy,now):
        require_coordinator()
        with self.store.transaction():
            self._current(item)
            state=self._read(item)
            if not state:
                raise Conflict('memory has no dynamics')
            anchor,old_policy=state
            updated=MemoryAnchor(**(advance(anchor,old_policy,now=now)|{'since':max(now,anchor.since)}))
            self._write(item,updated,policy)

    def synchronize(self,before,after):
        """A reviewed text edit changes the content revision, not the experience's age."""
        require_coordinator()
        with self.store.transaction():
            self._current(after)
            state=self._read(before)
            if state:
                self._write(after,*state)

    def reclassify(self,item,*,significance,now):
        """Reviewed meaning correction changes no detail, remaining level or recall count."""
        require_coordinator()
        with self.store.transaction():
            self._current(item)
            anchor,policy=self._read(item)
            current=advance(anchor,policy,now=now)
            updated=MemoryAnchor(**(current|{'initial_significance':significance,
                'significance':significance*current['remaining'],'since':max(now,anchor.since)}))
            self._write(item,updated,policy)

    def recall(self,item,*,event_id: str,now,gain: float = .05,spacing_half_life=None):
        """Caller supplies a validated real event, never an index/read/access receipt."""
        require_coordinator()
        if not isinstance(event_id,str) or not event_id.strip() or len(event_id)>300:
            raise ValueError('recall requires a bounded real event ID')
        if spacing_half_life is not None and (not event_id.startswith(('source:','merge:'))
                or isinstance(spacing_half_life,bool) or not isinstance(spacing_half_life,(int,float))
                or not math.isfinite(spacing_half_life) or not 1<=spacing_half_life<=604800):
            raise ValueError('spacing requires a bounded actual-source interval')
        with self.store.transaction():
            self._current(item)
            state=self._read(item)
            if not state:
                raise Conflict('memory has no dynamics')
            if self.store.rows('SELECT 1 FROM memory_signal_events WHERE memory_id=? AND event_id=?',
                               (item['id'],event_id)):
                return False
            anchor,policy=state
            # Validate time before sorting source receipts, including late arrivals.
            advance(anchor,policy,now=now)
            stamp=now if spacing_half_life is not None else max(now,anchor.since)
            events=[dict(row) for row in self.store.rows('SELECT * FROM memory_signal_events WHERE memory_id=?',(item['id'],))]
            previous_events=list(events)
            events.append({'event_id':event_id,'time':stamp,'spacing_half_life':spacing_half_life})
            if policy.curve_version==2:
                interval=spacing_half_life or 6*3600
                baseline=item['event_time'] if item['event_time'] is not None else item['created_at']
                def credits(receipts):
                    previous=baseline;session_end=None;count=0.
                    for event in sorted(receipts,key=lambda row:(row['time'],row['event_id'])):
                        scale=event['spacing_half_life'] or interval
                        when=event['time']
                        if session_end is None or when-session_end>=min(1800.,scale/4):
                            count+=-math.expm1(-math.log(2)*max(0.,when-previous)/scale)
                        previous=max(previous,when)
                        session_end=when
                    return count
                count=credits(events)
                contribution=max(0.,count-credits(previous_events))
                if contribution:
                    updated=reinforced(anchor,policy,now=max(stamp,anchor.since),
                        consolidation=consolidation_level(count),significance_gain=gain*contribution,
                        detail_gain=0,emotion_gain=0,event_age=max(0.,anchor.since-stamp))
                    self._write(item,updated,policy)
                effective=gain*contribution
                self.store.connection.execute('INSERT INTO memory_signal_events(memory_id,event_id,time,spacing_half_life,effective_gain) VALUES(?,?,?,?,?)',
                                              (item['id'],event_id,stamp,interval,effective))
                return True
            sources=[row['time'] for row in events if row['event_id'].startswith('source:')]
            baseline=item['event_time'] if item['event_time'] is not None else item['created_at']
            previous=min([baseline,*sources]);count=0.
            for event in sorted(events,key=lambda row:(row['time'],row['event_id'])):
                interval=event['spacing_half_life']
                count+=1 if interval is None else .1+.9*-math.expm1(-math.log(2)*max(0,event['time']-previous)/interval)
                if event['event_id'].startswith('source:'):previous=max(previous,event['time'])
            updated=reinforced(anchor,policy,now=now,consolidation=consolidation_level(count),
                               significance_gain=gain,detail_gain=gain,emotion_gain=gain)
            self._write(item,updated,policy)
            self.store.connection.execute('INSERT INTO memory_signal_events(memory_id,event_id,time,spacing_half_life) VALUES(?,?,?,?)',
                                          (item['id'],event_id,stamp,spacing_half_life))
            return True
