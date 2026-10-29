"""OneBot 11 boundary: stable identities, idempotent reception, native replies."""

import json
import math
from collections.abc import AsyncIterator
from typing import ClassVar
from urllib.parse import urljoin, urlparse

import httpx
from websockets.asyncio.client import connect

from .channels import ChannelError as QQError
from .channels import SendUncertain
from .config import Settings
from .conversations import ConversationRef, Message, MessageArchive, RecallEvent, resolve_source_ids
from .qq_activity import QQ_CONFIGURATION_FIELDS, QQ_SETTINGS_SECTIONS
from .video_links import video_references

__all__ = [
    "ConversationRef",
    "Message",
    "MessageArchive",
    "QQAdapter",
    "QQError",
    "RecallEvent",
    "SendUncertain",
    "normalize_event",
    "normalize_message",
    "resolve_source_ids",
]


def normalize_message(event: dict, settings: Settings) -> Message | None:
    if event.get("post_type") not in {"message", "message_sent"}:
        return None
    if event.get("message_type") not in {"group", "private"} or not event.get("message_id"):
        return None
    sender = event.get("sender") or {}
    user = str(event.get("user_id") or sender.get("user_id", ""))
    self_id = str(event.get("self_id") or settings.self_id)
    kind = event["message_type"]
    target_id = str(event.get("group_id", "")) if kind == "group" else user
    try:
        target = ConversationRef(kind, target_id)
    except ValueError:
        return None
    group = target.storage_key
    if not settings.allows_conversation(group) or user == self_id:
        return None
    segments = event.get("message")
    # Request array-format OneBot events; never mistake raw CQ markup for safe text.
    if not isinstance(segments, list):
        return None
    text = "".join(str(s.get("data", {}).get("text", "")) for s in segments
                   if isinstance(s, dict) and s.get("type") == "text")
    video_urls = video_references(text)
    for part in segments[:20]:
        if isinstance(part, dict) and part.get('type') in {'json', 'share'}:
            data = part.get('data', {})
            if not isinstance(data, dict):
                continue
            data = data.get('data', '') if part['type'] == 'json' else data
            raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            for value in video_references(raw):
                if value not in video_urls and len(video_urls) < 20:
                    video_urls.append(value)
    called = kind == "private" or any(s.get("type") == "at" and str(s.get("data", {}).get("qq")) == self_id
                 for s in segments if isinstance(s, dict))
    sequence = event.get("message_seq") or event.get("real_seq")
    key = f"{group}:seq:{sequence}" if sequence else f"{group}:id:{event['message_id']}"
    return Message(key, str(event["message_id"]), group, user, self_id,
                   float(event.get("time", 0)), str(sender.get("card") or sender.get("nickname") or user),
                   str(sender.get("nickname") or user), text, segments, called,
                   source_order=int(sequence) if sequence is not None else None,
                   source_provider=settings.qq_provider, source_video_urls=video_urls)


def normalize_event(event: dict, settings: Settings) -> Message | RecallEvent | None:
    if event.get("notice_type") in {"group_recall", "friend_recall"}:
        kind = "group" if event["notice_type"] == "group_recall" else "private"
        target = str(event.get("group_id" if kind == "group" else "user_id", ""))
        try:
            ref = ConversationRef(kind, target)
        except ValueError:
            return None
        return RecallEvent(ref.storage_key, str(event.get("message_id", "")))
    return normalize_message(event, settings)


class QQAdapter:
    plugin_id = "qq"
    management: ClassVar[dict] = {"name": "QQ 聊天", "description": "管理 QQ 账号、连接和授权会话。",
                  "enabled_setting": "qq_enabled", "settings_sections": QQ_SETTINGS_SECTIONS}
    configuration_fields = QQ_CONFIGURATION_FIELDS
    capabilities = frozenset({"group", "private", "quote", "history", "members", "images", "recall"})
    normalize_event = staticmethod(normalize_event)

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None,
                 media_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(timeout=20, trust_env=False)
        # Local OneBot bypasses system proxies; public CDN downloads use the user's network route.
        self._owned_media_client = client is None and media_client is None
        self.media_client = (media_client if media_client is not None else client
                             if client is not None else httpx.AsyncClient(timeout=25, trust_env=True))
        self.connected = False

    async def close(self):
        if self._owned_client:
            await self.client.aclose()
        if self._owned_media_client:
            await self.media_client.aclose()

    async def call(self, action: str, payload: dict | None = None) -> dict:
        try:
            response = await self.client.post(
                self.settings.qq_url.rstrip("/") + "/" + action,
                headers={"Authorization": "Bearer " + self.settings.qq_token.get_secret_value()},
                json=payload or {},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise QQError("QQ connection unavailable") from exc
        except httpx.TransportError as exc:
            error = SendUncertain if action in {"send_group_msg", "send_private_msg"} else QQError
            raise error("QQ response interrupted") from exc
        if response.status_code >= 500 and action in {"send_group_msg", "send_private_msg"}:
            raise SendUncertain("QQ returned an ambiguous server failure")
        if response.status_code != 200:
            raise QQError(f"QQ HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            error = SendUncertain if action in {"send_group_msg", "send_private_msg"} else QQError
            raise error("QQ returned invalid JSON") from exc
        if result.get("retcode", 0) != 0 or result.get("status") == "failed":
            raise QQError(f"QQ action rejected ({result.get('retcode')})")
        return result.get("data") or {}

    async def send(self, group_id: str, segments: list[dict], reply_id: str | None = None) -> str:
        return await self.send_to(ConversationRef.from_key(group_id), segments, reply_id)

    async def send_to(self, target: ConversationRef, segments: list[dict], reply_id=None) -> str:
        if target.channel_id != self.plugin_id:
            raise QQError("conversation belongs to another channel plugin")
        parts = ([{"type": "reply", "data": {"id": reply_id}}] if reply_id else []) + segments
        field = "group_id" if target.kind == "group" else "user_id"
        result = await self.call(f"send_{target.kind}_msg", {field: target.target_id, "message": parts})
        if not result.get("message_id"):
            raise SendUncertain("QQ did not acknowledge a message identifier")
        return str(result["message_id"])

    async def history(self, group_id: str, *, count: int = 100,
                      before: str | None = None) -> list[dict]:
        return await self.history_for(ConversationRef.from_key(group_id), count=count,
                                      before_sequence=before)

    async def history_for(self, target: ConversationRef, *, count=100, before_sequence=None):
        if target.channel_id != self.plugin_id:
            raise QQError("conversation belongs to another channel plugin")
        field = "group_id" if target.kind == "group" else "user_id"
        snowluma = self.settings.qq_provider == "snowluma"
        payload = {field: target.target_id, "count": count,
                   "reverse_order" if snowluma else "reverseOrder": True}
        if before_sequence:
            payload["message_id" if snowluma else "message_seq"] = (
                int(before_sequence) if snowluma else before_sequence)
        action = "get_group_msg_history" if target.kind == "group" else "get_friend_msg_history"
        result = await self.call(action, payload)
        records = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(records, list):
            raise QQError("QQ returned invalid history records")
        def order(row):
            timestamp = float(row["time"])
            if not math.isfinite(timestamp):
                raise ValueError("non-finite history timestamp")
            return timestamp, int(row.get("message_seq") or row.get("real_seq") or 0)
        try:
            records = sorted(records, key=order)
        except (TypeError, ValueError, KeyError, AttributeError):
            raise QQError("QQ returned invalid history order fields") from None
        if snowluma:
            # The core must not assume every OneBot provider pages by QQ sequence.
            records = [dict(row, history_cursor=str(row["message_id"]))
                       if row.get("message_id") else dict(row) for row in records]
        return records

    async def login_info(self) -> dict:
        return await self.call("get_login_info")

    async def forwarded(self, group_id: str, identifier: str) -> dict:
        if not self.settings.qq_enabled or not self.settings.allows_conversation(group_id):
            raise QQError('转发所属会话未启用')
        return await self.call('get_forward_msg', {'id':identifier})

    async def members(self, group_id: str) -> list[dict]:
        result = await self.call("get_group_member_list", {"group_id":group_id,"no_cache":True})
        if not isinstance(result,list) or any(not isinstance(row,dict) for row in result):
            raise QQError("QQ returned an invalid member list")
        return result

    @staticmethod
    def trusted_media_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            return (parsed.scheme in {"https", "http"} and not parsed.username and not parsed.password
                    and parsed.port in {None, 80, 443} and any(host == domain or host.endswith("." + domain)
                    for domain in ("qpic.cn", "qq.com", "qq.com.cn", "gtimg.cn", "qlogo.cn")))
        except ValueError:
            return False

    async def fetch_image(self, segment: dict, *, max_bytes: int) -> bytes:
        data = segment.get("data", {})
        url = data.get("url", "")
        if not url:
            details = await self.call("get_image", {"file":data.get("file", "")})
            url = details.get("url") or details.get("download_url") or ""
            return await self._download_image(url, max_bytes=max_bytes)
        try:
            return await self._download_image(url, max_bytes=max_bytes)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {400, 401, 403, 404, 410} or not data.get('file'):
                raise
            details = await self.call('get_image', {'file':data['file']})
            refreshed = details.get('url') or details.get('download_url') or ''
            if refreshed == url:
                raise
            return await self._download_image(refreshed, max_bytes=max_bytes)

    async def _download_image(self, url: str, *, max_bytes: int) -> bytes:
        if not self.trusted_media_url(url):
            raise ValueError("image source is not a trusted QQ media URL")
        for _ in range(4):
            async with self.media_client.stream("GET", url, timeout=25, follow_redirects=False) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    if not self.trusted_media_url(url):
                        raise ValueError("image redirect leaves trusted QQ media domains")
                    continue
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise ValueError("image exceeds size limit")
                return bytes(buffer)
        raise ValueError("too many image redirects")

    async def events(self) -> AsyncIterator[dict]:
        token = self.settings.qq_ws_token.get_secret_value() or self.settings.qq_token.get_secret_value()
        headers = {"Authorization": "Bearer " + token}
        async with connect(self.settings.qq_ws_url, additional_headers=headers,
                           proxy=None, max_size=16 * 1024 * 1024) as socket:
            self.connected = True
            try:
                async for raw in socket:
                    try:
                        value = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict):
                        yield value
            finally:
                self.connected = False
