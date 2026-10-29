"""Durable jobs and the sole final-send/formal-mutation boundary."""

import asyncio
import json
import random
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar

from .channels import ChannelError, SendUncertain
from .config import Settings
from .conversations import ConversationRef, MessageArchive
from .initiative import initiative_valid
from .store import Store

_committing = ContextVar("buwubot_committing", default=False)


class WorkPaused(RuntimeError):
    """Retain the task until the next allowed runtime window."""


def require_coordinator():
    if not _committing.get():
        raise RuntimeError("formal mutations require the coordinator")


class JobQueue:
    def __init__(self, store: Store):
        self.store = store

    def enqueue(self, kind: str, payload: dict, *, dedupe_key: str, priority: int,
                source_time: float) -> str:
        jid, now = uuid.uuid4().hex, time.time()
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,dedupe_key,kind,priority,source_time,payload,"
                       "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (jid, dedupe_key, kind, priority, source_time,
                        json.dumps(payload, ensure_ascii=False), now, now))
            return db.execute("SELECT id FROM jobs WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0]

    def get(self, jid: str) -> dict | None:
        row = self.store.connection.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row else None

    def claim(self, now: float, *, kinds: list[str] | None = None) -> dict | None:
        with self.store.transaction() as db:
            args = [now]
            condition = "state IN ('pending','retry_wait') AND next_attempt<=?"
            if kinds:
                condition += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                args.extend(kinds)
            row = db.execute(f"SELECT id FROM jobs WHERE {condition} "
                             "ORDER BY priority,source_time,created_at LIMIT 1", args).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?",
                       (now, row[0]))
        return self.get(row[0])

    def payload(self, jid: str, payload: dict):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False), time.time(), jid))

    def finish(self, jid: str, state: str = "succeeded", *, ready_at: float | None = None):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state=?,updated_at=?,last_error=NULL,"
                       "next_attempt=COALESCE(?,next_attempt),"
                       "attempts=CASE WHEN ?='pending' THEN 0 ELSE attempts END WHERE id=?",
                       (state, time.time(), ready_at, state, jid))

    def defer(self, jid: str, reason: str, *, now: float, retry_after: float = 0,
              needs_attention: bool = False):
        job = self.get(jid)
        delay = max(retry_after, [30, 120, 300, 900][min(max(job["attempts"] - 1, 0), 3)])
        if not retry_after:
            delay += random.uniform(0, 3)
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state=?,last_error=?,next_attempt=?,updated_at=? WHERE id=?",
                       ("needs_attention" if needs_attention else "retry_wait", reason[:500],
                        now + delay, now, jid))

    def retry_attention(self):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state='pending',next_attempt=0 WHERE state='needs_attention'")


class Coordinator:
    def __init__(self, store: Store, qq, settings: Settings,
                 *, allowed: Callable[[], bool] | None = None):
        self.store, self.qq, self.settings = store, qq, settings
        self.queue = JobQueue(store)
        self.archive = MessageArchive(store)
        self.lock = asyncio.Lock()
        self.allowed = allowed or (lambda: True)
        self.send_allowed = lambda key: self.allowed()
        self.draft_fingerprint: Callable[[], str] | None = None
        store.connection.executescript("""
            CREATE TABLE IF NOT EXISTS outgoing(
                job_id TEXT PRIMARY KEY REFERENCES jobs(id), group_id TEXT NOT NULL,
                reply_id TEXT, segments TEXT NOT NULL, started_at REAL NOT NULL,
                status TEXT NOT NULL, qq_message_id TEXT);
        """)

    async def commit(self, operation: Callable, *, background: bool = False):
        async with self.lock:
            if background and not self.allowed():
                raise WorkPaused("后台结果已保存，等待恢复运行后提交")
            token = _committing.set(True)
            try:
                return operation()
            finally:
                _committing.reset(token)

    async def effect(self, operation: Callable):
        """Serialize a channel-owned asynchronous effect with sends and formal commits."""
        async with self.lock:
            if not self.allowed():
                raise WorkPaused("操作等待恢复运行")
            token = _committing.set(True)
            try:
                return await operation()
            finally:
                _committing.reset(token)

    def response_history(self, jid):
        return [{'job_id':row['job_id'],'persona_id':json.loads(row['payload']).get('persona_id'),
                 'segments':json.loads(row['segments'])} for row in self.store.rows(
            "SELECT o.job_id,o.segments,j.payload FROM outgoing o JOIN jobs j ON j.id=o.job_id "
            "WHERE o.status='sent' AND json_extract(j.payload,'$.delivery_parent')=? "
            "ORDER BY o.started_at,j.rowid",(jid,))]

    def refresh_ambient(self, job):
        """Under the send lock, discard only unsent drafts overtaken by new chat."""
        root = self.queue.get(job['payload'].get('delivery_parent','')) if job['kind']=='delivery' else job
        if not root or root['kind']!='reply' or not root['payload'].get('ambient_participation'):
            return False
        payload = root['payload']
        if payload.get('ambient_order') == self.archive.incoming_order(
                payload.get('group_id',self.settings.group_id),self.settings.self_id):
            return False
        child = self.queue.get(payload.get('delivery_job_id',''))
        ids = [root['id']] + ([child['id']] if child else [])
        records = [row for jid in ids for row in self.store.rows(
            'SELECT status FROM outgoing WHERE job_id=?',(jid,))]
        if any(row['status'] in {'inflight','uncertain'} for row in records):
            return False  # Reconcile ambiguous sends before replacing any remaining draft.
        with self.store.transaction():
            if child:
                if child['state']!='succeeded':
                    self.queue.finish(child['id'],'skipped_expired')
                payload['delivery_serial'] = payload.get('delivery_serial',0)+1
            for key in ('delivery_job_id','response_batches','segments','persona_hash','feeling_recalls'):
                payload.pop(key,None)
            self.queue.payload(root['id'],payload)
            self.queue.finish(root['id'],'pending')
        return True

    async def continue_response(self, jid, *, now, finish_root=True):
        """One durable child per opportunity; existing send/reconcile remains the only boundary."""
        async with self.lock:
            root = self.queue.get(jid)
            if not root or root['state']=='succeeded' or root['state'].startswith('skipped_'):
                return True
            payload = root['payload']
            if 'response_batches' not in payload:
                return False
            if self.refresh_ambient(root):
                return False
            if not self.allowed() or not self.send_allowed(payload.get('group_id')):
                self.queue.finish(jid,'pending')
                return True
            child = self.queue.get(payload.get('delivery_job_id',''))
            record = self.store.rows('SELECT status FROM outgoing WHERE job_id=?',(child['id'],)) if child else []
            ambiguous = bool(record and record[0]['status'] in {'inflight','uncertain'})
            terminal = bool(child and (child['state']=='succeeded' or child['state'].startswith('skipped_')))
            if (not ambiguous and not terminal and self.draft_fingerprint
                    and payload.get('persona_hash') != self.draft_fingerprint()):
                if child:
                    self.queue.finish(child['id'],'skipped_expired')
                    payload['delivery_serial'] = payload.get('delivery_serial',0) + 1
                for key in ('delivery_job_id','response_batches','segments','persona_hash'):
                    payload.pop(key,None)
                self.queue.payload(jid,payload)
                self.queue.finish(jid,'pending')
                return False
            if not child:
                if not payload['response_batches']:
                    self.queue.finish(jid,'succeeded' if finish_root else 'pending')
                    return True
                fields = ('group_id','reply_id','reply_source_id','source_message_ids','messages',
                          'persona_id','persona_hash','batch_id')
                draft = {key:payload[key] for key in fields if key in payload}
                draft.update(delivery_parent=jid,segments=payload['response_batches'][0])
                with self.store.transaction():
                    cid = self.queue.enqueue('delivery',draft,
                        dedupe_key=f"delivery:{jid}:{payload.get('delivery_serial',0)}",
                        priority=root['priority'],source_time=root['source_time'])
                    self.queue.finish(cid,'held')
                    payload['delivery_job_id'] = cid
                    self.queue.payload(jid,payload)
                child = self.queue.get(cid)
            if child['state']=='retry_wait' and child['next_attempt'] > now:
                self.queue.finish(jid,'retry_wait',ready_at=child['next_attempt'])
                return True
        await self.send(child['id'],child['payload']['segments'],now=now)
        async with self.lock:
            child = self.queue.get(child['id'])
            root = self.queue.get(jid)
            payload = root['payload']
            if payload.get('delivery_job_id') != child['id']:
                return True  # The final send check already invalidated this child.
            if child['state']=='succeeded':
                payload['response_batches'] = payload['response_batches'][1:]
                payload.pop('delivery_job_id',None)
                payload['delivery_serial'] = payload.get('delivery_serial',0) + 1
                self.queue.payload(jid,payload)
                self.queue.finish(jid,'pending' if payload['response_batches'] or not finish_root else 'succeeded',
                                  ready_at=now+0.5)
            elif child['state'] == 'skipped_uncertain' and not finish_root:
                # The prefix is not resent; the root still owns work that has not started.
                payload.update(response_batches=[],response_unconfirmed=True)
                with self.store.transaction():
                    self.queue.payload(jid,payload)
                    self.queue.finish(jid,'pending',ready_at=now+0.5)
            elif child['state'].startswith('skipped_'):
                self.queue.finish(jid,child['state'])
            elif child['state']=='needs_attention':
                self.queue.defer(jid,child['last_error'] or '分条回复等待处理',now=now,needs_attention=True)
            else:
                self.queue.finish(jid,'retry_wait' if child['state']=='retry_wait' else 'pending',
                    ready_at=child['next_attempt'] if child['state']=='retry_wait' else now+0.5)
        return True

    async def send(self, jid: str, segments: list[dict], *, now: float | None = None,
                   greeting: bool = False):
        now = now if now is not None else time.time()
        async with self.lock:
            job = self.queue.get(jid)
            if not job or job['state']=='succeeded' or job['state'].startswith('skipped_'):
                return
            if greeting or job["kind"] == "greeting":
                self.queue.finish(jid, "skipped_expired")
                return
            if (not self.settings.runtime_enabled or not self.allowed()
                    or not self.send_allowed(job["payload"].get("group_id", self.settings.group_id))):
                self.queue.finish(jid, "pending")
                return
            target = job["payload"].get("group_id") or self.settings.group_id
            if not self.settings.qq_enabled and (not target or ConversationRef.from_key(target).channel_id == "qq"):
                self.queue.defer(jid, "QQ 聊天已停用，等待重新启用", now=now, retry_after=300)
                return
            existing = self.store.connection.execute(
                "SELECT * FROM outgoing WHERE job_id=?", (jid,)
            ).fetchone()
            if existing and existing["status"] in {"inflight", "uncertain"}:
                await self._reconcile(existing)
                return
            if self.refresh_ambient(job):
                return
            payload = job["payload"]
            if job['kind']=='delivery':
                parent = self.queue.get(payload.get('delivery_parent',''))
                if (not parent or parent['state']=='succeeded' or parent['state'].startswith('skipped_')
                        or parent['payload'].get('delivery_job_id') != jid):
                    self.queue.finish(jid,'skipped_expired')
                    return
            if job['kind'] == 'initiative' and not initiative_valid(self.store,self.store.settings()[0],payload,now,jid):
                self.queue.finish(jid,'skipped_expired')
                return
            if job['kind'] == 'initiative' and not getattr(self,'optional_send_allowed',lambda:True)():
                self.queue.finish(jid,'pending')
                return
            source_ids = payload.get("source_message_ids", [])
            canonical = {self.archive.canonical_id(key) for key in source_ids}
            if canonical and canonical != set(source_ids):
                answered = set()
                for prior in self.store.rows("SELECT j.payload FROM outgoing o JOIN jobs j ON j.id=o.job_id "
                                             "WHERE o.group_id=? AND o.status='sent' AND o.job_id<>?",
                                             (target, jid)):
                    prior_payload = json.loads(prior['payload'])
                    if prior_payload.get('delivery_parent') == jid:
                        continue
                    if payload.get('delivery_parent') and prior_payload.get('delivery_parent') == payload['delivery_parent']:
                        continue
                    if prior_payload.get('persona_id', self.settings.persona_id) == self.settings.persona_id:
                        answered.update(self.archive.canonical_id(key)
                                        for key in prior_payload.get('source_message_ids', []))
                if canonical <= answered:
                    self.queue.finish(jid, 'skipped_duplicate')
                    self.store.event('skip', '切换接入程序后的同一消息已有回复', {'job_id': jid})
                    return
            reply_id = payload.get("reply_id")
            for key in payload.get("source_message_ids", []):
                row = self.store.connection.execute(
                    "SELECT recalled,message_id FROM messages WHERE id=?", (self.archive.canonical_id(key),)
                ).fetchone()
                is_anchor = key == payload.get("reply_source_id") or (
                    not payload.get("reply_source_id") and row and row["message_id"] == reply_id)
                if row and row["recalled"] and is_anchor:
                    self.queue.finish(jid, "skipped_recalled")
                    self.store.event("skip", "原消息已撤回，无法引用", {"job_id": jid})
                    return
                if row and is_anchor:
                    reply_id = row["message_id"]
            group = payload.get("group_id") or self.settings.group_id
            if not self.settings.allows_conversation(group):
                self.queue.defer(jid, "会话未启用，不能发送", now=now, needs_attention=True)
                return
            if (job["kind"] in {"reply", "tool", "initiative", "delivery"} and self.draft_fingerprint
                    and payload.get("persona_hash") != self.draft_fingerprint()):
                self.queue.finish(jid, "pending")
                self.store.event("draft", "核心设定已更新，待发内容重新生成", {"job_id":jid})
                return
            with self.store.transaction() as db:
                db.execute("INSERT INTO outgoing VALUES(?,?,?,?,?,'inflight',NULL) "
                           "ON CONFLICT(job_id) DO UPDATE SET segments=excluded.segments,"
                           "reply_id=excluded.reply_id,started_at=excluded.started_at,status='inflight'",
                           (jid, group, reply_id, json.dumps(segments, ensure_ascii=False), now))
                db.execute("UPDATE jobs SET state='running',attempts=attempts+? WHERE id=?",
                           (1 if job["kind"]=="delivery" else 0,jid))
            try:
                result = await self.qq.send(group, segments, reply_id)
            except SendUncertain:
                with self.store.transaction() as db:
                    db.execute("UPDATE outgoing SET status='uncertain' WHERE job_id=?", (jid,))
                self.store.event("send_uncertain", "发送结果不明，等待核对", {"job_id": jid})
                return
            except ChannelError as exc:
                with self.store.transaction() as db:
                    db.execute("UPDATE outgoing SET status='failed' WHERE job_id=?", (jid,))
                self.queue.defer(jid, str(exc), now=now)
                return
            self._sent(jid, result)
            self.store.set_state("last_spoke", now)

    def _sent(self, jid: str, message_id: str):
        with self.store.transaction() as db:
            db.execute("UPDATE outgoing SET status='sent',qq_message_id=? WHERE job_id=?",
                       (message_id, jid))
            db.execute("UPDATE jobs SET state='succeeded',last_error=NULL,updated_at=? WHERE id=?",
                       (time.time(), jid))
            job = self.queue.get(jid)
            payload = job["payload"]
            outgoing = db.execute("SELECT * FROM outgoing WHERE job_id=?", (jid,)).fetchone()
            if job["kind"] in {"reply", "tool", "initiative", "delivery"} and payload.get("persona_id") and outgoing:
                segments = json.loads(outgoing["segments"])
                text = "\n".join(str(s.get("data", {}).get("text", ""))
                                 for s in segments if s.get("type") == "text").strip()
                if text:
                    sources = payload.get("messages", [])
                    own = {"id":"outgoing:" + jid, "message_id":message_id,
                           "group_id":outgoing["group_id"], "user_id":self.settings.self_id,
                           "self_id":self.settings.self_id, "time":outgoing["started_at"],
                           "display_name":self.settings.persona_name,
                           "nickname":self.settings.persona_name, "text":text, "segments":segments}
                    self.queue.enqueue("memory", {"group_id":outgoing["group_id"],
                        "persona_id":payload["persona_id"], "messages":[*sources, own],
                        "context_ids":[m["id"] for m in sources]},
                        dedupe_key="memory:sent:" + jid, priority=2,
                        source_time=outgoing["started_at"])
        self.store.event("reply", "消息发送成功", {"job_id": jid, "qq_message_id": message_id})

    async def _reconcile(self, outgoing):
        if not self.send_allowed(outgoing["group_id"]):
            return
        if not self.settings.allows_conversation(outgoing["group_id"]):
            self.queue.defer(outgoing["job_id"], "会话未启用，发送核对等待人工确认",
                             now=time.time(), needs_attention=True)
            return
        try:
            records = await self.qq.history(outgoing["group_id"])
        except ChannelError:
            records = []
        expected = json.loads(outgoing["segments"])
        matches = []
        for row in records:
            sender = str(row.get("user_id") or row.get("sender", {}).get("user_id", ""))
            if sender != self.settings.self_id or row.get("time", 0) < outgoing["started_at"] - 5:
                continue
            parts = row.get("message", [])
            if not isinstance(parts, list):
                continue
            reply = next((str(p.get("data", {}).get("id")) for p in parts
                          if p.get("type") == "reply"), None)
            body = [p for p in parts if p.get("type") != "reply"]
            if reply == outgoing["reply_id"] and body == expected:
                matches.append(str(row["message_id"]))
        if len(matches) == 1:
            self._sent(outgoing["job_id"], matches[0])
        else:
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET state='skipped_uncertain',updated_at=? WHERE id=?",
                           (time.time(), outgoing["job_id"]))
                db.execute("UPDATE outgoing SET status='unconfirmed' WHERE job_id=?",
                           (outgoing["job_id"],))
            self.store.event("skip", "无法可靠确认发送结果，按约定跳过", {"job_id": outgoing["job_id"]})

    def reset_running(self):
        """Only call after all old workers have stopped, never on a live reconnect."""
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state='pending' WHERE state='running' AND id NOT IN "
                       "(SELECT job_id FROM outgoing WHERE status IN ('inflight','uncertain'))")

    async def recover(self, *, reset_running: bool = True):
        async with self.lock:
            for row in self.store.connection.execute(
                "SELECT * FROM outgoing WHERE status IN ('inflight','uncertain')"
            ).fetchall():
                if not self.allowed():
                    break
                await self._reconcile(row)
            if reset_running:
                self.reset_running()
