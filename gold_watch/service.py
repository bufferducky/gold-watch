import asyncio
import logging
from datetime import datetime, timedelta
from uuid import uuid4

from .models import SHANGHAI, Quote, now_local
from .providers import Providers, SourceError
from .reports import SendFailure, SendUncertain, render, send_dingtalk
from .store import Store

logger = logging.getLogger(__name__)


def due_slots(settings, at: datetime):
    """Recompute from persisted settings, with bounded recovery across midnight."""
    if not settings.scheduler_enabled:
        return []
    due = []
    for kind in ("international", "domestic"):
        schedule = getattr(settings, kind)
        if not schedule.enabled:
            continue
        hour, minute = map(int, schedule.time.split(":"))
        for day in (at.date() - timedelta(days=1), at.date()):
            slot = datetime.combine(day, datetime.min.time(), SHANGHAI).replace(hour=hour, minute=minute)
            delay = (at - slot).total_seconds()
            if 0 <= delay <= settings.catchup_minutes * 60:
                due.append((kind, slot.isoformat()))
    return due


class Service:
    def __init__(self, store: Store, providers: Providers, client):
        self.store = store
        self.providers = providers
        self.client = client
        self.refresh_locks = {kind: asyncio.Lock() for kind in ("international", "domestic")}
        self.send_lock = asyncio.Lock()
        self.last_tick = None
        self.last_error = ""

    async def collect(self, kind: str) -> Quote:
        async with self.refresh_locks[kind]:
            settings = self.store.settings()
            try:
                # The whole provider chain has a deadline, even if each request retries.
                async with asyncio.timeout(65):
                    quote = await self.providers.fetch(kind, settings)
                self.store.save_quote(quote)
                return quote
            except (SourceError, TimeoutError) as exc:
                reason = str(exc) if isinstance(exc, SourceError) else "行情采集超过总时限"
                cached = self.store.recent_quotes(kind, limit=1)
                if cached and settings.allow_fallback:
                    quote = cached[0].model_copy(deep=True)
                    quote.warnings = ["本次采集失败，使用本地历史缓存：" + reason] + quote.warnings
                    return quote
                raise SourceError(reason) from None

    async def preview(self, kind: str, refresh=True):
        quotes = self.store.recent_quotes(kind, limit=1)
        quote = await self.collect(kind) if refresh or not quotes else quotes[0]
        settings = self.store.settings()
        schedule = getattr(settings, kind)
        message = render(quote, schedule, settings, self.store.previous_quote(quote))
        return {"quote": quote.model_dump(mode="json"), "status": quote.status(),
                "title": schedule.title, "message": message}

    async def deliver(self, kind: str, slot: str | None = None):
        async with self.send_lock:
            slot = slot or f"manual:{uuid4()}"
            delivery_id = self.store.claim(kind, slot)
            if delivery_id is None:
                return {"status": "skipped", "detail": "此推送批次已有记录"}
            title, message = "", ""
            try:
                preview = await self.preview(kind)
                settings = self.store.settings()
                # A user can pause/change a job while its network requests are running.
                if not slot.startswith("manual:"):
                    schedule = getattr(settings, kind)
                    expected_time = datetime.fromisoformat(slot).strftime("%H:%M")
                    if not settings.scheduler_enabled or not schedule.enabled or schedule.time != expected_time:
                        self.store.delivery(delivery_id, "cancelled", detail="采集期间任务被暂停或时间已修改")
                        return {"status": "cancelled"}
                title = preview["title"]
                message = preview["message"] + f"\n\n批次：{slot}"
                self.store.delivery(delivery_id, "sending", title=title, message=message)
                await send_dingtalk(self.client, settings, title, message)
                self.store.delivery(delivery_id, "success", title=title, message=message)
                return {"status": "success", "detail": "已推送到钉钉"}
            except SendUncertain as exc:
                status, detail = "uncertain", str(exc)
            except (SendFailure, SourceError, ValueError) as exc:
                status, detail = "failed", str(exc)
            except Exception:
                # Avoid logging HTTP request objects, which can contain access tokens.
                logger.error("Delivery failed unexpectedly; kind=%s id=%s", kind, delivery_id)
                status, detail = "failed", "任务异常，请检查服务状态"
            self.store.delivery(delivery_id, status, title=title, message=message, detail=detail)
            return {"status": status, "detail": detail}

    async def tick(self, at=None):
        at = at or now_local()
        self.last_tick = at.isoformat()
        for kind, slot in due_slots(self.store.settings(), at):
            result = await self.deliver(kind, slot)
            if result["status"] in ("failed", "uncertain"):
                self.last_error = result.get("detail", "")

    async def loop(self):
        last_prune_day = None
        while True:
            try:
                await self.tick()
                if last_prune_day != now_local().date():
                    self.store.prune()
                    last_prune_day = now_local().date()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "调度循环异常，请检查数据库、密钥和日志"
                logger.error("Scheduler tick failed")
            await asyncio.sleep(10)
