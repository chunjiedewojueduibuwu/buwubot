"""Local, rebuildable entity annotations bound to reviewed memory revisions."""

import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .coordinator import require_coordinator
from .store import Conflict


def normalize_name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


class EntityMention(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    memory_id: str
    expected_revision: int = Field(ge=1)
    node_type: Literal['person', 'place', 'organization', 'event', 'emotion', 'topic',
                       'work', 'activity', 'product', 'food']
    name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    discriminator: str = Field(default='', max_length=120)
    confidence: float = Field(ge=.75, le=1)


class EntityEdge(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    source_index: int = Field(ge=0, lt=48)
    target_index: int = Field(ge=0, lt=48)
    relation_type: Literal['mentions', 'participates_in', 'located_at', 'evokes', 'role_of', 'relates_to']
    supersedes: str | None = Field(default=None, min_length=1, max_length=64)
    evidence_memory_ids: list[str] = Field(min_length=1, max_length=24)
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=.75, le=1)


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['search', 'neighbors', 'timeline', 'network'] = 'search'
    entities: list[str] = Field(min_length=1, max_length=4)
    depth: int = Field(default=3, ge=1, le=3)


    @field_validator('entities')
    @classmethod
    def entity_names(cls, names):
        if any(not name.strip() or len(name) > 120 for name in names):
            raise ValueError('graph query entity exceeds its budget')
        return names


class MemoryGraph:
    def __init__(self, memory):
        self.memory, self.store = memory, memory.store

    @staticmethod
    def initialize(store):
        store.connection.executescript("""
            CREATE TABLE IF NOT EXISTS memory_graph_entities(
                id TEXT PRIMARY KEY, group_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                node_type TEXT NOT NULL, identity_key TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS memory_graph_identity ON memory_graph_entities(
                group_id,persona_id,node_type,identity_key);
            CREATE TABLE IF NOT EXISTS memory_graph_mentions(
                entity_id TEXT NOT NULL, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                name TEXT NOT NULL, normalized_name TEXT NOT NULL, is_primary INTEGER NOT NULL,
                confidence REAL NOT NULL, PRIMARY KEY(entity_id,memory_id,revision,normalized_name));
            CREATE INDEX IF NOT EXISTS memory_graph_names ON memory_graph_mentions(normalized_name);
            CREATE INDEX IF NOT EXISTS memory_graph_sources ON memory_graph_mentions(memory_id,revision);
            CREATE TABLE IF NOT EXISTS memory_graph_edges(
                id TEXT PRIMARY KEY, group_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation_type TEXT NOT NULL,
                summary TEXT NOT NULL, confidence REAL NOT NULL, valid_from REAL, evidence TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS memory_graph_edge_source ON memory_graph_edges(source_id);
            CREATE INDEX IF NOT EXISTS memory_graph_edge_target ON memory_graph_edges(target_id);
            CREATE TABLE IF NOT EXISTS memory_graph_supersessions(
                old_edge_id TEXT PRIMARY KEY, new_edge_id TEXT NOT NULL, valid_to REAL NOT NULL);
        """)

    def _record(self, mid):
        from .consolidation import MemoryConsolidation
        return MemoryConsolidation(self.memory)._record(mid)

    def apply(self, drafts, *, origin_model, reviewed, edges=()):
        require_coordinator()
        cfg = self.memory.settings
        active, _ = self.store.settings()
        if origin_model != cfg.primary_model or reviewed is not True:
            raise ValueError('entity annotations require DS adoption and independent review')
        if active.persona_id != cfg.persona_id or not active.allows_conversation(cfg.group_id):
            raise Conflict('entity annotation scope is no longer authorized')
        if len(drafts) > 48 or len(json.dumps(drafts, ensure_ascii=False)) > 48000:
            raise ValueError('entity annotation material exceeds its budget')
        prepared = self.prepare(drafts)
        prepared_edges = self.prepare_edges(prepared, edges)
        with self.store.transaction() as db:
            for eid, draft, key, names in prepared:
                db.execute('INSERT OR IGNORE INTO memory_graph_entities VALUES(?,?,?,?,?)',
                           (eid, cfg.group_id, cfg.persona_id, draft.node_type, key))
                db.executemany('INSERT OR IGNORE INTO memory_graph_mentions VALUES(?,?,?,?,?,?,?)',
                    [(eid, draft.memory_id, draft.expected_revision, name, normalize_name(name), int(n == 0),
                      draft.confidence) for n, name in enumerate(names)])
            db.executemany('INSERT OR IGNORE INTO memory_graph_edges VALUES(?,?,?,?,?,?,?,?,?,?)', prepared_edges)
            for raw, row in zip(edges, prepared_edges, strict=True):
                old_id = EntityEdge.model_validate(raw).supersedes
                if old_id:
                    db.execute('INSERT OR IGNORE INTO memory_graph_supersessions VALUES(?,?,?)',
                               (old_id, row[0], row[8]))

    def prepare(self, drafts):
        """Validate source annotations without writes; reused by the consolidation preflight."""
        cfg = self.memory.settings
        prepared = []
        for raw in drafts:
            draft = EntityMention.model_validate(raw)
            item = self._record(draft.memory_id)
            if not item or item['revision'] != draft.expected_revision:
                raise Conflict('entity evidence changed or left scope')
            names = list(dict.fromkeys([draft.name, *draft.aliases]))
            evidence = normalize_name(item['content'] + ' ' + ' '.join(item['tags']))
            if any(not normalize_name(name) or len(name) > 120 or normalize_name(name) not in evidence for name in names):
                raise ValueError('entity names must occur in the bound memory evidence')
            if draft.discriminator and normalize_name(draft.discriminator) not in evidence:
                raise ValueError('entity discriminator requires bound source evidence')
            if draft.node_type == 'person':
                if not re.fullmatch(r'[0-9]{1,20}', item['subject_id']):
                    raise ValueError('person entities require the memory subject QQ identity')
                key = 'qq:' + item['subject_id']
            else:
                key = json.dumps([normalize_name(draft.name), normalize_name(draft.discriminator)], ensure_ascii=False)
            identity = [cfg.group_id, cfg.persona_id, draft.node_type, key]
            eid = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            prepared.append((eid, draft, key, names))
        return prepared

    def prepare_edges(self, mentions, drafts):
        if len(drafts) > 48:
            raise ValueError('entity edge material exceeds budget')
        cfg = self.memory.settings
        result, closing = [], set()
        for raw in drafts:
            edge = EntityEdge.model_validate(raw)
            if max(edge.source_index, edge.target_index) >= len(mentions):
                raise ValueError('entity edge indices must refer to current mentions')
            source, target = mentions[edge.source_index], mentions[edge.target_index]
            target_types = {'participates_in': {'event', 'activity'}, 'located_at': {'place'},
                            'evokes': {'emotion'}, 'role_of': {'organization', 'activity', 'work'}}
            if (edge.relation_type in target_types and target[1].node_type not in target_types[edge.relation_type]
                    or edge.relation_type == 'role_of' and source[1].node_type != 'person'):
                raise ValueError('entity relation endpoint type does not match its meaning')
            ids = set(edge.evidence_memory_ids)
            if source[0] == target[0] or not {source[1].memory_id, target[1].memory_id} <= ids:
                raise ValueError('entity edge requires distinct endpoints and their bound evidence')
            evidence = [self._record(mid) for mid in sorted(ids)]
            if not all(evidence):
                raise Conflict('entity edge evidence changed or left scope')
            revisions = [[item['id'], item['revision']] for item in evidence]
            event_times = [item['event_time'] for item in evidence if item['event_time'] is not None]
            valid_from = max(event_times) if event_times else None
            identity = [cfg.group_id, cfg.persona_id, source[0], target[0], edge.relation_type, revisions]
            eid = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            row = (eid, cfg.group_id, cfg.persona_id, source[0], target[0], edge.relation_type,
                   edge.summary, edge.confidence, valid_from, json.dumps(revisions))
            if edge.supersedes:
                if edge.supersedes in closing:
                    raise ValueError('same batch cannot replace one old edge twice')
                closing.add(edge.supersedes)
                self.validate_supersession(edge.supersedes, row)
            result.append(row)
        return result

    def validate_supersession(self, old_id, replacement):
        rows = self.store.rows('SELECT * FROM memory_graph_edges WHERE id=?', (old_id,))
        if not rows or not self.edge_current(rows[0], include_history=True):
            raise Conflict('superseded edge is stale or outside the current scope')
        old = rows[0]
        if (old_id == replacement[0] or old['source_id'] != replacement[3]
                or old['relation_type'] != replacement[5]):
            raise ValueError('supersession must preserve the source entity and predicate')
        if replacement[8] is None or old['valid_from'] is not None and replacement[8] < old['valid_from']:
            raise ValueError('replacement time must be known and not precede the old relation')
        closed = self.store.rows('SELECT new_edge_id FROM memory_graph_supersessions WHERE old_edge_id=?', (old_id,))
        if closed and closed[0]['new_edge_id'] != replacement[0]:
            raise Conflict('old relation already has a different replacement')

    def entity_relations(self, name, *, include_history=False):
        cfg = self.memory.settings
        anchors = [node['id'] for node in self.lookup(name)]
        if not anchors:
            return []
        placeholders = ','.join('?' for _ in anchors)
        rows = self.store.rows(f"""SELECT * FROM memory_graph_edges WHERE group_id=? AND persona_id=?
            AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
            ORDER BY valid_from IS NULL,valid_from,id LIMIT 80""", (cfg.group_id, cfg.persona_id, *anchors, *anchors))
        result = []
        for row in rows:
            if view := self.edge_view(row['id'], include_history=include_history):
                result.append(view)
        return result

    def edge_view(self, edge_id, *, include_history=False):
        rows = self.store.rows('SELECT * FROM memory_graph_edges WHERE id=?', (edge_id,))
        if not rows or not self.edge_current(rows[0], include_history=include_history):
            return None
        row = rows[0]
        closure = self.store.rows('SELECT valid_to,new_edge_id FROM memory_graph_supersessions WHERE old_edge_id=?',
                                  (edge_id,))
        return dict(row) | {'source_memory_ids': [mid for mid, _ in json.loads(row['evidence'])],
            'valid_to': closure[0]['valid_to'] if closure else None,
            'superseded_by': closure[0]['new_edge_id'] if closure else None,
            'is_current': not bool(closure)}

    def edge_current(self, edge, *, include_history=False):
        cfg = self.memory.settings
        active, _ = self.store.settings()
        if (edge['group_id'] != cfg.group_id or edge['persona_id'] != cfg.persona_id
                or active.persona_id != cfg.persona_id or not active.allows_conversation(cfg.group_id)):
            return False
        if not include_history and self.store.rows(
                'SELECT 1 FROM memory_graph_supersessions WHERE old_edge_id=?', (edge['id'],)):
            return False
        return all((item := self._record(mid)) and item['revision'] == revision
                   for mid, revision in json.loads(edge['evidence']))

    def lookup(self, name, *, limit=40):
        cfg = self.memory.settings
        active, _ = self.store.settings()
        if active.persona_id != cfg.persona_id or not active.allows_conversation(cfg.group_id):
            return []
        rows = self.store.rows("""SELECT e.*,a.memory_id,a.revision,a.name,a.is_primary
            FROM memory_graph_entities e JOIN memory_graph_mentions a ON a.entity_id=e.id
            JOIN memories m ON m.id=a.memory_id AND m.revision=a.revision AND m.deleted=0
            WHERE e.group_id=? AND e.persona_id=? AND a.normalized_name=?
            ORDER BY a.is_primary DESC,m.event_time DESC,e.id LIMIT ?""",
            (cfg.group_id, cfg.persona_id, normalize_name(name), max(0, min(limit, 200))))
        result = {}
        for row in rows:
            item = self._record(row['memory_id'])
            if not item or item['revision'] != row['revision']:
                continue
            node = result.setdefault(row['id'], {key: row[key] for key in
                ('id', 'group_id', 'persona_id', 'node_type', 'identity_key')})
            node.setdefault('source_memories', []).append(item)
        return list(result.values())

    def recall(self, query, *, max_chars=12000):
        terms = list(dict.fromkeys([query, *re.findall(r'[\w\u4e00-\u9fff]+', query)]))[:13]
        result, seen = [], set()
        budget = min(max_chars, 12000)
        for term in terms:
            for node in self.lookup(term):
                for evidence in node['source_memories']:
                    mid = evidence['id']
                    if mid in seen:
                        continue
                    item = self.memory.get(mid) | {'entity_recall': {
                        'entity_id': node['id'], 'identity_key': node['identity_key'],
                        'matched_name': term, 'memory_revision': evidence['revision']}}
                    cost = len(json.dumps(item, ensure_ascii=False)) + 2
                    if cost <= budget:
                        result.append(item)
                        seen.add(mid)
                        budget -= cost
        return result

    def timeline(self, name):
        records = {item['id']: item for node in self.lookup(name) for item in node['source_memories']}
        # Unknown event time is explicit; indexing time never becomes the time of an experience.
        return sorted(records.values(), key=lambda item: (item['event_time'] is None,
                                                          item['event_time'] or 0, item['id']))

    def paths(self, source_name, target_name, *, max_hops=3, max_chars=12000):
        from .consolidation import MemoryConsolidation
        seeds = [item['id'] for item in self.timeline(source_name)]
        targets = {item['id'] for item in self.timeline(target_name)}
        typed = self.entity_paths(source_name, target_name, max_hops=max_hops, max_chars=max_chars)
        if typed:
            return typed
        return [item for item in MemoryConsolidation(self.memory).neighbors(
            seeds, max_hops=max_hops, max_chars=max_chars, query=target_name) if item['id'] in targets]

    def query(self, request, *, max_chars=12000):
        from .consolidation import MemoryConsolidation
        spec = GraphQuery.model_validate(request)
        if any(not name.strip() or len(name) > 120 for name in spec.entities):
            raise ValueError('graph query entity exceeds its budget')
        result = []
        if spec.mode == 'network' and len(spec.entities) >= 2:
            for target in spec.entities[1:]:
                result.extend(self.paths(spec.entities[0], target, max_hops=spec.depth, max_chars=max_chars))
        else:
            for name in spec.entities:
                direct = self.recall(name, max_chars=max_chars)
                links = self.entity_relations(name, include_history=spec.mode == 'timeline')
                for item in direct:
                    item['entity_relations'] = [edge for edge in links if item['id'] in edge['source_memory_ids']]
                for item in direct:
                    item['entity_recall']['mode'] = spec.mode
                result.extend(direct)
                if spec.mode == 'neighbors':
                    result.extend(MemoryConsolidation(self.memory).neighbors(
                        [item['id'] for item in direct], max_hops=spec.depth, query=name, max_chars=max_chars))
            if spec.mode == 'timeline':
                result.sort(key=lambda item: (item['event_time'] is None, item['event_time'] or 0, item['id']))
        found, seen = [], set()
        budget = min(max_chars, 12000)
        for item in result:
            cost = len(json.dumps(item, ensure_ascii=False)) + 2
            if item['id'] not in seen and cost <= budget:
                found.append(item)
                seen.add(item['id'])
                budget -= cost
        return found

    def entity_paths(self, source_name, target_name, *, max_hops=3, max_chars=12000):
        if max_hops not in (1, 2, 3):
            raise ValueError('entity path depth must be 1, 2 or 3')
        cfg = self.memory.settings
        roots = [node['id'] for node in self.lookup(source_name)]
        targets = {node['id'] for node in self.lookup(target_name)}
        frontier = [(node, []) for node in roots]
        visited, result, returned = set(roots), [], set()
        budget = min(max_chars, 12000)
        for _ in range(max_hops):
            next_frontier = []
            for node, steps in frontier:
                edges = self.store.rows("""SELECT * FROM memory_graph_edges
                    WHERE group_id=? AND persona_id=? AND (source_id=? OR target_id=?)
                    ORDER BY confidence DESC,valid_from DESC,id LIMIT 48""", (cfg.group_id, cfg.persona_id, node, node))
                for edge in edges:
                    if not self.edge_current(edge):
                        continue
                    target = edge['target_id'] if edge['source_id'] == node else edge['source_id']
                    if target in visited:
                        continue
                    path = steps + [{'edge_id': edge['id'], 'source_id': node, 'target_id': target,
                        'direction': 'forward' if edge['source_id'] == node else 'reverse'}]
                    if target in targets:
                        for step in path:
                            stored = self.store.rows('SELECT evidence FROM memory_graph_edges WHERE id=?', (step['edge_id'],))[0]
                            for mid, _ in json.loads(stored['evidence']):
                                if mid in returned:
                                    continue
                                item = self.memory.get(mid) | {'entity_path': {'steps': path}}
                                cost = len(json.dumps(item, ensure_ascii=False)) + 2
                                if cost <= budget:
                                    result.append(item)
                                    returned.add(mid)
                                    budget -= cost
                    if len(next_frontier) < 40:
                        next_frontier.append((target, path))
                        visited.add(target)
            frontier = next_frontier
            if not frontier or budget <= 0:
                break
        return result

    def entity_path_current(self, path):
        return bool(path.get('steps')) and all(
            (rows := self.store.rows('SELECT * FROM memory_graph_edges WHERE id=?', (step['edge_id'],)))
            and self.edge_current(rows[0]) for step in path['steps'])

    def context(self, records, *, max_chars=12000):
        """Current edges linked to exact source records or stable QQ identities, never other scopes."""
        cfg = self.memory.settings
        ids = [item['id'] for item in records]
        people = ['qq:' + item['subject_id'] for item in records if item['subject_id']]
        rows = self.store.rows("""SELECT r.* FROM memory_graph_edges r
            JOIN memory_graph_entities e ON e.id=r.source_id
            WHERE r.group_id=? AND r.persona_id=? AND (e.identity_key IN (SELECT value FROM json_each(?))
            OR EXISTS (SELECT 1 FROM json_each(r.evidence) ev WHERE json_extract(ev.value,'$[0]')
            IN (SELECT value FROM json_each(?)))) ORDER BY r.valid_from DESC,r.id LIMIT 80""",
            (cfg.group_id, cfg.persona_id, json.dumps(people), json.dumps(ids)))
        result, budget = [], max_chars
        for row in rows:
            if not self.edge_current(row):
                continue
            item = dict(row) | {'source_memory_ids': [mid for mid, _ in json.loads(row['evidence'])]}
            cost = len(json.dumps(item, ensure_ascii=False)) + 2
            if cost <= budget:
                result.append(item)
                budget -= cost
        return result
