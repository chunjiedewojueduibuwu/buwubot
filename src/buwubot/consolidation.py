"""Revision-bound consolidation; originals survive in history and emotional anchors stay unchanged."""

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .conversations import message_context
from .coordinator import require_coordinator
from .memory_graph import EntityEdge, EntityMention, MemoryGraph
from .schedule import SHANGHAI
from .store import Conflict


class Relation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str
    target_id: str
    kind: Literal['duplicate', 'correction', 'related']
    summary: str = Field(min_length=1, max_length=500)


class Relations(BaseModel):
    model_config = ConfigDict(extra='forbid')
    relations: list[Relation] = Field(max_length=48)


class DistilledMemory(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_memory_ids: list[str] = Field(min_length=1, max_length=24)
    content: str = Field(min_length=1, max_length=6000)
    tier: Literal['secondary', 'important','long_term']
    significance: float | None = Field(default=None,ge=0,le=1,allow_inf_nan=False,strict=True)
    tags: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    growth_kind: Literal['interest', 'habit', 'view', 'impression'] | None = None
    target_core_id: str | None = None
    teaching_scope: Literal['owner','general'] | None = None
    teaching_source_ids: list[str] = Field(default_factory=list,max_length=24)


class MemoryMerge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_id: str
    source_memory_ids: list[str] = Field(min_length=1, max_length=24)
    confidence: float = Field(ge=0, le=1)


class ConsolidationPlan(Relations):
    entity_mentions: list[EntityMention] = Field(default_factory=list, max_length=48)
    entity_edges: list[EntityEdge] = Field(default_factory=list, max_length=48)
    distillations: list[DistilledMemory] = Field(default_factory=list, max_length=24)
    merges: list[MemoryMerge] = Field(default_factory=list, max_length=24)

    @classmethod
    def parse_material(cls, data):
        if len(json.dumps(data, ensure_ascii=False)) > 48000:
            raise ValueError('consolidation exceeds its material budget')
        return cls.model_validate(data)


class MemoryConsolidation:
    def __init__(self, memory):
        self.memory, self.store = memory, memory.store

    @staticmethod
    def initialize(store):
        store.connection.executescript('''
            CREATE TABLE IF NOT EXISTS memory_consolidation_seen(
                persona_id TEXT NOT NULL, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(persona_id,memory_id));
            CREATE TABLE IF NOT EXISTS memory_relations(
                id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, group_id TEXT NOT NULL,
                source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
                target_id TEXT NOT NULL, target_revision INTEGER NOT NULL,
                kind TEXT NOT NULL, summary TEXT NOT NULL, source_ids TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS memory_relations_source ON memory_relations(source_id);
            CREATE INDEX IF NOT EXISTS memory_relations_target ON memory_relations(target_id);
            CREATE TABLE IF NOT EXISTS memory_consolidation_applied(
                operation_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_merges(
                operation_id TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
                target_id TEXT NOT NULL, target_before_revision INTEGER NOT NULL,
                target_revision INTEGER NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(operation_id,source_id));
        ''')

    def _record(self, mid):
        item = self.memory.get(mid)
        cfg = self.memory.settings
        if (not item or item['group_id'] != cfg.group_id or item['tier'] == 'core'
                or item['persona_id'] not in {None, cfg.persona_id}
                or (self.memory.schema_version()==2 and self.memory.has_recalled_source(item['source_message_ids']))
                or self.memory.with_reading_policy(item).get('memory_remaining', 1) == 0):
            return None
        fields = ('id', 'group_id', 'persona_id', 'subject_id', 'content', 'tier', 'tags',
                  'source_message_ids', 'event_time', 'revision', 'confidence')
        result={key: item[key] for key in fields}
        if self.memory.schema_version()==2 and (signals:=self.memory.lifecycle.signals._read(item)):
            result['significance']=signals[0].initial_significance
        return result

    @staticmethod
    def fingerprint(snapshot):
        data = {key: value for key, value in snapshot.items() if key != 'fingerprint'}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def snapshot(self, *, mutation_since=None, teaching_ids=None):
        cfg = self.memory.settings
        rows = self.store.rows('''SELECT m.id FROM memories m
            LEFT JOIN memory_consolidation_seen s ON s.memory_id=m.id AND s.persona_id=?
            WHERE m.group_id=? AND m.deleted=0 AND m.tier<>'core'
            AND (m.persona_id IS NULL OR m.persona_id=?)
            AND (s.revision IS NULL OR s.revision<>m.revision)
            ORDER BY m.updated_at,m.id LIMIT 24''', (cfg.persona_id, cfg.group_id, cfg.persona_id))
        focus = [item for row in rows if (item := self._record(row['id']))]
        if teaching_ids is not None:
            if not 1<=len(teaching_ids)<=24 or len(set(teaching_ids))!=len(teaching_ids):
                raise ValueError('teaching recheck requires distinct memory IDs')
            focus=[self._record(mid) for mid in teaching_ids]
            if any(not item or item['tier']!='recent' for item in focus):
                raise ValueError('teaching recheck requires current scoped recent memories')
        if not focus:
            return None
        records = {item['id']: item for item in focus}
        # ponytail: bounded batches and existing lexical/person candidates; no new graph database.
        for item in focus:
            for candidate in self.memory.search_text(' '.join(item['tags']), group_id=cfg.group_id,
                    subjects=[item['subject_id']], limit=12):
                record = self._record(candidate['id'])
                if record and len(records) < 48:
                    records.setdefault(record['id'], record)
        barrier = self.store.rows('SELECT COALESCE(MAX(epoch),0) epoch FROM memory_manual_guards WHERE group_id=?',
                                  (cfg.group_id,))[0]['epoch']
        snapshot = {'group_id': cfg.group_id, 'persona_id': cfg.persona_id,
                    'focus_ids': [item['id'] for item in focus], 'records': list(records.values()),
                    'memory_epoch': max(barrier, *(self.memory.get(mid)['epoch'] for mid in records)),
                    'mutation_since': mutation_since,
                    'mergeable_ids': [item['id'] for item in records.values()
                        if mutation_since is not None and item['tier'] in {'recent', 'secondary', 'important','long_term'}
                        and not any(tag.startswith('persona_growth:') for tag in item['tags'])
                        and self.memory.get(item['id'])['created_at'] >= mutation_since
                        and not self.memory.lifecycle.state(self.memory.get(item['id']))['protected']],
                    'mutable_ids': [item['id'] for item in records.values()
                        if mutation_since is not None and item['tier'] == 'recent'
                        and self.memory.get(item['id'])['created_at'] >= mutation_since
                        and not self.memory.lifecycle.state(self.memory.get(item['id']))['protected']]}
        if self.memory.schema_version()==2:
            snapshot['memory_schema_version']=2
        snapshot['existing_entity_edges'] = MemoryGraph(self.memory).context(list(records.values()))
        snapshot['protected_persona'] = self.memory.settings.persona
        snapshot['protected_core'] = self.memory.core_context()
        snapshot['cooldown_growth_kinds'] = self.memory.growth_cooldowns()
        snapshot['growth_context'] = self.memory.growth_context()
        snapshot['core_records'] = [dict(row) for row in self.store.rows(
            "SELECT id,revision,content,tags,epoch FROM memories WHERE group_id=? AND persona_id=? AND tier='core' AND deleted=0",
            ('persona:core:'+cfg.persona_id,cfg.persona_id))]
        snapshot['owner_qq']=cfg.owner_id
        snapshot['owner_teaching_messages']=[{key:value for key,value in message_context(message).items()
            if key in {'id','user_id','group_id','time','text','reply_to','addressing'}} for mid in sorted({
            mid for item in snapshot['records'] for mid in item['source_message_ids']})
            if (message:=self.memory.archive.get(mid)) and not message.recalled
            and message.user_id==cfg.owner_id and message.text.strip()]
        if teaching_ids is not None:
            snapshot['teaching_only']=True
        snapshot['memory_epoch'] = max([snapshot['memory_epoch'],*[item['epoch'] for item in snapshot['core_records']]])
        return snapshot | {'fingerprint': self.fingerprint(snapshot)}

    def enqueue_teaching(self,queue,ids,*,mutation_since=None):
        snapshot=self.snapshot(mutation_since=mutation_since,teaching_ids=ids)
        if not snapshot:
            return None
        if not snapshot['owner_teaching_messages']:
            raise ValueError('teaching recheck has no actual owner evidence')
        if all(self.memory.teaching_message_used(message['id']) for message in snapshot['owner_teaching_messages']):
            return None
        return queue.enqueue('consolidate',{'group_id':snapshot['group_id'],
            'persona_id':snapshot['persona_id'],'snapshot':snapshot},
            dedupe_key='owner-teaching:'+snapshot['fingerprint'],priority=3,source_time=time.time())

    def validate(self, snapshot, *, check_records=True):
        cfg, _ = self.store.settings()
        if check_records and snapshot.get('memory_schema_version',1)!=self.memory.schema_version():
            raise Conflict('memory schema changed during consolidation')
        if (snapshot['group_id'] != self.memory.settings.group_id
                or snapshot['persona_id'] != self.memory.settings.persona_id
                or cfg.persona_id != snapshot['persona_id']
                or not cfg.allows_conversation(snapshot['group_id'])
                or snapshot['fingerprint'] != self.fingerprint(snapshot)
                or (check_records and any(self._record(item['id']) != item for item in snapshot['records']))):
            raise Conflict('memory consolidation sources changed or left scope')
        if ('protected_persona' in snapshot and (snapshot['protected_persona'] != cfg.persona
                or (check_records and snapshot.get('protected_core') != self.memory.core_context()))):
            raise Conflict('protected personality changed during consolidation')
        if 'owner_qq' in snapshot and snapshot['owner_qq']!=cfg.owner_id:
            raise Conflict('owner identity changed during consolidation')
        if check_records and 'core_records' in snapshot:
            current={row['id']:(row['revision'],row['content']) for row in self.store.rows(
                "SELECT id,revision,content FROM memories WHERE group_id=? AND persona_id=? AND tier='core' AND deleted=0",
                ('persona:core:'+cfg.persona_id,cfg.persona_id))}
            if current!={item['id']:(item['revision'],item['content']) for item in snapshot['core_records']}:
                raise Conflict('core memory versions changed during consolidation')

    def commit(self, snapshot, output, *, reviewed):
        require_coordinator()
        plan = ConsolidationPlan.parse_material(output)
        material=plan.model_dump()
        for draft in material['distillations']:
            if draft['significance'] is None:
                draft.pop('significance')  # Keep receipts for legacy already-applied plans.
            if draft['target_core_id'] is None:
                draft.pop('target_core_id')  # Preserve operation IDs from before growing core targets.
            if draft['teaching_scope'] is None:
                draft.pop('teaching_scope')
            if not draft['teaching_source_ids']:
                draft.pop('teaching_source_ids')
        operation_id = hashlib.sha256(json.dumps([snapshot['fingerprint'], material],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        applied = bool(self.store.rows('SELECT 1 FROM memory_consolidation_applied WHERE operation_id=?', (operation_id,)))
        self.validate(snapshot, check_records=not applied)
        if applied:
            return
        self.validate_plan(snapshot,output)
        relations = plan.relations
        if (relations or plan.distillations or plan.merges or plan.entity_mentions or plan.entity_edges) and reviewed is not True:
            raise ValueError('memory relations require independent review')
        records = {item['id']: item for item in snapshot['records']}
        prepared = self._relation_rows(snapshot, plan, records)
        merges = self._merge_candidates(snapshot, plan, records)
        self._entity_drafts(snapshot, plan)
        with self.store.transaction() as db:
            MemoryGraph(self.memory).apply([item.model_dump() for item in plan.entity_mentions],
                origin_model=self.memory.settings.primary_model, reviewed=reviewed or not plan.entity_mentions,
                edges=[edge.model_dump() for edge in plan.entity_edges])
            for draft in plan.distillations:
                self._distill(snapshot, draft, records)
            for target, sources in merges:
                self._merge(target, sources, snapshot, operation_id)
            # Update every survivor before tombstones create source barriers for later proposals.
            for _, sources in merges:
                for source in sources:
                    self.memory.delete(source['id'], expected_revision=source['revision'])
            db.executemany('INSERT OR IGNORE INTO memory_relations VALUES(?,?,?,?,?,?,?,?,?,?,?)', prepared)
            if not snapshot.get('teaching_only'):
                db.executemany('''INSERT INTO memory_consolidation_seen VALUES(?,?,?)
                    ON CONFLICT(persona_id,memory_id) DO UPDATE SET revision=excluded.revision''',
                    [(snapshot['persona_id'], mid, (self.memory.get(mid) or records[mid])['revision'])
                     for mid in snapshot['focus_ids']])
            db.execute('INSERT INTO memory_consolidation_applied VALUES(?,?)', (operation_id, time.time()))

    def validate_plan(self, snapshot, output):
        """Pure preflight; it shares checks with commit and cannot mark a focus processed."""
        self.validate(snapshot)
        plan = ConsolidationPlan.parse_material(output)
        records = {item['id']: item for item in snapshot['records']}
        errors = []
        if snapshot.get('teaching_only') and (plan.relations or plan.merges or plan.entity_mentions or plan.entity_edges
                or any(not draft.teaching_scope for draft in plan.distillations)):
            errors.append('teaching-only recheck cannot perform ordinary consolidation')
        growth_kinds=[draft.growth_kind for draft in plan.distillations if draft.growth_kind and not draft.teaching_scope]
        if len(growth_kinds)!=len(set(growth_kinds)):
            errors.append('growth batch may contain only one change per kind during cooldown')
        for index, relation in enumerate(plan.relations):
            try:
                self._relation_rows(snapshot, plan.model_copy(update={'relations': [relation]}), records)
            except ValueError as exc:
                errors.append(f'relations[{index}]: {exc}')
        try:
            self._merge_candidates(snapshot, plan, records)
        except ValueError as exc:
            errors.append(f'merges: {exc}')
        for index, draft in enumerate(plan.distillations):
            try:
                self._distillation_proposal(snapshot, draft, records)
            except ValueError as exc:
                errors.append(f'distillations[{index}]: {exc}')
        try:
            self._entity_drafts(snapshot, plan)
        except ValueError as exc:
            errors.append(f'entity_mentions: {exc}')
        if errors:
            raise ValueError('; '.join(errors))
        return plan

    def _entity_drafts(self, snapshot, plan):
        records = {item['id']: item for item in snapshot['records']}
        if any(item.memory_id not in snapshot['focus_ids']
               or item.memory_id not in records or records[item.memory_id]['revision'] != item.expected_revision
               for item in plan.entity_mentions):
            raise ValueError('entity mentions require current focus source revisions')
        known_edges = {edge['id'] for edge in snapshot.get('existing_entity_edges', [])}
        if any(edge.supersedes and edge.supersedes not in known_edges for edge in plan.entity_edges):
            raise ValueError('superseded edge must be supplied by the current snapshot')
        if any(not set(edge.evidence_memory_ids) <= set(records) for edge in plan.entity_edges):
            raise ValueError('entity edge evidence must be in the current snapshot')
        graph = MemoryGraph(self.memory)
        mentions = graph.prepare([item.model_dump() for item in plan.entity_mentions])
        graph.prepare_edges(mentions, [edge.model_dump() for edge in plan.entity_edges])
        return mentions

    @staticmethod
    def model_context(snapshot):
        focus = set(snapshot['focus_ids'])
        distill = set(snapshot.get('mutable_ids', []))
        merge = set(snapshot.get('mergeable_ids', []))
        groups = {}
        merge_groups = {}
        for item in snapshot['records']:
            identity = (item['group_id'], item['subject_id'], item['persona_id'])
            if item['id'] in distill:
                groups.setdefault(identity, []).append(item['id'])
            if item['id'] in merge:
                merge_groups.setdefault(identity, []).append(item['id'])
        roots = {'relation': list(snapshot['focus_ids']),
                 'distillation': [mid for mid in snapshot['focus_ids'] if mid in distill],
                 'merge': [mid for mid in snapshot['focus_ids'] if mid in merge],
                 'entity': list(snapshot['focus_ids'])}
        projected = []
        for item in snapshot['records']:
            local_time = None
            if item.get('event_time') is not None:
                try:
                    local_time = datetime.fromtimestamp(item['event_time'], SHANGHAI).isoformat()
                except (ValueError, OverflowError, OSError):
                    pass
            projected.append(item | {'local_time': local_time, 'focus': item['id'] in focus,
                'can_distill': item['id'] in distill and item['id'] in focus,
                'can_support_distillation': item['id'] in distill, 'can_merge': item['id'] in merge})
        return snapshot | {'operation_roots': roots,
            'distillation_groups': [ids for ids in groups.values() if focus.intersection(ids)],
            'merge_groups': [ids for ids in merge_groups.values() if focus.intersection(ids)], 'records': projected}

    @staticmethod
    def review_context(snapshot, output):
        context = MemoryConsolidation.model_context(snapshot)
        records = {item['id']: item for item in context['records']}
        evidence = {}
        for key, drafts in output.items():
            if key not in {'relations', 'distillations', 'merges', 'entity_mentions', 'entity_edges'}:
                continue
            groups = []
            for index, draft in enumerate(drafts):
                if key == 'relations':
                    ids = [draft['source_id'], draft['target_id']]
                elif key == 'merges':
                    ids = [draft['target_id'], *draft['source_memory_ids']]
                elif key == 'entity_mentions':
                    ids = [draft['memory_id']]
                else:
                    ids = draft.get('source_memory_ids', draft.get('evidence_memory_ids', []))
                group = {'index': index, 'records': [records[mid] for mid in dict.fromkeys(ids)]}
                if key == 'merges':
                    group['effective_target_tier'] = max((records[mid]['tier'] for mid in ids),
                        key={'recent': 1, 'secondary': 2, 'important': 3,'long_term':3}.get)
                groups.append(group)
            evidence[key] = groups
        used = {item['id'] for groups in evidence.values() for group in groups for item in group['records']}
        context = context | {'records': [item for item in context['records'] if item['id'] in used],
            'focus_ids': [mid for mid in snapshot['focus_ids'] if mid in used]}
        return {'snapshot': context, 'proposal': output, 'operation_evidence': evidence,
                'structure_validated': True}

    @staticmethod
    def review_batches(snapshot, output):
        plan = ConsolidationPlan.parse_material(output).model_dump()
        for key, drafts in plan.items():
            for start in range(0, len(drafts), 4):
                partial = {name: [] for name in plan}
                partial[key] = drafts[start:start + 4]
                if key == 'merges':
                    pairs = {frozenset((draft['target_id'], mid)) for draft in partial[key]
                             for mid in draft['source_memory_ids']}
                    partial['relations'] = [r for r in plan['relations'] if r['kind'] == 'duplicate'
                        and frozenset((r['source_id'], r['target_id'])) in pairs]
                elif key == 'entity_edges':
                    indices = sorted({draft[field] for draft in partial[key]
                                      for field in ('source_index', 'target_index')})
                    remap = {old: new for new, old in enumerate(indices)}
                    partial['entity_mentions'] = [plan['entity_mentions'][index] for index in indices]
                    partial[key] = [draft | {'source_index': remap[draft['source_index']],
                        'target_index': remap[draft['target_index']]} for draft in partial[key]]
                packet = MemoryConsolidation.review_context(snapshot, partial)
                packet['review_unit'] = key
                yield packet

    def _relation_rows(self, snapshot, plan, records):
        prepared = []
        for relation in plan.relations:
            source, target = records.get(relation.source_id), records.get(relation.target_id)
            if (not source or not target or source['id'] == target['id']
                    or not {source['id'], target['id']} & set(snapshot['focus_ids'])):
                raise ValueError('relation requires distinct known sources and a current focus')
            if relation.kind in {'duplicate', 'correction'} and (
                    (source['persona_id'], source['subject_id']) != (target['persona_id'], target['subject_id'])):
                raise ValueError('a correction or duplicate cannot change the subject identity')
            sources = sorted(set(source['source_message_ids'] + target['source_message_ids']))
            key = hashlib.sha256(json.dumps([snapshot['persona_id'], source['id'], source['revision'],
                target['id'], target['revision'], relation.kind], ensure_ascii=False).encode()).hexdigest()
            prepared.append((key, snapshot['persona_id'], snapshot['group_id'], source['id'], source['revision'],
                target['id'], target['revision'], relation.kind, relation.summary, json.dumps(sources), time.time()))
        return prepared

    def _merge_candidates(self, snapshot, plan, records):
        used = {mid for draft in plan.distillations for mid in draft.source_memory_ids}
        duplicates = {frozenset((r.source_id, r.target_id)) for r in plan.relations if r.kind == 'duplicate'}
        rank = {'recent': 1, 'secondary': 2, 'important': 3,'long_term':3}
        prepared = []
        for draft in plan.merges:
            ids = [draft.target_id, *draft.source_memory_ids]
            if any(any(tag.startswith('persona_growth:') for tag in records.get(mid,{}).get('tags',[])) for mid in ids):
                raise ValueError('growth memories keep their independent lineage; use reviewed edits or relations instead of merge')
            overlap = set(ids) & used
            if overlap:
                raise ValueError('merge/distillation overlap; choose one operation for sources: ' + ','.join(sorted(overlap)))
            if (len(set(ids)) != len(ids) or set(ids) & used
                    or not set(ids) <= set(snapshot.get('mergeable_ids', []))
                    or not set(ids) & set(snapshot['focus_ids']) or draft.confidence < .75):
                raise ValueError('merge requires distinct confident mutable sources and a focus')
            target, *sources = [records[mid] for mid in ids]
            if (target['tier'] not in rank or any(
                    (s['group_id'], s['subject_id'], s['persona_id']) !=
                    (target['group_id'], target['subject_id'], target['persona_id'])
                    or s['tier'] not in rank
                    or frozenset((s['id'], target['id'])) not in duplicates for s in sources)):
                raise ValueError('merge requires explicit duplicates of the same identity without a tier downgrade')
            if any(self.memory.lifecycle.state(self.memory.get(mid))['protected']
                   or self.memory.has_recalled_source(records[mid]['source_message_ids']) for mid in ids):
                raise Conflict('merge source was protected or recalled')
            used.update(ids)
            prepared.append((target, sources))
        return prepared

    def _merge(self, target, sources, snapshot, operation_id):
        from .memory import MemoryProposal

        evidence = {self.memory.archive.canonical_id(mid)
                    for item in [target, *sources] for mid in item['source_message_ids']}
        p = MemoryProposal(group_id=target['group_id'], persona_id=target['persona_id'], subject_id=target['subject_id'],
            memory_id=target['id'], expected_revision=target['revision'], content=target['content'],
            tier=max((item['tier'] for item in [target, *sources]), key={'recent': 1, 'secondary': 2, 'important': 3,'long_term':3}.get),
            memory_schema_version=self.memory.schema_version(),
            significance=max(self.memory.lifecycle.signals._read(self.memory.get(item['id']))[0].initial_significance
                for item in [target,*sources]) if self.memory.schema_version()==2 else None,
            tags=list(dict.fromkeys(tag for item in [target, *sources] for tag in item['tags'])),
            confidence=target['confidence'], event_time=target['event_time'],
            source_message_ids=sorted(evidence), snapshot_epoch=snapshot['memory_epoch'],
            origin_model=self.memory.settings.primary_model)
        self.memory.commit(p, reviewed=True, source_snapshots=[{'id': mid, 'group_id': target['group_id']} for mid in evidence])
        seen={self.memory.archive.canonical_id(mid) for mid in target['source_message_ids']}
        source_times=[]
        for source in sources:
            ids={self.memory.archive.canonical_id(mid) for mid in source['source_message_ids']}
            if ids-seen and source['event_time'] is not None:
                source_times.append(source['event_time'])
            seen.update(ids)
        self.memory.lifecycle.recall_merge(self.memory.get(target['id']), self.memory.settings.memory_policies,
            operation_id=operation_id, count=len(sources), now=time.time(),source_times=source_times)
        for source in sources:
            prior = self.store.rows('SELECT source_id FROM memory_evidence_seen WHERE memory_id=?', (source['id'],))
            self.store.connection.executemany('INSERT OR IGNORE INTO memory_evidence_seen VALUES(?,?)',
                [(target['id'], self.memory.archive.canonical_id(row['source_id'])) for row in prior])
            self.store.connection.execute('INSERT INTO memory_merges VALUES(?,?,?,?,?,?,?)',
                (operation_id, source['id'], source['revision'], target['id'], target['revision'],
                 self.memory.get(target['id'])['revision'], time.time()))

    def _distill(self, snapshot, draft, records):
        p = self._distillation_proposal(snapshot, draft, records)
        self.memory.commit(p, reviewed=True,
            source_snapshots=[{'id': mid, 'group_id': p.source_group_id or p.group_id} for mid in p.source_message_ids])

    def _distillation_proposal(self, snapshot, draft, records):
        if (self.memory.schema_version()==2 and not draft.growth_kind
                and (draft.tier!='long_term' or draft.significance is None)):
            raise ValueError('V2 distillation requires long_term and explicit significance')
        from .memory import MemoryProposal, core_scope

        ids = draft.source_memory_ids
        if (draft.confidence < .75 or len(set(ids)) != len(ids) or not set(ids) <= set(snapshot.get('mutable_ids', []))
                or not set(ids) & set(snapshot['focus_ids'])):
            blocked = sorted(set(ids) - set(snapshot.get('mutable_ids', [])))
            raise ValueError('distillation requires distinct mutable snapshot sources and a focus; '
                             f'not_mutable={blocked}; has_focus={bool(set(ids) & set(snapshot["focus_ids"]))}')
        sources = [records[mid] for mid in ids]
        identity = {(s['group_id'], s['subject_id'], s['persona_id']) for s in sources}
        if len(identity) != 1 or any(s['tier'] != 'recent' for s in sources):
            raise ValueError('distillation cannot combine different identities or non-recent memories')
        group, subject, persona = identity.pop()
        if any(tag.startswith(('persona_growth:','owner_teaching:')) for tag in draft.tags):
            raise ValueError('growth markers are assigned only by the program')
        if draft.teaching_source_ids and not draft.teaching_scope:
            raise ValueError('teaching evidence requires its explicit scope')
        if draft.teaching_scope and draft.growth_kind!='habit':
            raise ValueError('teaching can change expression habits only')
        if draft.teaching_scope:
            if snapshot.get('owner_qq')!=self.memory.settings.owner_id:
                raise ValueError('teaching requires a trusted owner snapshot')
            persona=snapshot['persona_id'];subject=''
        if draft.growth_kind and not draft.teaching_scope:
            if draft.growth_kind in self.memory.growth_cooldowns():
                raise ValueError('growth cooldown is still active')
            if 'protected_persona' not in snapshot or 'protected_core' not in snapshot:
                raise ValueError('growth requires a current protected-personality snapshot')
            events={source['event_time'] for source in sources if source.get('event_time') is not None}
            independent=[{self.memory.archive.canonical_id(mid) for mid in source['source_message_ids']}
                         for source in sources]
            if (subject or persona != snapshot['persona_id'] or len(events)<2
                    or len(independent)<2 or any(left & right for index,left in enumerate(independent)
                                               for right in independent[index+1:])):
                raise ValueError('growth requires independent personal events from the current scoped persona')
        evidence = sorted({self.memory.archive.canonical_id(mid)
                           for source in sources for mid in source['source_message_ids']})
        target=None
        if draft.target_core_id:
            target=next((item for item in snapshot.get('core_records',[]) if item['id']==draft.target_core_id),None)
            if not draft.growth_kind or target is None:
                raise ValueError('growth target must be a current core memory in this snapshot')
        p = MemoryProposal(group_id=core_scope(persona) if draft.growth_kind else group,
            subject_id=subject, persona_id=persona,
            content=draft.content, tier='core' if draft.growth_kind else draft.tier,
            significance=draft.significance,memory_schema_version=self.memory.schema_version(),
            memory_id=target['id'] if target else None,
            expected_revision=target['revision'] if target else None,
            source_group_id=group if draft.growth_kind else None,
            teaching_scope=draft.teaching_scope,teaching_source_ids=draft.teaching_source_ids,
            tags=[*draft.tags, *([f'persona_growth:{draft.growth_kind}'] if draft.growth_kind else []),
                  *(['owner_teaching:'+draft.teaching_scope] if draft.teaching_scope else [])],
            confidence=draft.confidence,
            source_message_ids=evidence, snapshot_epoch=snapshot['memory_epoch'],
            origin_model=self.memory.settings.primary_model,
            distilled_from=[{'memory_id': source['id'], 'expected_revision': source['revision']}
                            for source in sources])
        if draft.teaching_scope:
            self.memory.validate_teaching(p,sources)
        return p

    def relations(self, mid, *, max_chars=16000, query=""):
        if max_chars < 2 or not self._record(mid):
            return []
        remaining = min(max_chars, 16000) - 2
        cfg = self.memory.settings
        terms = list(dict.fromkeys(re.findall(r'[\w\u4e00-\u9fff]+', query.casefold())))[:12]
        result = []
        for row in self.store.rows('''SELECT r.* FROM memory_relations r
                JOIN memories s ON s.id=r.source_id AND s.revision=r.source_revision AND s.deleted=0
                JOIN memories t ON t.id=r.target_id AND t.revision=r.target_revision AND t.deleted=0
                WHERE r.persona_id=? AND r.group_id=? AND (r.source_id=? OR r.target_id=?)
                ORDER BY (SELECT COUNT(*) FROM json_each(?) q WHERE instr(lower(
                    CASE WHEN r.source_id=? THEN t.content ELSE s.content END),q.value)>0) DESC,
                CASE r.kind WHEN 'correction' THEN 0 WHEN 'duplicate' THEN 1 ELSE 2 END,
                json_array_length(CASE WHEN r.source_id=? THEN t.source_ids ELSE s.source_ids END) DESC,
                r.created_at DESC,r.id LIMIT 48''',
                (cfg.persona_id, cfg.group_id, mid, mid, json.dumps(terms), mid, mid)):
            source, target = self._record(row['source_id']), self._record(row['target_id'])
            if source and target and (source['revision'], target['revision']) == (
                    row['source_revision'], row['target_revision']):
                item = dict(row) | {'source_message_ids': json.loads(row['source_ids']),
                    'source_content': source['content'], 'target_content': target['content'],
                    'source_event_time': source['event_time'], 'target_event_time': target['event_time']}
                cost = len(json.dumps(item, ensure_ascii=False)) + (2 if result else 0)
                if cost <= remaining:
                    result.append(item)
                    remaining -= cost
        return result

    def neighbors(self, seed_ids, *, max_chars=12000, now=None, max_hops=None, query=""):
        """Bounded BFS over reviewed evidence; paths are leads, not inferred facts."""
        now = time.time() if now is None else now
        depth = self.memory.settings.memory_relation_depth if max_hops is None else max_hops
        if depth not in (1, 2, 3):
            raise ValueError('relation depth must be 1, 2 or 3')
        seeds = list(dict.fromkeys(seed_ids))
        seen, result = set(seeds), []
        budget = max(0, min(max_chars, 12000))
        frontier = [(seed, seed, []) for seed in seeds]
        deferred = {}
        for hop in range(depth):
            next_frontier = []
            hop_budget, hop_used = budget // (depth - hop), 0
            for source, root, steps in frontier:
                if (not self._record(source) or self.memory.with_reading_policy(
                        self.memory.get(source), now=now).get('memory_remaining', 1) <= 0):
                    continue
                for edge in self.relations(source, query=query):
                    mid = edge['target_id'] if edge['source_id'] == source else edge['source_id']
                    if mid in seen:
                        continue
                    current = self.memory.get(mid)
                    if not current or self.memory.with_reading_policy(current, now=now).get('memory_remaining', 1) <= 0:
                        continue
                    path = steps + [{'source_id': source, 'target_id': mid, 'relation_id': edge['id']}]
                    candidate = current | {'relation_recall': {'seed_id': root, 'relation_id': edge['id'],
                        'kind': edge['kind'], 'steps': path}}
                    cost = len(json.dumps(candidate, ensure_ascii=False)) + 2
                    if cost > budget:
                        continue
                    if cost > hop_budget - hop_used or len(next_frontier) >= 40:
                        if len(deferred) < 120:
                            deferred.setdefault(mid, (candidate, cost))
                        continue
                    result.append(candidate)
                    seen.add(mid)
                    deferred.pop(mid, None)
                    next_frontier.append((mid, root, path))
                    budget -= cost
                    hop_used += cost
            frontier = next_frontier
            if not frontier:
                break
        # Reuse unspent deep-hop space when a branch terminates; do not starve leaf neighborhoods.
        for mid, (candidate, cost) in deferred.items():
            if mid not in seen and cost <= budget:
                result.append(candidate)
                seen.add(mid)
                budget -= cost
        return result

    def path_current(self, path):
        steps = path.get('steps') or [path | {'source_id': path['seed_id']}]
        return all(any(edge['id'] == step['relation_id'] for edge in self.relations(step['source_id']))
                   for step in steps)
