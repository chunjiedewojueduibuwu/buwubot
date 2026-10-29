"""Validated, versioned settings. Secret values are never included in public responses."""

import hashlib
import json
import re
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .memory_dynamics import DAY, DynamicsPolicy


class MemoryLayerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    strategy: Literal["always", "rolling", "relevance", "recency", "importance"] = "relevance"
    half_life_minutes: float | None = Field(default=None, ge=1, le=5256000)
    window_batches: int = Field(default=5, ge=1, le=50)
    dynamics: dict[str,float] = Field(default_factory=dict)

    @field_validator('dynamics',mode='before')
    @classmethod
    def valid_dynamics(cls,value):
        if (not isinstance(value,dict) or 'base_half_life' in value
                or any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in value.values())):
            raise ValueError('invalid memory dynamics parameters')
        try:
            DynamicsPolicy(**value)
        except (TypeError,ValueError) as exc:
            raise ValueError('invalid memory dynamics parameters') from exc
        return value

    def curve(self):
        return DynamicsPolicy(base_half_life=self.half_life_minutes*60
            if self.half_life_minutes is not None else 36500*DAY,**self.dynamics)


class FeelingPolicy(MemoryLayerPolicy):
    perceptibility: float = Field(default=.01,gt=0,lt=1)
    forget_threshold: float = Field(default=.01,gt=0,lt=1)
    low_days: float = Field(default=2, ge=.01, le=36500)
    high_days: float = Field(default=365, ge=.01, le=36500)
    recall_gain: float = Field(default=.15, ge=0, le=1)
    permanent_threshold: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def forgetting_range(self):
        if self.high_days < self.low_days:
            raise ValueError("high-strength retention must not be shorter")
        return self


class FactualPolicy(MemoryLayerPolicy):
    curve_shape: Literal['exponential','dual'] = 'exponential'
    # Old saved policies opt in through the management page, rather than silently deleting history.
    forget_threshold: float = Field(default=0, ge=0, lt=1)
    reinforcement_gain: float = Field(default=.15, ge=0, le=1)
    merge_recall_gain: float = Field(default=.05, ge=0, le=1)
    spacing_half_life_minutes: float = Field(default=360,ge=1,le=10080,allow_inf_nan=False,strict=True)

    @model_validator(mode='before')
    @classmethod
    def discard_legacy_idle_limit(cls,value):
        if isinstance(value,dict):
            return {key:item for key,item in value.items() if key!='max_idle_days'}
        return value

    def curve(self):
        return replace(super().curve(),curve_version=2 if self.curve_shape=='dual' else 1)


class RecentPolicy(FactualPolicy):
    distilled_secondary_days: float = Field(default=14, ge=1, le=36500)
    distilled_important_days: float = Field(default=30, ge=1, le=36500)


class MemoryPolicies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    core: MemoryLayerPolicy = Field(default_factory=lambda:MemoryLayerPolicy(strategy="always"))
    context: MemoryLayerPolicy = Field(default_factory=lambda:MemoryLayerPolicy(strategy="rolling"))
    feeling: FeelingPolicy = Field(default_factory=lambda:FeelingPolicy(strategy="recency", half_life_minutes=30))
    important: FactualPolicy = Field(default_factory=lambda:FactualPolicy(strategy="importance"))
    secondary: FactualPolicy = Field(default_factory=lambda:FactualPolicy(half_life_minutes=180*1440, forget_threshold=.01))
    recent: RecentPolicy = Field(default_factory=lambda:RecentPolicy(strategy="recency", half_life_minutes=7*1440, forget_threshold=.01))
    long_term: FactualPolicy = Field(default_factory=lambda:FactualPolicy(
        half_life_minutes=180*1440,forget_threshold=.01,dynamics={'meaning_influence':12}))

    def for_schema(self,version):
        return self.model_dump(exclude={'important','secondary'} if version==2 else {'long_term'})

    @model_validator(mode="after")
    def layer_boundaries(self):
        if self.core.strategy != "always" or self.core.half_life_minutes is not None or self.core.dynamics:
            raise ValueError("core memory is always read without decay")
        if self.context.strategy != "rolling" or self.context.half_life_minutes is not None or self.context.dynamics:
            raise ValueError("context memory uses a fully read rolling window without decay")
        if any(getattr(self, key).strategy in {"always", "rolling"}
               for key in ("feeling", "important", "secondary", "recent", "long_term")):
            raise ValueError("factual layers require a retrieval strategy")
        return self


class Settings(BaseModel):
    mood_half_life_minutes: float = Field(default=60,ge=1,le=10080,allow_inf_nan=False,strict=True)
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    runtime_enabled: bool = False
    qq_enabled: bool = False
    qq_provider: Literal["napcat", "snowluma"] = "napcat"
    group_id: str = ""
    group_ids: list[str] = Field(default_factory=list)
    private_ids: list[str] = Field(default_factory=list)
    owner_id: str = ""
    owner_private_enabled: bool = False
    owner_initiative_enabled: bool = False
    owner_initiative_interval_minutes: int = Field(default=60, ge=15, le=1440)
    owner_initiative_unanswered_hours: int = Field(default=72, ge=24, le=720)
    self_id: str = ""
    persona_id: str = Field(default="assistant", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    persona_name: str = Field(default="AI助手", min_length=1, max_length=40)
    persona: str = ""
    aliases: list[str] = Field(default_factory=lambda: ["AI助手"])
    qq_url: str = "http://127.0.0.1:3000"
    qq_ws_url: str = "ws://127.0.0.1:3001"
    qq_token: SecretStr = SecretStr("")
    qq_ws_token: SecretStr = SecretStr("")
    snowluma_auto_connect: bool = False
    snowluma_directory: str = ""
    snowluma_password: SecretStr = SecretStr("")
    workbuddy_url: str = "http://127.0.0.1:3065/v1"
    workbuddy_key: SecretStr = SecretStr("")
    primary_model: str = "cn:deepseek-v4.1-flash"
    scene_analysis_enabled: bool = False
    auxiliary_model: str = "cn:glm-5.3-flash"
    reviewer_model: str = "cn:glm-5.3-flash"
    active_poll_seconds: int = Field(default=60, ge=5, le=3600)
    inactive_after_seconds: int = Field(default=300, ge=30, le=86400)
    sleeping_poll_seconds: int = Field(default=900, ge=30, le=86400)
    qq_ambient_reply_delay_seconds: int = Field(default=15, ge=0, le=300)
    routine_enabled: bool = True
    bedtime: str = "22:30"
    reboot_wakeup: str = "06:30"
    continuous_wakeup: str = "07:30"
    # Legacy greeting values are retained for stored-config compatibility, never scheduled.
    morning_cutoff: str = "12:00"
    morning_template: str = "早安！"
    evening_template: str = "晚安，明天见。"
    memory_policies: MemoryPolicies = Field(default_factory=MemoryPolicies)
    memory_relation_expansion: bool = True
    memory_relation_depth: int = Field(default=3, ge=1, le=3)
    retention_days: int = Field(default=7, ge=1, le=365)
    memory_deleted_retention_days: int = Field(default=7,ge=1,le=365)
    history_reply_hours: int = Field(default=24, ge=1, le=168)
    sticker_per_category: int = Field(default=20, ge=1, le=2000)
    sticker_total: int = Field(default=200, ge=1, le=10000)
    embedding_provider: Literal["none", "local", "api"] = "none"
    local_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_url: str = ""
    embedding_model: str = ""
    embedding_key: SecretStr = SecretStr("")
    embedding_dimensions: int = Field(default=512, ge=1, le=16384)
    embedding_download_allowed: bool = False
    local_threads: int = Field(default=1, ge=1, le=4)
    local_batch_size: int = Field(default=8, ge=1, le=32)
    background_concurrency: int = Field(default=2, ge=1, le=4)
    model_timeout_seconds: int = Field(default=90, ge=5, le=600)
    learning_enabled: bool = False
    learning_topics: list[str] = Field(default_factory=list, max_length=12)
    learning_sources: list[str] = Field(default_factory=list, max_length=30)
    learning_segment_seconds: int = Field(default=60, ge=10, le=300)
    learning_extend_sleep: bool = False
    learning_sleep_max_seconds: int = Field(default=3600, ge=60, le=14400)
    drawing_daily_limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fallback(cls, value):
        if isinstance(value, dict) and "fallback_model" in value:
            value = dict(value)
            legacy = value.pop("fallback_model")
            value.setdefault("auxiliary_model", legacy)
        return value

    @field_validator("primary_model")
    @classmethod
    def ds_primary_only(cls, value: str) -> str:
        if "deepseek" not in value.casefold():
            raise ValueError("primary consciousness must use a DeepSeek model")
        return value

    @field_validator("group_id", "self_id", "owner_id")
    @classmethod
    def numeric_identifier(cls, value: str) -> str:
        if value and (not value.isascii() or not value.isdigit()):
            raise ValueError("QQ identifiers must be decimal strings")
        return value

    @field_validator("group_ids", "private_ids")
    @classmethod
    def valid_groups(cls, values: list[str]) -> list[str]:
        if any(not x or not x.isascii() or not x.isdigit() for x in values):
            raise ValueError("group IDs must be decimal strings")
        return list(dict.fromkeys(values))

    @field_validator("learning_sources")
    @classmethod
    def valid_learning_sources(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            match = re.fullmatch(r"https://(?:www\.)?bilibili\.com/video/(BV[a-zA-Z0-9]{10})/?", value.strip())
            if not match:
                raise ValueError("学习来源需要公开 BV 视频地址，分 P 请在队列中单独选择")
            result.append(f"https://www.bilibili.com/video/{match[1]}/")
        return list(dict.fromkeys(result))

    @field_validator("learning_topics")
    @classmethod
    def valid_learning_topics(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value.strip()) > 200 for value in values):
            raise ValueError("学习方向需为 1 至 200 字")
        return list(dict.fromkeys(value.strip() for value in values))

    @property
    def enabled_groups(self) -> list[str]:
        return list(dict.fromkeys([*([self.group_id] if self.group_id else []), *self.group_ids]))

    @property
    def conversation_keys(self) -> list[str]:
        private = [*([self.owner_id] if self.owner_private_enabled and self.owner_id else []), *self.private_ids]
        return [*self.enabled_groups, *("private:" + target for target in dict.fromkeys(private))]

    def allows_conversation(self, key: str) -> bool:
        return key in self.conversation_keys

    def scoped(self, key: str):
        # Internal snapshot only: legacy group_id column also holds private:<QQ> scopes.
        if not self.allows_conversation(key):
            raise ValueError("conversation is not enabled")
        return self.model_copy(update={"group_id": key})

    @field_validator("bedtime", "reboot_wakeup", "continuous_wakeup", "morning_cutoff")
    @classmethod
    def clock_time(cls, value: str) -> str:
        from datetime import time

        parsed = time.fromisoformat(value)
        if len(value) != 5 or parsed.second or parsed.tzinfo:
            raise ValueError("time must be HH:MM")
        return value

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value.strip()) > 40 for value in values):
            raise ValueError("aliases must contain 1–40 characters")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def ordered_routine(self):
        if not self.reboot_wakeup <= self.continuous_wakeup < self.bedtime:
            raise ValueError("routine requires reboot wake <= continuous wake < bedtime")
        return self

    @property
    def persona_fingerprint(self) -> str:
        return hashlib.sha256(json.dumps({"id": self.persona_id, "name": self.persona_name,
            "content": self.persona, "aliases": self.aliases}, ensure_ascii=False,
            sort_keys=True).encode()).hexdigest()

    @property
    def ready(self) -> bool:
        return bool(self.conversation_keys and self.persona.strip() and self.workbuddy_key.get_secret_value())

    def storage(self) -> dict:
        result = self.model_dump(mode="json")
        for name in ("workbuddy_key", "qq_token", "qq_ws_token", "embedding_key", "snowluma_password"):
            result[name] = getattr(self, name).get_secret_value()
        return result

    def public(self) -> dict:
        result = self.model_dump(mode="json")
        for name in ("workbuddy_key", "qq_token", "qq_ws_token", "embedding_key", "snowluma_password"):
            result.pop(name)
            result[f"{name}_set"] = bool(getattr(self, name).get_secret_value())
        return result
