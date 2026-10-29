"""Canonical conversation identities, events and persistent archive, independent of transport."""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

from .config import Settings
from .schedule import SHANGHAI
from .store import Store


@dataclass(frozen=True)
class ConversationRef:
    kind: Literal["group", "private"]
    target_id: str
    channel_id: str = "qq"

    def __post_init__(self):
        if (self.kind not in {"group", "private"} or not self.target_id or len(self.target_id) > 200
                or any(ord(c) < 32 for c in self.target_id)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", self.channel_id)
                or (self.channel_id == "qq" and (not self.target_id.isascii() or not self.target_id.isdigit()))):
            raise ValueError("invalid conversation target")

    @property
    def id(self) -> str:
        key = f"{self.kind}:{self.target_id}"
        return key if self.channel_id == "qq" else f"{self.channel_id}:{key}"

    @property
    def storage_key(self) -> str:
        # Preserve all existing group IDs, source IDs, hashes and vector namespaces.
        return self.target_id if self.channel_id == "qq" and self.kind == "group" else self.id

    @classmethod
    def from_key(cls, key: str):
        parts = key.split(":", 2)
        if len(parts) == 3:
            return cls(parts[1], parts[2], channel_id=parts[0])
        kind, target = parts if len(parts) == 2 else ("group", key)
        return cls(kind, target)


@dataclass(frozen=True)
class RecallEvent:
    conversation_id: str
    message_id: str


@dataclass
class Message:
    id: str
    message_id: str
    group_id: str
    user_id: str
    self_id: str
    time: float
    display_name: str
    nickname: str
    text: str
    segments: list[dict]
    called: bool = False
    recalled: bool = False
    name_mentioned: bool = False
    source_order: int | None = None  # Optional chronological order supplied by the channel.
    source_provider: str = ""
    source_video_urls: list[str] = field(default_factory=list)
    forwarded: dict | None = None  # Derived external material; never replaces the received event.

    @property
    def conversation_id(self) -> str:
        return ConversationRef.from_key(self.group_id).id


def _with_local_time(raw):
    if 'time' not in raw:
        return raw
    try:
        local = datetime.fromtimestamp(raw['time'], SHANGHAI).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        local = None
    return raw | {'local_time': local}


def message_context(message: Message | dict) -> dict:
    raw = _with_local_time(vars(message) if isinstance(message,Message) else message)
    mentions = list(dict.fromkeys(str(part.get('data', {}).get('qq', ''))
        for part in raw.get('segments', []) if part.get('type') == 'at'))
    people = [uid for uid in mentions if uid and uid != 'all']
    self_id = str(raw.get('self_id', ''))
    raw = raw | {'addressing': {'mentioned_user_ids': people,
        'mentions_self': bool(self_id and self_id in people),
        'mentions_other_users': any(uid != self_id for uid in people),
        'mentions_everyone': 'all' in mentions}}
    material = raw.get('forwarded')
    if not material:
        return raw
    # The archive keeps download locators; the model only needs ordered references and analysis.
    return raw | {'forwarded':material | {'nodes':[_with_local_time(node) | {'images':[
        {'type':'image','position':index} for index,_ in enumerate(node['images'])]}
        for node in material['nodes']]}}


def reply_context(context: list[dict], targets: list[Message | dict]) -> list[dict]:
    """Later incoming messages have their own tasks; keep real sent replies for continuity."""
    targets=[vars(item) if isinstance(item,Message) else item for item in targets]
    if not targets or any('time' not in item for item in targets):
        return context
    last=max(targets,key=lambda item:(item['time'],item.get('source_order') or 0))
    ids={item['id'] for item in targets}
    def included(item):
        if item.get('role')=='assistant' or item.get('id') in ids:
            return True
        when=item.get('time')
        if when is None or when<last['time']:
            return True
        if when>last['time']:
            return False
        if (item.get('source_order') is not None and last.get('source_order') is not None
                and item.get('source_provider')==last.get('source_provider')):
            return item['source_order']<=last['source_order']
        return True  # Same-second messages without a comparable order remain available.
    return [item for item in context if included(item)]


def resolve_source_ids(values: list, messages: list[Message]) -> list[str]:
    if not isinstance(values,list):
        raise TypeError("source IDs must be an array")
    known={message.id for message in messages}
    native={}
    for message in messages:
        native.setdefault(message.message_id,set()).add(message.id)
    resolved=[]
    for value in values:
        if type(value) not in {str,int}:
            raise ValueError("invalid source ID type")
        value=str(value)
        if value in known:
            resolved.append(value)
        elif len(native.get(value,set()))==1:
            resolved.append(next(iter(native[value])))
        else:
            raise ValueError("model invented or ambiguous source IDs")
    return list(dict.fromkeys(resolved))


class MessageArchive:
    def __init__(self, store: Store):
        self.store = store
        store.connection.executescript("""
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                message_id TEXT NOT NULL, time REAL NOT NULL, payload TEXT NOT NULL,
                recalled INTEGER NOT NULL DEFAULT 0, received_order INTEGER);
            CREATE INDEX IF NOT EXISTS messages_group_time ON messages(group_id,time);
            CREATE TABLE IF NOT EXISTS message_aliases(
                alias_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL,
                provider TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS members(
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT NOT NULL,
                aliases TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(group_id,user_id));
        """)

    @staticmethod
    def _member(db, group: str, user: str, nickname: str, card: str, observed_at: float):
        row = db.execute("SELECT * FROM members WHERE group_id=? AND user_id=?", (group,user)).fetchone()
        aliases = list(dict.fromkeys([*(json.loads(row["aliases"]) if row else []),
                                     *[name for name in (nickname,card) if name]]))
        display_name = card or nickname or user
        if row and row["updated_at"] > observed_at:
            display_name, observed_at = row["display_name"], row["updated_at"]
        db.execute("INSERT INTO members VALUES(?,?,?,?,?) ON CONFLICT(group_id,user_id) "
                   "DO UPDATE SET display_name=excluded.display_name,aliases=excluded.aliases,"
                   "updated_at=excluded.updated_at", (group,user,display_name,
                   json.dumps(aliases,ensure_ascii=False),observed_at))

    def sync_members(self, records: list[dict], settings: Settings, *, observed_at: float | None = None):
        observed_at = observed_at if observed_at is not None else time.time()
        with self.store.transaction() as db:
            for row in records:
                user = str(row.get("user_id", ""))
                if (str(row.get("group_id", settings.group_id)) != settings.group_id
                        or not user.isascii() or not user.isdigit() or user == settings.self_id):
                    continue
                self._member(db,settings.group_id,user,str(row.get("nickname") or ""),
                             str(row.get("card") or ""),observed_at)

    def accept(self, event: Message | RecallEvent | dict | None, settings: Settings) -> Message | None:
        if isinstance(event, dict):
            # Compatibility for old callers; native decoding belongs to the plugin.
            from .channels import builtin_channels
            event = builtin_channels().normalize("qq", event, settings)
        if isinstance(event, RecallEvent):
            if settings.allows_conversation(event.conversation_id):
                with self.store.transaction() as db:
                    db.execute("UPDATE messages SET recalled=1 WHERE group_id=? AND message_id=?",
                               (event.conversation_id, event.message_id))
            return None
        message = event
        if not message or not settings.allows_conversation(message.group_id) or message.user_id == message.self_id:
            return None
        message.name_mentioned = any(alias.casefold() in message.text.casefold() for alias in settings.aliases)
        with self.store.transaction() as db:
            alias = db.execute("SELECT canonical_id FROM message_aliases WHERE alias_id=?",
                               (message.id,)).fetchone()
            if alias:
                db.execute("UPDATE messages SET message_id=? WHERE id=?",
                           (message.message_id, alias[0]))
                return None
            exists = db.execute("SELECT id,payload FROM messages WHERE id=?", (message.id,)).fetchone()
            identified = exists and json.loads(exists['payload']).get('source_provider')
            canonical = None if identified else self._transport_duplicate(db, message)
            if canonical:
                db.execute("INSERT INTO message_aliases VALUES(?,?,?)",
                           (message.id, canonical, message.source_provider))
                db.execute("UPDATE messages SET message_id=?,payload=json_set(payload,'$.source_order',?) "
                           "WHERE id=?", (message.message_id, message.source_order, canonical))
                return None
            if exists:
                db.execute("UPDATE messages SET message_id=?,payload=CASE WHEN ? IS NULL THEN payload "
                           "ELSE json_set(payload,'$.source_order',?) END WHERE id=?",
                           (message.message_id, message.source_order, message.source_order, message.id))
                return None
            self._member(db,message.group_id,message.user_id,message.nickname,
                         message.display_name,message.time)
            counter = db.execute("SELECT value FROM state WHERE key='message_sequence'").fetchone()
            order = int(json.loads(counter[0])) + 1 if counter else db.execute(
                "SELECT COALESCE(MAX(received_order),0)+1 FROM messages"
            ).fetchone()[0]
            db.execute("INSERT INTO state VALUES('message_sequence',?) ON CONFLICT(key) "
                       "DO UPDATE SET value=excluded.value", (json.dumps(order),))
            db.execute("INSERT INTO messages(id,group_id,user_id,message_id,time,payload,received_order) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (message.id, message.group_id, message.user_id, message.message_id,
                        message.time, json.dumps(asdict(message), ensure_ascii=False), order))
        return message

    @staticmethod
    def _segment_identity(segments):
        identity = []
        for segment in segments:
            kind, data = segment.get('type'), segment.get('data', {})
            if kind == 'text':
                identity.append(('text', data.get('text', '')))
            elif kind == 'image' and re.fullmatch(r'[a-fA-F0-9]{32}\.(?:jpg|jpeg|png|gif|webp|bmp)',
                                                  str(data.get('file', '')), re.IGNORECASE):
                identity.append(('image', data['file'].split('.')[0].lower()))
            elif kind == 'at':
                identity.append(('at', str(data.get('qq', ''))))
            else:
                return None
        return identity or None

    @staticmethod
    def _transport_duplicate(db, message):
        # Cross-provider matching requires an unambiguous content identity.
        # Temporary image URLs are excluded; only QQ's file hash is comparable.
        identity = MessageArchive._segment_identity(message.segments)
        if message.source_provider not in {"napcat", "snowluma"} or identity is None:
            return None
        rows = db.execute("SELECT * FROM messages WHERE group_id=? AND user_id=? AND time=? "
                          "AND id<>? AND id NOT IN (SELECT alias_id FROM message_aliases)",
                          (message.group_id, message.user_id, message.time, message.id)).fetchall()
        matches = [row for row in rows if (old := json.loads(row['payload'])).get('self_id') == message.self_id
                   and old.get('text') == message.text
                   and MessageArchive._segment_identity(old.get('segments', [])) == identity]
        if len(matches) != 1:
            return None
        old = matches[0]
        old_payload = json.loads(old['payload'])
        old_order = old_payload.get('source_order')
        if old_order is not None and message.source_order is not None and old_order != message.source_order:
            return None
        provider = old_payload.get('source_provider') or 'napcat'
        if provider == message.source_provider:
            return None
        if db.execute("SELECT 1 FROM message_aliases WHERE canonical_id=? AND provider=?",
                      (old['id'], message.source_provider)).fetchone():
            return None
        existing = db.execute("SELECT received_order FROM messages WHERE id=?", (message.id,)).fetchone()
        if existing and existing['received_order'] <= old['received_order']:
            return None
        return old['id']

    def canonical_id(self, message_key: str) -> str:
        row = self.store.connection.execute("SELECT canonical_id FROM message_aliases WHERE alias_id=?",
                                            (message_key,)).fetchone()
        return row[0] if row else message_key

    def get(self, message_key: str) -> Message | None:
        row = self.store.connection.execute("SELECT * FROM messages WHERE id=?",
                                            (self.canonical_id(message_key),)).fetchone()
        return self._decode(row) if row else None

    def quoted(self, messages: list[Message], *, limit: int = 12) -> list[Message]:
        """Resolve explicit reply chains in the parent's conversation, never globally."""
        pending = list(messages)
        seen = {m.id for m in messages}
        result = []
        for parent in pending:
            for segment in parent.segments:
                native = segment.get('data', {}).get('id') if segment.get('type') == 'reply' else None
                if not isinstance(native, (str, int)) or isinstance(native, bool):
                    continue
                rows = self.store.rows('SELECT * FROM messages WHERE group_id=? AND message_id=? '
                    'AND recalled=0 AND time<=? LIMIT 2', (parent.group_id, str(native), parent.time))
                if len(rows) != 1:
                    continue
                source = self.get(rows[0]['id'])
                if source is None or source.recalled or source.id in seen:
                    continue
                seen.add(source.id)
                result.append(source)
                if len(result) >= min(max(limit, 1), 12):
                    return result
                pending.append(source)
        return result

    @staticmethod
    def _decode(row) -> Message:
        payload = json.loads(row["payload"])
        payload.update(message_id=row["message_id"], recalled=bool(row["recalled"]))
        return Message(**payload)

    def recent(self, group_id: str, *, limit: int = 50, after_order: int = 0) -> list[Message]:
        rows = self.store.connection.execute(
            "SELECT * FROM messages WHERE group_id=? AND received_order>? "
            "AND id NOT IN (SELECT alias_id FROM message_aliases) "
            "ORDER BY time DESC,COALESCE(json_extract(payload,'$.source_order'),received_order) DESC,"
            "received_order DESC LIMIT ?", (group_id, after_order, min(limit, 200))
        ).fetchall()
        return [self._decode(row) for row in reversed(rows)]

    def incoming_order(self, group_id: str, self_id: str) -> int:
        row = self.store.connection.execute('SELECT max(received_order) FROM messages '
            'WHERE group_id=? AND user_id<>? AND id NOT IN (SELECT alias_id FROM message_aliases)',
            (group_id, self_id)).fetchone()
        return row[0] or 0

    def members(self, group_id: str) -> list[dict]:
        rows = self.store.connection.execute("SELECT * FROM members WHERE group_id=?", (group_id,))
        return [dict(row) | {"aliases": json.loads(row["aliases"])} for row in rows]

    def tag_self_replies(self, settings: Settings):
        # Migrate legacy drafts before a persona edit changes their fingerprint.
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET payload=json_set(payload,'$.persona_id',?) WHERE kind='reply' "
                       "AND json_extract(payload,'$.persona_id') IS NULL "
                       "AND json_extract(payload,'$.persona_hash')=?",
                       (settings.persona_id,settings.persona_fingerprint))

    def context(self, settings: Settings, *, limit: int = 50) -> list[dict]:
        limit = min(max(limit,1),200)
        items = [message_context(message) | {"role":"user"} for message in self.recent(settings.group_id,limit=limit)]
        if self.store.rows("SELECT 1 FROM sqlite_master WHERE name='outgoing'"):
            for row in reversed(self.store.rows("SELECT o.*,j.payload FROM outgoing o JOIN jobs j ON j.id=o.job_id "
                    "WHERE o.group_id=? AND o.status='sent' ORDER BY o.started_at DESC,o.rowid DESC LIMIT ?",
                    (settings.group_id,limit))):
                payload = json.loads(row["payload"])
                if not (payload.get("persona_id") == settings.persona_id or
                        (not payload.get("persona_id") and payload.get("persona_hash") == settings.persona_fingerprint)):
                    continue
                segments = json.loads(row["segments"])
                if not segments:
                    continue
                items.append({"id":"outgoing:"+row["job_id"],"role":"assistant",
                    "group_id":row["group_id"],"message_id":row["qq_message_id"],
                    "persona_id":settings.persona_id,"display_name":settings.persona_name,
                    "user_id":settings.self_id,"time":row["started_at"],"reply_to":row["reply_id"],
                    "text":"".join(str(s.get("data",{}).get("text","")) for s in segments if s.get("type")=="text"),
                    "segments":segments})
        return sorted(items,key=lambda item:(item["time"],item["role"]=="assistant"))[-limit:]
