"""Optional CPU/API embeddings and a rebuildable, explicitly configured Mem0 index."""

import asyncio
import hashlib
import json
import math
import multiprocessing
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings
from .store import Store


async def _in_thread(operation, *args, **kwargs):
    # Cancellation must not release the index lock while its thread still touches
    # Qdrant/SQLite; shutdown closes those resources only after the thread exits.
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def normalized_average(vectors: list[list[float]]) -> list[float]:
    if not vectors or not vectors[0]:
        raise ValueError("empty embedding")
    dimension = len(vectors[0])
    if any(len(row) != dimension for row in vectors):
        raise ValueError("inconsistent embedding dimensions")
    mean = [sum(row[i] for row in vectors) / len(vectors) for i in range(dimension)]
    norm = math.sqrt(sum(x * x for x in mean))
    if not norm or not all(math.isfinite(x) for x in mean):
        raise ValueError("invalid embedding values")
    return [x / norm for x in mean]


def _local_worker(pipe, settings: dict, cache: str):
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = str(settings["local_threads"])
    os.environ["HF_HOME"] = str(Path(cache)/".hf")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(settings["local_threads"])
        torch.set_num_interop_threads(1)
        model = SentenceTransformer(settings["local_embedding_model"], device="cpu",
                                    cache_folder=cache,
                                    local_files_only=not settings["embedding_download_allowed"])
        pipe.send({"ready": True,"device":str(next(model.parameters()).device),
                   "cuda_build":torch.version.cuda,"threads":torch.get_num_threads()})
        while True:
            text = pipe.recv()
            if text is None:
                break
            ids = model.tokenizer.encode(text, add_special_tokens=False)
            window = min(510, model.max_seq_length - 2)
            stride = max(1, window - 64)
            chunks = [model.tokenizer.decode(ids[i:i + window])
                      for i in range(0, max(1, len(ids)), stride)]
            vectors = model.encode(chunks, batch_size=settings["local_batch_size"],
                                   normalize_embeddings=True, show_progress_bar=False).tolist()
            pipe.send({"vector": normalized_average(vectors)})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:  # noqa: BLE001 -- child boundary reports failures to its parent
        try:
            pipe.send({"error": type(exc).__name__ +
                       ": 本地模型未就绪；请安装 local-embedding 依赖并允许首次下载，或使用已缓存模型。"})
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


class LocalEncoder:
    def __init__(self, settings: Settings, cache: Path):
        self.settings, self.cache = settings, cache
        self.process = None
        self.pipe = None
        self.details = {}
        self._lock = threading.Lock()

    def start(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.pipe = parent
        worker_settings = {name: getattr(self.settings, name) for name in (
            "local_threads", "local_batch_size", "local_embedding_model", "embedding_download_allowed")}
        self.process = context.Process(target=_local_worker,
                                       args=(child, worker_settings, str(self.cache)),
                                       daemon=True, name="BuwuBot CPU embedding")
        self.process.start()
        child.close()
        if not parent.poll(300):
            self.close()
            raise RuntimeError("本地模型加载超时")
        result = parent.recv()
        if not result.get("ready"):
            self.close()
            raise RuntimeError(result.get("error", "本地模型加载失败"))
        self.details = result

    def embed(self, text: str, memory_action=None) -> list[float]:
        with self._lock:
            if not self.process or not self.process.is_alive():
                raise RuntimeError("本地模型已关闭")
            self.pipe.send(text)
            if not self.pipe.poll(60):
                raise TimeoutError("本地检索超时")
            result = self.pipe.recv()
            if "vector" not in result:
                raise RuntimeError(result.get("error", "本地检索失败"))
            return result["vector"]

    def close(self):
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            self.process = None
        if self.pipe:
            self.pipe.close()
            self.pipe = None


class APIEncoder:
    def __init__(self, settings: Settings):
        if not settings.embedding_url or not settings.embedding_model:
            raise ValueError("外部 embedding URL 和模型名称尚未配置")
        self.settings = settings
        local = urlparse(settings.embedding_url).hostname in {"localhost", "127.0.0.1", "::1"}
        self.http = httpx.Client(timeout=30, trust_env=not local)

    def embed(self, text: str, memory_action=None) -> list[float]:
        # Do not silently drop the suffix of long memories on unknown provider token limits.
        chunks = [text[i:i + 1200] for i in range(0, max(1, len(text)), 1100)]
        response = self.http.post(self.settings.embedding_url.rstrip("/") + "/embeddings",
                                  headers={"Authorization": "Bearer " +
                                           self.settings.embedding_key.get_secret_value()},
                                  json={"model": self.settings.embedding_model, "input": chunks})
        if response.status_code != 200:
            raise RuntimeError(f"embedding HTTP {response.status_code}")
        rows = sorted(response.json()["data"], key=lambda item: item["index"])
        if len(rows) != len(chunks):
            raise ValueError("embedding response omitted a text chunk")
        vectors = [row["embedding"] for row in rows]
        if any(len(row) != self.settings.embedding_dimensions for row in vectors):
            raise ValueError("embedding dimensions differ from configured index")
        return normalized_average(vectors)

    def close(self):
        self.http.close()


class EmbeddingIndex:
    def __init__(self, store: Store, settings: Settings, data_dir: Path, *, encoder=None):
        self.store, self.settings, self.data_dir = store, settings, Path(data_dir)
        self.encoder = encoder
        self.memory = None
        self._lock = asyncio.Lock()
        self.status = {"state": "disabled", "loaded": False, "error": None}
        model = settings.local_embedding_model if settings.embedding_provider == "local" else (
            settings.embedding_url + ":" + settings.embedding_model)
        dims = 512 if settings.embedding_provider == "local" else settings.embedding_dimensions
        self.dimensions = dims
        self.fingerprint = hashlib.sha256(
            f"{settings.embedding_provider}:{model}:{dims}:chunks-v1".encode()
        ).hexdigest()[:20]

    async def start(self):
        if self.settings.embedding_provider == "none":
            return
        self.status.update(state="loading", loaded=False, error=None)
        async with self._lock:
            try:
                await _in_thread(self._open)
                self.status.update(state="ready", loaded=True)
            except Exception as exc:  # noqa: BLE001 -- optional index must not prevent text retrieval
                if self.encoder:
                    self.encoder.close()
                self.status.update(state="error", loaded=False, error=str(exc)[:300])
                self.store.event("index", "语义索引未就绪，文本检索继续可用",
                                 {"error_type": type(exc).__name__})

    def _open(self):
        if self.encoder is None:
            if self.settings.embedding_provider == "local":
                self.encoder = LocalEncoder(self.settings, self.data_dir / "models")
                self.encoder.start()
            else:
                self.encoder = APIEncoder(self.settings)
        os.environ["MEM0_TELEMETRY"] = "false"
        os.environ["MEM0_DIR"] = str(self.data_dir / "mem0")
        from mem0 import Memory

        directory = self.data_dir / "indexes" / self.fingerprint
        if self.store.get_state("index_rebuild:"+self.fingerprint,False):
            self._quarantine(directory)
        fresh=not (directory/"qdrant"/"meta.json").exists()
        if directory.exists() and self._corrupt(directory):
            self._quarantine(directory)
            fresh=True
        directory.mkdir(parents=True, exist_ok=True)
        config={
            "llm": {"provider": "openai", "config": {
                "model": self.settings.primary_model, "api_key": "unused-infer-false",
                "openai_base_url": "http://127.0.0.1:9/v1"}},
            "embedder": {"provider": "openai", "config": {
                "model": "replaced-by-explicit-encoder", "api_key": "unused",
                "openai_base_url": "http://127.0.0.1:9/v1", "embedding_dims": self.dimensions}},
            "vector_store": {"provider": "qdrant", "config": {
                "collection_name": "buwubot", "path": str(directory / "qdrant"),
                "embedding_model_dims": self.dimensions}},
            "history_db_path": str(directory / "history.db"),
        }
        corrupted=False
        try:
            self.memory=Memory.from_config(config)
        except (json.JSONDecodeError,sqlite3.DatabaseError):
            corrupted=True
        if corrupted:
            self._quarantine(directory)
            directory.mkdir(parents=True,exist_ok=True)
            self.memory=Memory.from_config(config)
            fresh=True
        if fresh:
            with self.store.transaction() as db:
                db.execute("DELETE FROM index_sync WHERE fingerprint=?",(self.fingerprint,))
        self.store.set_state("index_rebuild:"+self.fingerprint,False)
        # Mem0 1.0.11 eagerly creates its embedder. Replace that inert client before any operation.
        # No Memory() defaults, no LLM inference, no hidden embedding SDK retries.
        self.memory.embedding_model = self.encoder

    @staticmethod
    def _corrupt(directory: Path) -> bool:
        # Inspect before SDK construction: on Windows a failed constructor can
        # retain SQLite handles and prevent quarantining the broken directory.
        try:
            meta=directory/"qdrant"/"meta.json"
            if meta.exists():
                json.loads(meta.read_text(encoding="utf-8"))
            for path in [*directory.rglob("*.sqlite"),*directory.glob("*.db")]:
                with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as db:
                    if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                        return True
        except (json.JSONDecodeError,sqlite3.DatabaseError):
            return True
        return False

    def _quarantine(self,directory: Path):
        base=(self.data_dir/"indexes").resolve()
        target=directory.with_name(directory.name+".corrupt-"+str(time.time_ns()))
        if directory.resolve().parent!=base or target.resolve().parent!=base:
            raise ValueError("index recovery path is outside the index directory")
        if directory.exists():
            directory.rename(target)
        with self.store.transaction() as db:
            db.execute("DELETE FROM index_sync WHERE fingerprint=?",(self.fingerprint,))
        self.store.event("index","派生索引已隔离，正在从正式记忆重建",{"fingerprint":self.fingerprint})

    async def search(self, query: str, group_id: str) -> list[dict]:
        if self.status["state"] != "ready":
            return []
        async with self._lock:
            result = await _in_thread(self.memory.search, query, user_id=group_id, limit=20)
        return [row["metadata"]|({'similarity_score':row['score']} if 'score' in row else {})
                for row in result["results"] if row.get("metadata")]

    async def sync_pending(self, *, limit: int = 20) -> int:
        if self.status["state"] != "ready":
            return 0
        async with self._lock:
            return await _in_thread(self._sync, limit)

    def _sync(self, limit: int) -> int:
        rows = self.store.rows(
            "SELECT o.* FROM index_outbox o WHERE NOT EXISTS "
            "(SELECT 1 FROM index_sync s WHERE s.fingerprint=? AND s.outbox_id=o.id) "
            "ORDER BY o.id LIMIT ?", (self.fingerprint, limit),
        )
        completed = 0
        for row in rows:
            found_rows = self.store.rows("SELECT * FROM memories WHERE id=?", (row["memory_id"],))
            current = found_rows[0] if found_rows else None
            if current:
                found = self.memory.get_all(user_id=current["group_id"],
                                            filters={"app_id": current["id"]}, limit=200)["results"]
                if current["deleted"]:
                    for old in found:
                        self.memory.delete(old["id"])
                else:
                    metadata = {"app_id": current["id"], "revision": current["revision"],
                                "group_id": current["group_id"], "subject_id": current["subject_id"],
                                "tier": current["tier"]}
                    if found:
                        self.memory.update(found[0]["id"], current["content"], metadata=metadata)
                        for extra in found[1:]:
                            self.memory.delete(extra["id"])
                    else:
                        self.memory.add(current["content"], user_id=current["group_id"],
                                        metadata=metadata, infer=False)
            with self.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO index_sync VALUES(?,?)",
                           (self.fingerprint, row["id"]))
                db.execute("UPDATE index_outbox SET state='done',error=NULL WHERE id=?", (row["id"],))
            completed += 1
        return completed

    async def close(self):
        # Termination interrupts an in-flight CPU inference rather than retaining its RAM indefinitely.
        if isinstance(self.encoder, LocalEncoder):
            self.encoder.close()
        async with self._lock:
            if self.memory:
                await asyncio.to_thread(self.memory.vector_store.client.close)
                await asyncio.to_thread(self.memory.close)
                self.memory = None
            if self.encoder:
                self.encoder.close()
                self.encoder = None
        self.status.update(state="disabled", loaded=False)
