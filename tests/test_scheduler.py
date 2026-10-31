import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from gold_watch.models import SHANGHAI, Schedule, Settings
from gold_watch.providers import SourceError
from gold_watch.reports import SendUncertain, send_dingtalk
from gold_watch.service import Service, due_slots

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=test-only-placeholder"


def configured():
    return Settings(scheduler_enabled=True, webhook=WEBHOOK)


def test_due_slots_timezone_pause_and_grace():
    settings = configured()
    at = datetime(2026, 10, 30, 8, 1, tzinfo=SHANGHAI)
    assert due_slots(settings, at) == [("international", "2026-10-30T08:00:00+08:00")]
    assert due_slots(settings, at.replace(hour=9)) == []
    assert due_slots(settings, at.replace(hour=9, minute=30))[0][0] == "domestic"
    settings.scheduler_enabled = False
    assert due_slots(settings, at) == []


def test_midnight_catchup():
    settings = configured()
    settings.international.time = "23:55"
    due = due_slots(settings, datetime(2026, 10, 31, 0, 5, tzinfo=SHANGHAI))
    assert due == [("international", "2026-10-30T23:55:00+08:00")]


def test_restart_and_repeated_tick_do_not_duplicate(store, quote):
    async def run():
        store.save_settings(configured())
        providers = AsyncMock()
        providers.fetch.return_value = quote
        sends = []
        def handler(request):
            sends.append(request)
            return httpx.Response(200, json={"errcode": 0})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = Service(store, providers, client)
            at = datetime(2026, 10, 30, 8, 1, tzinfo=SHANGHAI)
            await service.tick(at)
            await service.tick(at)
            store.recover()
            restarted = Service(store, providers, client)
            await restarted.tick(at)
        assert len(sends) == 1
        assert store.history()[0]["status"] == "success"
    asyncio.run(run())


def test_crash_during_sending_marks_uncertain(store):
    record = store.claim("international", "scheduled-slot")
    store.delivery(record, "sending", message="hello")
    store.recover()
    assert store.history()[0]["status"] == "uncertain"
    assert store.claim("international", "scheduled-slot") is None


def test_pause_during_collection_cancels_send(store, quote):
    async def run():
        store.save_settings(configured())
        async def fetch(kind, settings):
            settings.scheduler_enabled = False
            store.save_settings(settings)
            return quote
        providers = AsyncMock()
        providers.fetch.side_effect = fetch
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("must not send"))) as client:
            result = await Service(store, providers, client).deliver("international", "2026-10-30T08:00:00+08:00")
        assert result["status"] == "cancelled"
    asyncio.run(run())


def test_source_failure_uses_cache_without_rewriting_quote_time(store, quote):
    async def run():
        store.save_quote(quote)
        providers = AsyncMock()
        providers.fetch.side_effect = SourceError("source unavailable")
        service = Service(store, providers, None)
        cached = await service.collect("international")
        assert cached.quote_time == quote.quote_time
        assert "历史缓存" in cached.warnings[0]
        settings = store.settings()
        settings.allow_fallback = False
        store.save_settings(settings)
        with pytest.raises(SourceError):
            await service.collect("international")
    asyncio.run(run())


def test_send_timeout_is_not_automatically_retried():
    async def run():
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("timeout", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(SendUncertain):
                await send_dingtalk(client, configured(), "日报", "内容")
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("time", ["8:00", "24:00", "08:60", "08:00:00", "tomorrow"])
def test_invalid_schedule_times(time):
    with pytest.raises(ValueError):
        Schedule(time=time, title="test", template="{price}")
