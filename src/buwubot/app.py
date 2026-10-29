"""Local-only administration server. Unconfigured startup performs no AI work."""

import argparse
import faulthandler
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .admin import register_admin
from .coordinator import Coordinator
from .engine import BotEngine
from .memory import MemoryService
from .runtime import InstanceLock
from .skill_plugins import SkillPlugins
from .stickers import MediaStore, StickerService
from .store import Store
from .tools import ToolRegistry

logger = logging.getLogger(__name__)
# faulthandler keeps a file descriptor, not a Python reference. Keep it alive
# through interpreter shutdown; main() is called once by the CLI.
_fault_log = None


def create_app(data_dir: Path | None = None, *, run_bot: bool = True, port: int = 8765) -> FastAPI:
    directory = Path(data_dir or "data").resolve()

    @asynccontextmanager
    async def lifespan(app):
        with InstanceLock(directory / "bot.lock",port=port):
            store = Store(directory / "bot.db")
            app.state.store = store
            app.state.bot = None
            settings, _ = store.settings()
            app.state.coordinator = Coordinator(store, None, settings)
            app.state.memory = MemoryService(store, settings)
            app.state.media = MediaStore(store, directory / "media")
            app.state.stickers = StickerService(store, settings, app.state.media)
            app.state.tools = ToolRegistry()
            app.state.skill_plugins = SkillPlugins(store,app.state.tools,directory / 'skills',media=app.state.media)
            try:
                if run_bot:
                    app.state.bot = BotEngine(store, app.state.coordinator, app.state.memory,
                                             app.state.media, app.state.stickers, app.state.tools,
                                             directory)
                    await app.state.bot.start()
                yield
            finally:
                if app.state.bot:
                    await app.state.bot.close()
                store.close()

    app = FastAPI(title="助手", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"detail": "local access only"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "cross-site writes are not allowed"}, status_code=403)
            content_type = request.headers.get('content-type','').split(';')[0].strip()
            package_upload = (request.method == 'POST' and request.url.path == '/api/skill-plugins/install'
                              and content_type == 'application/zip')
            if content_type != 'application/json' and not package_upload:
                return JSONResponse({"detail": "JSON request required"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/api/status")
    def status(request: Request):
        store = request.app.state.store
        settings, revision = store.settings()
        result = {
            "mode": "setup" if not settings.ready else "not_started",
            "configured": settings.ready,
            "preview_mode": not run_bot,
            "config_revision": revision,
            "embedding_loaded": False,
            "pending_jobs": store.pending_count(),
            "instance_pid": os.getpid(),
        }
        if request.app.state.bot:
            result.update(request.app.state.bot.status())
        return result

    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    @app.post("/api/runtime/stop")
    async def shutdown():
        from fastapi import HTTPException

        callback = getattr(app.state,"shutdown",None)
        if not callback:
            raise HTTPException(409,"Service lifecycle is managed externally")
        logger.info("event=management_stop_requested pid=%s", os.getpid())
        app.state.shutdown_requested = True
        callback()
        return {"state":"stopping"}

    register_admin(app)
    return app


def main():
    global _fault_log
    import uvicorn

    parser = argparse.ArgumentParser(description="BuwuBot local QQ companion")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logs = args.data_dir.resolve()/"logs"
    logs.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[RotatingFileHandler(logs/"service.log",maxBytes=2*1024*1024,
                                                      backupCount=3,encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)  # Resource URLs can contain short-lived signatures.
    logger.info("event=service_start pid=%s port=%s", os.getpid(), args.port)
    try:
        # Preserve an existing handler (including pytest's) and its output target.
        if not faulthandler.is_enabled():
            _fault_log = (logs / "fault.log").open("a", encoding="utf-8")
            faulthandler.enable(file=_fault_log, all_threads=True)
        logger.info("event=fault_handler pid=%s target=%s", os.getpid(),
                    "fault.log" if _fault_log else "existing_handler")
        app = create_app(args.data_dir,port=args.port)
        server = uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=args.port,workers=1,
                                               log_config=None,access_log=False))
        app.state.shutdown = lambda:setattr(server,"should_exit",True)
        server.run()
    except KeyboardInterrupt:
        logger.info("event=service_exit pid=%s reason=keyboard_interrupt", os.getpid())
        raise
    except SystemExit as exc:
        logger.info("event=service_exit pid=%s reason=system_exit code=%r", os.getpid(), exc.code)
        raise
    except BaseException:
        logger.exception("event=service_exit pid=%s reason=exception", os.getpid())
        raise
    else:
        logger.info("event=service_exit pid=%s reason=%s", os.getpid(),
                    "management_stop" if getattr(app.state, "shutdown_requested", False)
                    else "server_returned")


if __name__ == "__main__":
    main()
