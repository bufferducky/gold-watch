import asyncio
import fcntl
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .models import Kind, Settings, now_local
from .providers import Providers, SourceError
from .service import Service
from .store import SECRET_FIELDS, Store

STATIC = Path(__file__).parent / "static"
basic = HTTPBasic()
# HTTPX request logs include URLs, which can contain webhook tokens or provider API keys.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app(*, store=None, client=None, run_scheduler=True):
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "")

    @asynccontextmanager
    async def lifespan(app):
        if len(password) < 16:
            raise RuntimeError("请设置至少 16 位的 ADMIN_PASSWORD；可运行 scripts/setup.py 自动生成")
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        lock_file = (data_dir / "scheduler.lock").open("w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError("同一数据目录已有进程运行；请保持一个实例、一个 worker") from None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), follow_redirects=False,
                                         headers={"User-Agent": "GoldWatch/0.1 (+self-hosted gold briefing)"})
        task = None
        try:
            db = store or Store(data_dir / "gold-watch.db", os.environ["APP_ENCRYPTION_KEY"])
            db.settings()  # Fail at startup if the persisted encryption key does not match.
            db.recover()
            app.state.store = db
            app.state.service = Service(db, Providers(http), http)
            task = asyncio.create_task(app.state.service.loop()) if run_scheduler else None
            app.state.scheduler_task = task
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if client is None:
                await http.aclose()
            lock_file.close()

    app = FastAPI(title="Gold Watch", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(credentials: Annotated[HTTPBasicCredentials, Depends(basic)]):
        valid_user = secrets.compare_digest(credentials.username.encode(), username.encode())
        valid_password = secrets.compare_digest(credentials.password.encode(), password.encode())
        if not valid_user or not valid_password:
            raise HTTPException(401, "登录信息错误", headers={"WWW-Authenticate": 'Basic realm="Gold Watch"'})

    @app.middleware("http")
    async def request_guards(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if (request.headers.get("x-gold-watch") != "1"
                    or request.headers.get("sec-fetch-site") == "cross-site"):
                return JSONResponse({"detail": "请求来源无效"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def health():
        try:
            with app.state.store.db() as db:
                db.execute("SELECT 1").fetchone()
            task = app.state.scheduler_task
            if task is not None and task.done():
                raise RuntimeError("scheduler stopped")
            tick = app.state.service.last_tick
            if tick:
                from datetime import datetime
                if (now_local() - datetime.fromisoformat(tick)).total_seconds() > 180:
                    raise RuntimeError("scheduler stale")
        except Exception:
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return {"status": "ok"}

    @app.get("/", dependencies=[Depends(authenticate)])
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state", dependencies=[Depends(authenticate)])
    async def state():
        db = app.state.store
        settings = db.settings()
        latest, next_runs = {}, {}
        at = now_local()
        for kind in ("international", "domestic"):
            quotes = db.recent_quotes(kind, limit=1)
            latest[kind] = ({"quote": quotes[0].model_dump(mode="json"), "status": quotes[0].status()}
                            if quotes else None)
            schedule = getattr(settings, kind)
            if settings.scheduler_enabled and schedule.enabled:
                hour, minute = map(int, schedule.time.split(":"))
                next_at = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_at <= at:
                    next_at += timedelta(days=1)
                next_runs[kind] = next_at.isoformat()
            else:
                next_runs[kind] = None
        return {"settings": db.public_settings(), "latest": latest, "next_runs": next_runs,
                "history": db.history(), "server_time": at.isoformat(), "timezone": "Asia/Shanghai",
                "scheduler": {"last_tick": app.state.service.last_tick, "error": app.state.service.last_error}}

    @app.put("/api/settings", dependencies=[Depends(authenticate)])
    async def save_settings(body: dict):
        current = app.state.store.settings().model_dump(mode="json")
        clear = body.pop("clear_secrets", [])
        if not isinstance(clear, list) or any(name not in SECRET_FIELDS for name in clear):
            raise HTTPException(422, "无效的密钥清除字段")
        for name in SECRET_FIELDS:
            if name in clear:
                current[name] = ""
            elif body.get(name):
                current[name] = body[name]
        for key in current:
            if key not in SECRET_FIELDS and key in body:
                current[key] = body[key]
        try:
            settings = Settings.model_validate(current)
        except ValidationError as exc:
            # ValidationError's default JSON includes the submitted input; redact it.
            raise HTTPException(422, "；".join(err["msg"] for err in exc.errors(include_input=False))) from None
        app.state.store.save_settings(settings)
        return {"status": "saved", "settings": app.state.store.public_settings()}

    @app.post("/api/preview/{kind}", dependencies=[Depends(authenticate)])
    async def preview(kind: Kind):
        try:
            return await app.state.service.preview(kind)
        except SourceError as exc:
            raise HTTPException(502, str(exc)) from None
        except ValueError:
            raise HTTPException(422, "模板渲染失败，请检查模板长度和变量") from None

    @app.post("/api/send/{kind}", dependencies=[Depends(authenticate)])
    async def send(kind: Kind):
        if app.state.service.send_lock.locked():
            raise HTTPException(409, "已有推送正在处理，请稍候")
        result = await app.state.service.deliver(kind)
        return JSONResponse(result, status_code=200 if result["status"] == "success" else 502)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
