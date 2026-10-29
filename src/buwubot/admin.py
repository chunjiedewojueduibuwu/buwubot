"""Same-origin, local administration endpoints; all formal mutations use Coordinator."""

import hashlib
import json
import time
import uuid
import zipfile
from copy import copy
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .affect import EMOTIONS, AffectProfile, EmotionScores
from .channels import builtin_channels
from .config import Settings
from .consolidation import MemoryConsolidation
from .conversations import ConversationRef
from .memory import KNOWLEDGE_SCOPE, MemoryProposal, core_scope
from .memory_dynamics import DynamicsPolicy
from .memory_maintenance import MaintenancePlan, MemoryMaintenance
from .memory_migration import MemoryMigration
from .models import ModelClient, ModelUnavailable
from .store import Conflict


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    changes: dict
    confirm_evictions: bool = False
    eviction_preview: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CoreEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1,max_length=6000)
    tags: list[str] = Field(default_factory=list,max_length=20)


class CoreUnify(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    expected_core_versions: dict[str,int]
    persona: str = Field(min_length=1,max_length=6000)
    items: list[CoreEntry] = Field(min_length=1,max_length=100)


class SkillSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    changes: dict


class SkillEnabled(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    enabled: bool


class PersonaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=40)
    persona: str = Field(min_length=1, max_length=6000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class PersonaSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    persona_revision: int = Field(ge=1)


class LearningAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2000)
    part: int = Field(default=1, ge=1, le=10000)


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=6000)
    feeling_strength: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    feeling_valence: float | None = Field(default=None,ge=-1,le=1,allow_inf_nan=False,strict=True)
    feeling_emotions: EmotionScores | None = None
    feeling_arousal: float | None = Field(default=None,ge=0,le=1,allow_inf_nan=False,strict=True)
    significance: float | None = Field(default=None,ge=0,le=1,allow_inf_nan=False,strict=True)
    tier: str | None = Field(default=None, pattern=r"^(core|feeling|important|secondary|long_term|recent)$")
    tags: list[str] | None = None


class MoodPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    operation_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    reset: bool = False
    valence: float | None = Field(default=None,ge=-1,le=1,allow_inf_nan=False,strict=True)
    intensity: float | None = Field(default=None,ge=0,le=1,allow_inf_nan=False,strict=True)
    emotions: EmotionScores | None = None
    arousal: float | None = Field(default=None,ge=0,le=1,allow_inf_nan=False,strict=True)


class TeachingRecheck(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    memory_ids: list[str] = Field(min_length=1,max_length=24)


class MaintenanceApply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirm: Literal[True]


class MigrationPreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: int = Field(ge=1)


class MigrationApply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token: str = Field(pattern=r'^[a-f0-9]{32}$')
    confirm: Literal[True]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    config_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=6000)
    subject_id: str = Field(default="", pattern=r"^[0-9]{0,20}$")
    feeling_strength: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    feeling_valence: float | None = Field(default=None,ge=-1,le=1,allow_inf_nan=False,strict=True)
    feeling_emotions: EmotionScores | None = None
    feeling_arousal: float | None = Field(default=None,ge=0,le=1,allow_inf_nan=False,strict=True)
    significance: float | None = Field(default=None,ge=0,le=1,allow_inf_nan=False,strict=True)
    role_memory: bool = True
    tier: str = Field(default="important", pattern=r"^(core|feeling|important|secondary|long_term|recent)$")
    tags: list[str] = Field(default_factory=list, max_length=20)


class StickerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    category: str = Field(min_length=1, max_length=30)
    meaning: str = Field(min_length=1, max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=20)


def scope_key(state, conversation_id=None, *, allow_public=False, allow_core=False):
    cfg, _ = state.store.settings()
    key = conversation_id or cfg.group_id or next(iter(cfg.conversation_keys), "")
    if not key:
        return ""
    if allow_public and key == KNOWLEDGE_SCOPE:
        return key
    if allow_core and key == core_scope(cfg.persona_id):
        return key
    try:
        return ConversationRef.from_key(key).storage_key
    except ValueError:
        raise HTTPException(422, "会话标识格式无效") from None


def scoped_services(state, conversation_id=None, *, allow_public=False, allow_core=False):
    cfg, _ = state.store.settings()
    cfg = cfg.model_copy(update={"group_id": scope_key(state, conversation_id,
        allow_public=allow_public, allow_core=allow_core)})
    memory, stickers = copy(state.memory), copy(state.stickers)
    memory.settings, stickers.settings = cfg, cfg
    return memory, stickers


def register_admin(app: FastAPI):
    async def skill_change(state,operation):
        try:
            return await state.coordinator.commit(operation)
        except Conflict:
            raise HTTPException(409,'插件已安装或设置已更新，请刷新后核对') from None
        except KeyError:
            raise HTTPException(404,'技能插件不存在') from None
        except (ValueError,zipfile.BadZipFile):
            raise HTTPException(422,'插件包或设置无效，请核对清单、文件和参数') from None
        except OSError:
            raise HTTPException(503,'插件文件暂时无法访问，请稍后再试') from None

    @app.get('/api/skill-plugins')
    def skill_plugins(request: Request):
        return {'items':request.app.state.skill_plugins.list()}

    @app.post('/api/skill-plugins/install')
    async def install_skill(request: Request):
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > 8 * 1024 * 1024:
                raise HTTPException(413,'插件包不能超过 8 MiB')
            content.extend(chunk)
        state = request.app.state
        return await skill_change(state,lambda:state.skill_plugins.install(bytes(content)))

    @app.patch('/api/skill-plugins/{pid}/settings')
    async def skill_settings(pid: str,body: SkillSettings,request: Request):
        state = request.app.state
        return await skill_change(state,lambda:state.skill_plugins.configure(pid,body.changes,
                                  expected_revision=body.revision))

    @app.patch('/api/skill-plugins/{pid}/enabled')
    async def skill_enabled(pid: str,body: SkillEnabled,request: Request):
        state = request.app.state
        return await skill_change(state,lambda:state.skill_plugins.set_enabled(pid,body.enabled,
                                  expected_revision=body.revision))

    @app.delete('/api/skill-plugins/{pid}')
    async def uninstall_skill(pid: str,request: Request,revision: int = Query(ge=1)):
        state = request.app.state
        await skill_change(state,lambda:state.skill_plugins.uninstall(pid,expected_revision=revision))
        return {'removed':True}

    @app.get("/api/plugins")
    def plugins(request: Request):
        state = request.app.state
        cfg, _ = state.store.settings()
        registry = state.bot.channels if state.bot else builtin_channels()
        return {"items": registry.manifests(cfg),
                "available_tools": [{"id": "search", "name": "网页搜索", "installed": False},
                {"id": "video", "name": "视频读取", "installed": False},
                {"id": "drawing", "name": "绘图", "installed": False}]}

    @app.patch("/api/plugins/{plugin_id}/settings")
    async def plugin_settings(plugin_id: str, request: Request, body: ConfigPatch):
        state = request.app.state
        registry = state.bot.channels if state.bot else builtin_channels()
        if plugin_id not in registry.names:
            raise HTTPException(404, "活动范围插件不存在")
        if not set(body.changes) <= registry.configuration_fields(plugin_id):
            raise HTTPException(422, "只能修改此活动范围声明的设置，不能修改本体设置")
        return await patch_config(request, body)

    def learning_service(state):
        if not state.bot or not state.bot.learning:
            raise HTTPException(503, "学习服务尚未启动")
        return state.bot.learning

    @app.get("/api/learning")
    def learning_status(request: Request):
        return learning_service(request.app.state).listing()

    @app.post("/api/learning")
    async def learning_add(request: Request, body: LearningAdd):
        state = request.app.state
        service = learning_service(state)
        def add():
            try:
                return service.get(service.enqueue(body.url, body.part))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        return await state.coordinator.commit(add)

    @app.post("/api/learning/{item_id}/{action}")
    async def learning_control(item_id: str, action: str, request: Request):
        state = request.app.state
        service = learning_service(state)
        def change():
            try:
                return service.control(item_id, action)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        return await state.coordinator.commit(change)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/config")
    def config(request: Request):
        settings, revision = request.app.state.store.settings()
        version=request.app.state.memory.schema_version()
        public=settings.public()
        public['memory_policies']=settings.memory_policies.for_schema(version)
        return {"revision": revision, "settings": public,'memory_schema_version':version,
                'memory_dynamics_defaults':asdict(DynamicsPolicy())}

    @app.get('/api/mood')
    def mood(request: Request):
        state=request.app.state
        memory=copy(state.memory)
        memory.settings,revision=state.store.settings()
        now=time.time()
        return memory.mood.view(now=now)|{'config_revision':revision,'changes':memory.mood.changes(now=now),
                                         'emotion_labels':EMOTIONS}

    @app.post('/api/mood')
    async def update_mood(request: Request,body: MoodPatch):
        state=request.app.state
        def update():
            memory=copy(state.memory)
            memory.settings,revision=state.store.settings()
            if revision!=body.revision:raise Conflict('配置已更新，请刷新后调整心情')
            if (body.reset and (body.valence is not None or body.intensity is not None or body.emotions is not None or body.arousal is not None)
                    or not body.reset and (body.valence is None or body.intensity is None)):
                raise HTTPException(422,'调整需填写方向和强度，重置不携带分数')
            if not body.reset:
                try:AffectProfile(emotions=body.emotions or {},arousal=body.arousal).within(body.intensity)
                except ValueError:raise HTTPException(422,'具体情绪分量不能超过整体感受强度') from None
            key='mood-command:'+memory.settings.persona_id+':'+body.operation_id
            command=body.model_dump(exclude={'revision'}|({'emotions'} if body.emotions is None else set())|
                                    ({'arousal'} if body.arousal is None else set()))
            existing=state.store.get_state(key)
            if existing is not None and existing!=command:raise Conflict('同一操作编号的内容已变化')
            if existing is None:
                with state.store.transaction():
                    now=time.time()
                    memory.mood.reset(now=now)
                    if not body.reset:memory.mood.adjust(valence=body.valence,intensity=body.intensity,
                        emotions=body.emotions,arousal=body.arousal,
                        operation_id='admin:'+body.operation_id,now=now)
                    state.store.set_state(key,command)
                    state.store.event('memory','管理页面重置心情' if body.reset else '管理页面调整心情',
                                      {'persona_id':memory.settings.persona_id})
            return memory.mood.view(now=time.time())|{'config_revision':revision}
        return await state.coordinator.commit(update)

    @app.patch("/api/config")
    async def patch_config(request: Request, body: ConfigPatch):
        state = request.app.state
        if "persona_id" in body.changes:
            raise HTTPException(422, "请通过人格切换入口选择已有角色")
        current, _ = state.store.settings()
        try:
            updated = Settings.model_validate(current.storage() | body.changes)
        except ValidationError as exc:
            raise HTTPException(422, detail=exc.errors(include_input=False, include_context=False)) from None
        def save():
            selected = copy(state.stickers)
            selected.settings = updated
            with state.store.transaction():
                evictions = selected.eviction_ids()
                preview = hashlib.sha256(json.dumps(sorted(evictions)).encode()).hexdigest()
                if evictions and (not body.confirm_evictions or body.eviction_preview != preview):
                    raise HTTPException(409, detail={"eviction_count": len(evictions),
                        "eviction_preview": preview, "message": "收藏淘汰清单需确认；若已变化请重新保存确认"})
                revision = state.store.save_settings(updated, body.revision)
                if current.memory_policies!=updated.memory_policies:
                    state.memory.lifecycle.reconfigure(updated.memory_policies,now=time.time())
                    state.memory.feelings.reconfigure(updated.memory_policies.feeling,now=time.time())
                if current.mood_half_life_minutes!=updated.mood_half_life_minutes:
                    state.memory.mood.reconfigure(updated.mood_half_life_minutes,now=time.time())
                selected.enforce_limits()
                memory_fields={'memory_policies','memory_deleted_retention_days',
                    'memory_relation_expansion','memory_relation_depth','mood_half_life_minutes'}
                if any(getattr(current,key)!=getattr(updated,key) for key in set(body.changes)-memory_fields):
                    state.coordinator.queue.retry_attention()
                state.store.event("config", "管理页面更新配置", {"fields": list(body.changes)})
            state.memory.settings = updated
            state.stickers.settings = updated
            state.coordinator.settings = updated
            return revision

        revision = await state.coordinator.commit(save)
        if state.bot:
            state.bot.configuration_changed.set()
        version=state.memory.schema_version()
        public=updated.public();public['memory_policies']=updated.memory_policies.for_schema(version)
        return {"revision": revision, "settings": public,'memory_schema_version':version,
                'memory_dynamics_defaults':asdict(DynamicsPolicy())}

    @app.get("/api/personas")
    def personas(request: Request):
        store = request.app.state.store
        cfg, _ = store.settings()
        return {"items": store.personas(), "active_id": cfg.persona_id}

    @app.post('/api/persona-core/unify')
    async def unify_core(body: CoreUnify,request: Request):
        state=request.app.state
        def apply():
            with state.store.transaction():
                cfg,revision=state.store.settings()
                scope=core_scope(cfg.persona_id)
                current=state.store.rows("SELECT id,revision FROM memories WHERE group_id=? "
                    "AND persona_id=? AND tier='core' AND deleted=0",(scope,cfg.persona_id))
                if revision!=body.revision or {r['id']:r['revision'] for r in current}!=body.expected_core_versions:
                    raise Conflict('人格或核心记忆已变化，请重新核对')
                contents=[entry.content.strip() for entry in body.items]
                if (any(not content for content in contents) or len(set(contents))!=len(contents)
                        or sum(map(len,contents))>12000
                        or any(tag.startswith('persona_growth:') for item in body.items for tag in item.tags)):
                    raise HTTPException(422,'核心条目为空、重复、过长或包含保留标记')
                memory=copy(state.memory);memory.settings=cfg
                for row in current:
                    memory.delete(row['id'],expected_revision=row['revision'])
                for entry,content in zip(body.items,contents):
                    source='manual:'+uuid.uuid4().hex
                    memory.commit(MemoryProposal(group_id=scope,persona_id=cfg.persona_id,
                        tier='core',content=content,tags=entry.tags,source_message_ids=[source],
                        snapshot_epoch=memory.epoch(),origin_model='admin'),reviewed=True,manual=True,
                        source_snapshots=[{'id':source,'group_id':scope}])
                updated=cfg.model_copy(update={'persona':body.persona})
                saved=state.store.save_settings(updated,revision)
                state.store.event('persona','统合固定人设与可成长核心记忆',{'items':len(contents)})
            state.memory.settings=state.stickers.settings=state.coordinator.settings=updated
            return {'revision':saved,'core_count':len(contents)}
        result=await state.coordinator.commit(apply)
        if state.bot:
            state.bot.configuration_changed.set()
        return result

    @app.post("/api/personas")
    async def create_persona(request: Request, body: PersonaCreate):
        try:
            checked = Settings(persona=body.persona, aliases=body.aliases)
        except ValidationError:
            raise HTTPException(422, "称呼格式无效") from None
        return await request.app.state.coordinator.commit(lambda: request.app.state.store.create_persona(
            body.id, body.name.strip(), checked.persona, checked.aliases))

    @app.post("/api/personas/{pid}/switch")
    async def switch_persona(pid: str, request: Request, body: PersonaSwitch):
        state = request.app.state
        def switch():
            with state.store.transaction():
                cfg, revision = state.store.switch_persona(pid, body.revision, body.persona_revision)
                state.store.event("persona", "切换全局当前人格", {"persona_id": pid})
            state.coordinator.settings = cfg
            state.memory.settings = cfg
            state.stickers.settings = cfg
            return cfg, revision
        cfg, revision = await state.coordinator.commit(switch)
        if state.bot:
            state.bot.configuration_changed.set()
        return {"settings": cfg.public(), "revision": revision}

    @app.put("/api/embedding/settings")
    async def embedding_settings(request: Request, body: ConfigPatch):
        permitted = {"embedding_provider", "local_embedding_model", "embedding_url", "embedding_model",
                     "embedding_key", "embedding_dimensions", "embedding_download_allowed",
                     "local_threads", "local_batch_size"}
        if not set(body.changes) <= permitted:
            raise HTTPException(422, "only embedding settings may be changed here")
        return await patch_config(request, body)

    @app.post("/api/embedding/reindex")
    async def reindex(request: Request):
        state = request.app.state
        if not state.bot or not state.bot.index or state.bot.settings.embedding_provider=="none":
            raise HTTPException(409, "请先配置并开启语义检索")

        def clear_progress():
            state.store.set_state("index_rebuild:"+state.bot.index.fingerprint,True)

        await state.coordinator.commit(clear_progress)
        state.bot.configuration_changed.set()
        return {"state": "queued"}

    @app.post("/api/jobs/{jid}/retry")
    async def retry_job(jid: str, request: Request):
        state = request.app.state

        def retry():
            job = state.coordinator.queue.get(jid)
            if not job:
                raise HTTPException(404, "任务不存在")
            if job["state"] not in {"needs_attention", "retry_wait"}:
                raise Conflict("只能重试等待恢复或需要检查的任务")
            with state.store.transaction() as db:
                db.execute("UPDATE jobs SET state='pending',next_attempt=0 WHERE id=?", (jid,))
            state.store.event("job", "管理页面请求重试任务", {"job_id": jid})

        await state.coordinator.commit(retry)
        if state.bot and state.bot.models:
            state.bot.models.reset()
        return {"state": "pending"}

    @app.post('/api/memory-maintenance/preview')
    async def preview_maintenance(body: MaintenancePlan, request: Request):
        try:
            return MemoryMaintenance(request.app.state.memory).preview(body.model_dump())
        except Conflict:
            raise
        except ValueError:
            raise HTTPException(422,'维护计划格式无效或没有实际变更') from None

    @app.post('/api/memory-maintenance/review')
    async def review_maintenance(body: MaintenancePlan, request: Request):
        state=request.app.state
        service=MemoryMaintenance(state.memory)
        cfg,revision=state.store.settings()
        def allowed():
            current,current_revision=state.store.settings()
            return not current.runtime_enabled and current_revision==revision
        # An explicit management review may use AI while normal bot activity stays paused.
        models=ModelClient(cfg,allowed=allowed)
        try:
            return await service.review(body.model_dump(),models,state.coordinator)
        except Conflict:
            raise
        except ModelUnavailable:
            raise HTTPException(503,'独立审核暂不可用，维护计划已保留，尚未修改记忆') from None
        except ValueError:
            raise HTTPException(422,'审核未完整覆盖计划或返回格式无效，尚未修改记忆') from None
        finally:
            await models.close()

    @app.get('/api/memory-maintenance/{token}')
    def get_maintenance(token: str, request: Request):
        try:
            result=MemoryMaintenance(request.app.state.memory).get(token)
        except ValueError:
            raise HTTPException(422,'维护计划编号无效') from None
        if not result:
            raise HTTPException(404,'维护计划不存在')
        return result

    @app.post('/api/memory-maintenance/apply')
    async def apply_maintenance(body: MaintenanceApply, request: Request):
        state=request.app.state
        try:
            return await state.coordinator.commit(lambda:MemoryMaintenance(state.memory).apply(body.token))
        except Conflict:
            raise
        except ValueError:
            raise HTTPException(422,'维护计划无法应用，未提交本批变更') from None

    @app.post('/api/memory-migration/preview')
    async def preview_memory_migration(body: MigrationPreview,request: Request):
        state=request.app.state
        def prepare():
            if state.store.settings()[1]!=body.revision:
                raise Conflict('配置已变化，请刷新后重新预览')
            return MemoryMigration(state.memory).preview(now=time.time())
        return await state.coordinator.commit(prepare)

    @app.post('/api/memory-migration/apply')
    async def apply_memory_migration(body: MigrationApply,request: Request):
        state=request.app.state
        return await state.coordinator.commit(lambda:MemoryMigration(state.memory).apply(body.token))

    @app.post('/api/memory-teaching/recheck')
    async def recheck_teaching(body: TeachingRecheck,request: Request,conversation_id: str | None = None):
        state=request.app.state
        def enqueue():
            _cfg,revision=state.store.settings()
            if revision!=body.revision:
                raise Conflict('配置已更新，请刷新后再重查教导')
            memory,_=scoped_services(state,scope_key(state,conversation_id))
            return MemoryConsolidation(memory).enqueue_teaching(state.coordinator.queue,body.memory_ids,
                mutation_since=state.store.get_state('memory_consolidation_mutation_since',0))
        try:
            jid=await state.coordinator.commit(enqueue)
            return {'state':'queued' if jid else 'no_change','job_id':jid}
        except Conflict:
            raise
        except ValueError:
            raise HTTPException(422,'只能重查当前会话中有主号原消息依据、未受人工保护的近期教导') from None

    @app.get("/api/memories")
    def memories(request: Request, query: str = "", tier: str | None = None,
                 subject: str | None = None, offset: int = Query(0, ge=0),
                 limit: int = Query(50, ge=1, le=200), conversation_id: str | None = None):
        state = request.app.state
        settings, revision = state.store.settings()
        selected = (core_scope(settings.persona_id) if tier == 'core' else
                    scope_key(state, conversation_id, allow_public=True))
        if tier == 'context':
            memory, _ = scoped_services(state, selected, allow_public=True)
            items = memory.context_memory()
            items = [item for item in items if (not query or query.casefold() in
                     json.dumps(item, ensure_ascii=False).casefold()) and (not subject or
                     any(m['user_id'] == subject for m in item['messages']))]
            return {'items':items[offset:offset + limit], 'total':len(items),
                    'core':settings.persona, 'config_revision':revision}
        memory, _ = scoped_services(state, selected, allow_public=True, allow_core=True)
        consolidation = MemoryConsolidation(memory)
        def view(item):
            reading=memory.with_reading_policy(item)
            return (reading | memory.lifecycle.observation(item,reading,memory.settings.memory_policies)
                    | {'relations': consolidation.relations(item['id'])}
                    | memory.lifecycle.lineage(memory, item))
        if query:
            items = memory.search_text(query, group_id=selected, limit=200)
            items = [view(x) for x in items if (not tier or x["tier"] == tier)
                     and (not subject or x["subject_id"] == subject)]
            return {"items": items[offset:offset + limit], "total": len(items),
                    "core": settings.persona, "config_revision": revision}
        where, args = "group_id=? AND deleted=0 AND (tier!='feeling' OR persona_id=?)", [selected, settings.persona_id]
        for field, value in (("tier", tier), ("subject_id", subject)):
            if value:
                where += f" AND {field}=?"
                args.append(value)
        rows = state.store.rows(f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC "
                                "LIMIT ? OFFSET ?", [*args, limit, offset])
        count = state.store.rows(f"SELECT count(*) n FROM memories WHERE {where}", args)[0]["n"]
        return {"items": [view(memory.decode(row)) for row in rows], "total": count,
                "core": settings.persona, "config_revision": revision}

    @app.post("/api/memories")
    async def create_memory(body: MemoryCreate, request: Request, conversation_id: str | None = None):
        state = request.app.state
        def create():
            cfg, revision = state.store.settings()
            if revision != body.config_revision:
                raise Conflict("配置已更新，请刷新页面后再新增记忆")
            selected = core_scope(cfg.persona_id) if body.tier == 'core' else scope_key(state, conversation_id)
            if body.tier == 'core' and (not body.role_memory or body.subject_id):
                raise HTTPException(422, "核心记忆属于当前角色，不关联某个聊天人物")
            if body.tier != 'core' and not cfg.allows_conversation(selected):
                raise HTTPException(422, "只能向已授权的聊天会话新增记忆")
            memory, _ = scoped_services(state, selected, allow_core=True)
            source = "manual:" + uuid.uuid4().hex
            proposal = MemoryProposal(group_id=selected, subject_id=body.subject_id,
                persona_id=cfg.persona_id if body.role_memory else None,
                content=body.content, tier=body.tier, tags=body.tags, confidence=1, feeling_strength=body.feeling_strength,
                memory_schema_version=memory.schema_version(),
                significance=body.significance,feeling_valence=body.feeling_valence,
                feeling_emotions=body.feeling_emotions,feeling_arousal=body.feeling_arousal,
                source_message_ids=[source], origin_model="admin", snapshot_epoch=memory.epoch())
            mid = memory.commit(proposal, reviewed=True, manual=True,
                source_snapshots=[{"id":source,"group_id":selected,"origin":"admin"}])
            state.store.event("memory", "人工新增记忆", {"memory_id":mid,"origin":"admin"})
            return memory.get(mid)
        try:
            return await state.coordinator.commit(create)
        except Conflict:
            raise
        except ValueError:
            raise HTTPException(422, "记忆无法保存，请核对内容和相关人物 QQ") from None

    @app.patch("/api/memories/{mid}")
    async def edit_memory(mid: str, body: MemoryPatch, request: Request):
        state = request.app.state
        current = state.memory.get(mid)
        if not current:
            raise HTTPException(404, "记忆不存在")
        memory, _ = scoped_services(state, current["group_id"], allow_public=True, allow_core=True)
        if (current['tier'] == 'core') != ((body.tier or current['tier']) == 'core'):
            raise HTTPException(422, "核心记忆与会话记忆的范围不同，请分别管理")
        try:
            await state.coordinator.commit(lambda: memory.edit(
                mid, content=body.content, expected_revision=body.revision, tier=body.tier, tags=body.tags,
                feeling_strength=body.feeling_strength,significance=body.significance,
                feeling_valence=body.feeling_valence,feeling_emotions=body.feeling_emotions,
                **({'feeling_arousal':body.feeling_arousal} if 'feeling_arousal' in body.model_fields_set else {})))
        except Conflict:
            raise
        except ValueError:
            raise HTTPException(422, "记忆无法保存，请核对内容与范围") from None
        return state.memory.get(mid)

    @app.delete("/api/memories/{mid}")
    async def delete_memory(mid: str, request: Request, revision: int = Query(ge=1)):
        state = request.app.state
        current = state.memory.get(mid)
        if not current:
            raise HTTPException(404, "记忆不存在")
        memory, _ = scoped_services(state, current["group_id"], allow_public=True, allow_core=True)
        await state.coordinator.commit(lambda: memory.delete(mid, expected_revision=revision))
        return {"deleted": True}

    @app.get("/api/stickers")
    def stickers(request: Request, category: str | None = None, offset: int = Query(0, ge=0),
                 limit: int = Query(50, ge=1, le=200), conversation_id: str | None = None):
        _, selected = scoped_services(request.app.state, conversation_id)
        items = selected.list(category=category)
        return {"items": items[offset:offset + limit], "total": len(items)}

    @app.get("/api/sticker-categories")
    def categories(request: Request, conversation_id: str | None = None):
        _, selected = scoped_services(request.app.state, conversation_id)
        values = {}
        for item in selected.list():
            values[item["category"]] = values.get(item["category"], 0) + 1
        return {"items": [{"name": name, "count": count} for name, count in sorted(values.items())]}

    @app.patch("/api/stickers/{digest}")
    async def edit_sticker(digest: str, request: Request, body: StickerPatch,
                           conversation_id: str | None = None):
        state = request.app.state
        _, selected = scoped_services(state, conversation_id)
        await state.coordinator.commit(lambda: selected.edit(
            digest, body.model_dump(exclude={"revision"}), body.revision))
        return {"updated": True}

    @app.delete("/api/stickers/{digest}")
    async def delete_sticker(digest: str, request: Request, revision: int = Query(ge=1),
                             conversation_id: str | None = None):
        state = request.app.state
        _, selected = scoped_services(state, conversation_id)
        await state.coordinator.commit(lambda: selected.delete(digest, revision))
        return {"deleted": True}

    @app.get("/api/media/{digest}")
    def media(digest: str, request: Request):
        try:
            path = request.app.state.media.path(digest)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "image not found") from None
        return FileResponse(path)

    @app.get("/api/tools")
    def tools(request: Request):
        return {"items": request.app.state.tools.definitions()}

    @app.get("/api/members")
    def members(request: Request, conversation_id: str | None = None):
        state = request.app.state
        return {"items": state.memory.archive.members(scope_key(state, conversation_id))}

    @app.get("/api/{resource}")
    def records(resource: str, request: Request, kind: str | None = None,
                state_filter: str | None = Query(None, alias="state"),
                pending_only: bool = False,
                user_id: str | None = None, since: float | None = None, until: float | None = None,
                conversation_id: str | None = None,
                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        if resource not in {"messages", "events", "jobs"}:
            raise HTTPException(404)
        state = request.app.state
        where, args = [], []
        time_column = "created_at" if resource == "jobs" else "time"
        if resource == "messages":
            where.append("group_id=?")
            args.append(scope_key(state, conversation_id))
            if user_id:
                where.append("user_id=?")
                args.append(user_id)
        if kind and resource != "messages":
            where.append("kind=?")
            args.append(kind)
        if state_filter and resource == "jobs":
            where.append("state=?")
            args.append(state_filter)
        if pending_only and resource == "jobs":
            where.append("state IN ('pending','running','retry_wait','needs_attention')")
        for operator, value in ((">=", since), ("<=", until)):
            if value is not None:
                where.append(f"{time_column}{operator}?")
                args.append(value)
        clause = " AND ".join(where) or "1=1"
        rows = state.store.rows(f"SELECT * FROM {resource} WHERE {clause} "
                                f"ORDER BY {time_column} DESC LIMIT ? OFFSET ?", [*args, limit, offset])
        total = state.store.rows(f"SELECT count(*) n FROM {resource} WHERE {clause}", args)[0]["n"]
        items = []
        for row in rows:
            item = dict(row)
            if resource == "messages":
                item["message"] = json.loads(item.pop("payload"))
            elif resource == "jobs":
                item.pop("payload")
            else:
                item["details"] = json.loads(item["details"])
            items.append(item)
        return {"items": items, "total": total}
