"""Ordinary memory retention; factual confidence is independent of remaining strength."""

import math
from dataclasses import replace

from .coordinator import require_coordinator
from .memory_dynamics import advance, half_lives, threshold_time
from .memory_signals import MemorySignals
from .store import Conflict

TIERS = {'recent', 'secondary', 'important', 'long_term'}


def remaining(strength, since, now, half_life_minutes):
    if not all(math.isfinite(v) for v in (strength, since, now)) or not 0 <= strength <= 1:
        raise ValueError('invalid memory retention state')
    if half_life_minutes is None:
        return strength
    if not math.isfinite(half_life_minutes) or half_life_minutes <= 0:
        raise ValueError('invalid memory half life')
    return strength * 2 ** -min(1024, max(0, now - since) / (half_life_minutes * 60))


class MemoryLifecycle:
    def __init__(self, store):
        self.store = store
        self.signals=MemorySignals(store)
        store.connection.execute('''CREATE TABLE IF NOT EXISTS memory_lifecycle(
            memory_id TEXT PRIMARY KEY, strength REAL NOT NULL, since REAL NOT NULL,
            half_life_override REAL, protected INTEGER NOT NULL DEFAULT 0)''')
        store.connection.execute('''CREATE TABLE IF NOT EXISTS memory_evidence_seen(
            memory_id TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(memory_id,source_id))''')
        store.connection.execute('''CREATE TABLE IF NOT EXISTS memory_distillations(
            source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
            target_id TEXT NOT NULL, target_revision INTEGER NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(source_id,source_revision,target_id,target_revision))''')
        store.connection.execute('''CREATE TABLE IF NOT EXISTS memory_merge_recalls(
            operation_id TEXT NOT NULL, memory_id TEXT NOT NULL, recall_count INTEGER NOT NULL,
            gain REAL NOT NULL, before_remaining REAL NOT NULL, after_strength REAL NOT NULL, recalled_at REAL NOT NULL,
            PRIMARY KEY(operation_id,memory_id))''')

    def state(self, item):
        rows = self.store.rows('SELECT * FROM memory_lifecycle WHERE memory_id=?', (item['id'],))
        if rows:
            return dict(rows[0])
        basis = item['event_time'] if item.get('event_time') is not None else item['created_at']
        protected = bool(self.store.rows('SELECT 1 FROM memory_manual_guards WHERE memory_id=?', (item['id'],)))
        return {'memory_id': item['id'], 'strength': 1., 'since': basis,
                'half_life_override': None, 'protected': protected}

    def lineage(self, memory, item):
        result = {'distillation_sources': [], 'distillation_targets': []}
        if item['tier'] not in TIERS | {'core'}:
            return result
        for row in self.store.rows('SELECT * FROM memory_distillations WHERE source_id=? OR target_id=?',
                                   (item['id'], item['id'])):
            source = row['target_id'] == item['id']
            prefix = 'source' if source else 'target'
            other = memory.get(row[prefix + '_id'], include_deleted=True)
            if (not other or (other['group_id'], other['subject_id'], other['persona_id']) !=
                    (item['group_id'], item['subject_id'], item['persona_id'])):
                continue
            current = (not other['deleted'] and other['revision'] == row[prefix + '_revision']
                       and item['revision'] == row[('target' if source else 'source') + '_revision'])
            result['distillation_sources' if source else 'distillation_targets'].append({
                'memory_id': other['id'], 'revision': row[prefix + '_revision'],
                'created_at': row['created_at'], 'current': current,
                'content': other['content'] if current else None, 'tier': other['tier']})
        return result

    def view(self, item, policies, *, now):
        state = self.state(item)
        policy = getattr(policies, item['tier'])
        signals=self.signals.view(item,now=now)
        if signals is not None:
            anchor,dynamics=self.signals._read(item)
            level=signals['remaining']
            if policy.forget_threshold and not state['protected'] and level<=policy.forget_threshold:
                level=0.
            return {'reading_strategy':policy.strategy,'decay_factor':level,'memory_remaining':level,
                'decay_basis_time':anchor.since,'memory_strength':anchor.remaining,
                'memory_half_life_minutes':half_lives(anchor,dynamics)['meaning']/60,
                'memory_protected':bool(state['protected']),'meaning_strength':signals['significance'],
                'initial_meaning_strength':anchor.initial_significance,
                'detail_integrity':signals['detail'],'emotional_residue':signals['emotion'],
                'emotional_direction':signals['valence'],'consolidation':signals['consolidation'],
                'interference':signals['interference'],'dynamics_version':2,
                'curve_shape':'dual' if dynamics.curve_version==2 else 'exponential'}
        half_life = state['half_life_override'] if state['half_life_override'] is not None else policy.half_life_minutes
        level = remaining(state['strength'], state['since'], now, half_life)
        if policy.forget_threshold and not state['protected'] and level <= policy.forget_threshold:
            level = 0.
        return {'reading_strategy': policy.strategy, 'decay_factor': level,
                'decay_basis_time': state['since'], 'memory_remaining': level,
                'memory_strength': state['strength'], 'memory_half_life_minutes': half_life,
                'memory_protected': bool(state['protected'])}

    def last_reinforced(self,item):
        if self.signals._read(item):
            return self.store.rows('SELECT MAX(time) FROM memory_signal_events WHERE memory_id=? '
                'AND (effective_gain IS NULL OR effective_gain>0)',(item['id'],))[0][0]
        return self.store.rows('SELECT MAX(recalled_at) FROM memory_merge_recalls WHERE memory_id=?',
                               (item['id'],))[0][0]

    def observation(self,item,view,policies):
        """Management-only forecast; no reads become reinforcement or model instructions."""
        if item['tier'] not in TIERS:return {}
        policy=getattr(policies,item['tier'])
        last=self.last_reinforced(item)
        stamp=None
        if view['memory_protected']:status='protected'
        elif not policy.forget_threshold:status='disabled'
        elif view['memory_half_life_minutes'] is None:status='no_decay'
        else:
            strength=view['memory_strength'];threshold=policy.forget_threshold
            duration=max(0.,(math.log2(strength)-math.log2(threshold)))*view['memory_half_life_minutes']*60 if strength>threshold else 0.
            stamp=view['decay_basis_time']+duration
            signals=self.signals._read(item)
            if signals and signals[1].curve_version==2:
                stamp=threshold_time(*signals,threshold)
            status='reached' if strength<=threshold or view['memory_remaining']==0 else 'active'
            if not math.isfinite(stamp) or abs(stamp)>8.64e12:stamp=None;status='out_of_range'
            if (any(tag.startswith('persona_growth:') for tag in item.get('tags',[]))
                    and view['memory_remaining']==0 and strength>threshold and duration>0):
                # A source-invalid growth item is hidden independently of the time curve.
                status='unavailable';stamp=None
        return {'memory_retention_status':status,'memory_threshold_at':stamp,
                'memory_last_reinforced_at':last}

    def sync(self, before, after, policies, *, now, manual):
        require_coordinator()
        previous=self.signals._read(before) if before else None
        if before:
            self.signals.synchronize(before,after)
        if after['tier'] not in TIERS:
            return
        if before and before['tier'] in TIERS:
            state = self.state(before)
            if before['tier'] != after['tier']:
                state.update(strength=advance(*previous,now=now)['remaining'] if previous else
                             self.view(before, policies, now=now)['memory_remaining'],
                             since=now, half_life_override=None)
                if previous:
                    self.signals.reanchor(after,policy=getattr(policies,after['tier']).curve(),now=now)
        else:
            state = self.state(after)
            state['since'] = min(now, state['since'])
        self.store.connection.execute('''INSERT INTO memory_lifecycle VALUES(?,?,?,?,?)
            ON CONFLICT(memory_id) DO UPDATE SET strength=excluded.strength,since=excluded.since,
            half_life_override=excluded.half_life_override,protected=excluded.protected''',
            (after['id'], state['strength'], state['since'], state['half_life_override'],
             bool(state['protected'] or manual)))

    def reconfigure(self,policies,*,now):
        require_coordinator()
        for row in self.store.rows('SELECT m.* FROM memories m JOIN memory_signals s ON s.memory_id=m.id WHERE m.deleted=0'):
            if row['tier'] not in TIERS:
                continue
            policy=getattr(policies,row['tier']).curve()
            override=self.state(dict(row))['half_life_override']
            if override is not None:
                policy=replace(policy,base_half_life=max(policy.base_half_life,override*60))
            self.signals.reanchor(dict(row),policy=policy,now=now)

    def record_evidence(self, before, after, policies, archive, *, reinforcement_ids, now):
        require_coordinator()
        if after['tier'] not in TIERS:
            return
        canonical = archive.canonical_id
        baseline = (before or after)['source_message_ids']
        seen = {canonical(row['source_id']) for row in self.store.rows(
            'SELECT source_id FROM memory_evidence_seen WHERE memory_id=?', (after['id'],))}
        seen.update(canonical(mid) for mid in baseline)
        stamps = []
        for mid in {canonical(mid) for mid in reinforcement_ids} - seen:
            source = archive.get(mid)
            if (source and not source.recalled and source.group_id == after['group_id']
                    and not source.forwarded and not any(s.get('type') == 'forward' for s in source.segments)
                    and any(s.get('type') == 'text' and str(s.get('data', {}).get('text', '')).strip()
                            for s in source.segments) and math.isfinite(source.time)):
                stamps.append((min(now, source.time),mid))
        state = self.state(after)
        signals=self.signals._read(after)
        if signals:
            state['since']=signals[0].since
        policy = getattr(policies, after['tier'])
        spaced=self.store.get_state('memory_schema_version',1)==2
        for when,source_id in sorted(stamps) if policy.reinforcement_gain else []:
            if when < state['since'] and not (signals and spaced):
                continue
            if signals:
                self.signals.recall(after,event_id='source:'+source_id,now=when,gain=policy.reinforcement_gain,
                    spacing_half_life=policy.spacing_half_life_minutes*60 if spaced else None)
                state['since']=max(state['since'],when)
                continue
            level = self.view(after, policies, now=when)['memory_remaining']
            strengthened = level + (1 - level) * policy.reinforcement_gain
            self.store.connection.execute('UPDATE memory_lifecycle SET strength=?,since=? WHERE memory_id=?',
                                           (strengthened, when, after['id']))
            state['since'] = when
        seen.update(canonical(mid) for mid in after['source_message_ids'])
        self.store.connection.executemany('INSERT OR IGNORE INTO memory_evidence_seen VALUES(?,?)',
                                          [(after['id'], mid) for mid in seen])

    def recall_merge(self, item, policies, *, operation_id, count, now,source_times=None):
        require_coordinator()
        with self.store.transaction():
            self._recall_merge(item,policies,operation_id=operation_id,count=count,now=now,source_times=source_times)

    def _recall_merge(self, item, policies, *, operation_id, count, now,source_times=None):
        gain = getattr(policies, item['tier']).merge_recall_gain
        if not gain:
            return
        state = self.state(item)
        signals=self.signals._read(item)
        when = max(now, signals[0].since if signals else state['since'])
        level = self.view(item, policies, now=when)['memory_remaining']
        strengthened = 1 - (1 - level) * (1 - gain) ** count
        changed = self.store.connection.execute('INSERT OR IGNORE INTO memory_merge_recalls VALUES(?,?,?,?,?,?,?)',
            (operation_id, item['id'], count, gain, level, strengthened, when))
        if changed.rowcount:
            if signals:
                times=source_times if signals[1].curve_version==2 and source_times is not None else [when]*count
                for index,stamp in enumerate(sorted(times)):
                    self.signals.recall(item,event_id=f'merge:{operation_id}:{index}',now=min(now,stamp),gain=gain,
                        spacing_half_life=getattr(policies,item['tier']).spacing_half_life_minutes*60
                        if signals[1].curve_version==2 else None)
                actual=self.signals.view(item,now=when)['remaining']
                self.store.connection.execute('UPDATE memory_merge_recalls SET after_strength=? '
                    'WHERE operation_id=? AND memory_id=?',(actual,operation_id,item['id']))
                return
            self.store.connection.execute('UPDATE memory_lifecycle SET strength=?,since=? WHERE memory_id=?',
                (strengthened, when, item['id']))

    def distill(self, memory, proposal, target, *, now):
        """Called inside the canonical commit transaction; never replace the source fact."""
        require_coordinator()
        refs = proposal.distilled_from
        if not refs:
            return
        core_growth=target['tier']=='core' and proposal.source_group_id is not None
        if (target['tier'] not in {'secondary', 'important','long_term'} and not core_growth) or len({r.memory_id for r in refs}) != len(refs):
            raise ValueError('distillation requires distinct recent sources and a long-term target')
        policies = memory.settings.memory_policies
        if target['tier']=='long_term':
            signals=self.signals._read(target)
            significance=signals[0].initial_significance if signals else .5
            days=(policies.recent.distilled_secondary_days+
                  (policies.recent.distilled_important_days-policies.recent.distilled_secondary_days)*significance)
        else:
            days = getattr(policies.recent, 'distilled_' + ('important' if core_growth else target['tier']) + '_days')
        canonical = memory.archive.canonical_id
        evidence = {canonical(mid) for mid in target['source_message_ids']}
        for ref in refs:
            source = memory.get(ref.memory_id)
            if (not source or source['id'] == target['id'] or source['tier'] != 'recent'
                    or source['revision'] != ref.expected_revision or source['epoch'] > proposal.snapshot_epoch
                    or (not proposal.teaching_scope and (source['group_id'], source['subject_id'], source['persona_id']) !=
                       (proposal.source_group_id or target['group_id'], target['subject_id'], target['persona_id']))
                    or self.state(source)['protected'] or memory.has_recalled_source(source['source_message_ids'])
                    or not {canonical(mid) for mid in source['source_message_ids']} <= evidence):
                raise Conflict('distillation source changed, is protected, or left its evidence scope')
            view = self.view(source, policies, now=now)
            if view['memory_remaining'] == 0:
                raise Conflict('distillation source has expired')
            state = self.state(source)
            half_life = max(state['half_life_override'] or 0, days * 1440)
            signals=self.signals._read(source)
            if signals:
                _,dynamics=signals
                self.signals.reanchor(source,policy=replace(dynamics,
                    base_half_life=max(dynamics.base_half_life,half_life*60)),now=now)
            # Preserve the current remaining level; organizing is not a new experience.
            self.store.connection.execute('''INSERT INTO memory_lifecycle VALUES(?,?,?,?,0)
                ON CONFLICT(memory_id) DO UPDATE SET strength=excluded.strength,
                since=excluded.since,half_life_override=excluded.half_life_override''',
                (source['id'], view['memory_remaining'], max(now, state['since']), half_life))
            self.store.connection.execute('INSERT INTO memory_distillations VALUES(?,?,?,?,?)',
                (source['id'], source['revision'], target['id'], target['revision'], now))
