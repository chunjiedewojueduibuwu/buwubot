"""Channel plugin construction; built-ins currently contain only QQ."""

import re
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Protocol

from .config import Settings

if TYPE_CHECKING:
    from .conversations import Message, RecallEvent


class ChannelError(RuntimeError):
    pass


class SendUncertain(ChannelError):
    """The transport may have accepted a send; reconcile before retrying."""


class ChatChannel(Protocol):
    plugin_id: str
    capabilities: frozenset[str]
    connected: bool

    @staticmethod
    def normalize_event(event: dict, settings: Settings) -> "Message | RecallEvent | None": ...

    async def close(self) -> None: ...
    async def login_info(self) -> dict: ...
    async def send(self, conversation_id: str, segments: list[dict], reply_id: str | None = None) -> str: ...
    async def history(self, conversation_id: str, *, count: int = 100,
                      before: str | None = None) -> list[dict]: ...
    async def members(self, conversation_id: str) -> list[dict]: ...
    async def fetch_image(self, segment: dict, *, max_bytes: int) -> bytes: ...
    def events(self) -> AsyncIterator[dict]: ...


class ChannelRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[[Settings], ChatChannel]] = {}
        self._decoders = {}
        self._instances = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def register(self, name: str, factory: Callable[[Settings], ChatChannel], *, decoder=None):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name) or name in self._factories:
            raise ValueError("invalid or duplicate channel plugin")
        self._factories[name] = factory
        self._decoders[name] = decoder or getattr(factory, "normalize_event", None)

    def create(self, name: str, settings: Settings) -> ChatChannel:
        if name not in self._factories:
            raise ValueError("channel plugin is not registered")
        instance = self._factories[name](settings)
        self._instances[name] = instance
        return instance

    def manifests(self, settings: Settings) -> list[dict]:
        items = []
        for name, factory in self._factories.items():
            manifest = getattr(factory, "management", {})
            instance = self._instances.get(name)
            enabled = getattr(settings, manifest.get("enabled_setting", ""), False)
            items.append({"id": name, "name": manifest.get("name", name), "kind": "activity",
                "page_id": name, "description": manifest.get("description", ""),
                "installed": True, "enabled": enabled is True,
                "connected": bool(instance and instance.connected),
                "capabilities": sorted(getattr(factory, "capabilities", ())),
                "settings_sections": deepcopy(manifest.get("settings_sections", []))})
        return items

    def configuration_fields(self, name: str) -> frozenset[str]:
        return frozenset(getattr(self._factories[name], "configuration_fields", ()))

    def normalize(self, name: str, event: dict, settings: Settings):
        if name not in self._factories:
            raise ValueError("channel plugin is not registered")
        decoder = self._decoders[name]
        if decoder is None:
            raise ChannelError("channel plugin has no native event decoder")
        return decoder(event, settings)


def builtin_channels() -> ChannelRegistry:
    from .qq import QQAdapter

    registry = ChannelRegistry()
    registry.register("qq", QQAdapter)
    return registry
