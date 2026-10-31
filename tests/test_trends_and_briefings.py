import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gold_watch.app import create_app
from gold_watch.models import (
    INTERNATIONAL_TEMPLATE,
    LEGACY_INTERNATIONAL_TEMPLATE,
    SHANGHAI,
    Settings,
)
from gold_watch.providers import SourceError
from gold_watch.reports import render, report_title
from gold_watch.service import Service
from gold_watch.store import Store

AT = datetime(2026, 10, 31, 22, 54, tzinfo=SHANGHAI)


def sample(quote, time=AT, price="100", **updates):
    return quote.model_copy(update={"quote_time": time, "fetched_at": time,
                                    "price": Decimal(price), **updates})


@pytest.mark.parametrize(("price", "expected"), [
    ("110", "距观察目标还差 10.00 美元/金衡盎司，约 10.00%"),
    ("100", "当前参考价已达到目标"),
    ("90", "当前参考价已低于目标 10.00 美元/金衡盎司，约 10.00%"),
])
def test_target_gap_and_concise_manual_report(quote, monkeypatch, price, expected):
    monkeypatch.setattr("gold_watch.reports.now_local", lambda: AT)
    settings = Settings()
    settings.international.target_price = Decimal("100")
    q = sample(quote, price=price, warnings=["时间为上游提供的更新时间，不保证是最后成交时间。"])
    message = render(q, settings.international, settings)
    assert expected in message
    assert "### 国际金价观察 · 10月31日 周六" in message
    assert "周末参考" in message
    assert "上游更新：10-31 22:54（北京时间）" in message
    assert "上游更新时间不代表成交时间。" in message
    assert "+08:00" not in message


def test_empty_fields_hidden_and_previous_close_preferred(quote):
    settings = Settings()
    previous = sample(quote, AT - timedelta(days=1), "80")
    message = render(sample(quote, price="110", previous_close=Decimal("100")),
                     settings.international, settings, previous)
    assert "较来源上一交易日收盘：+10.00 美元/金衡盎司（+10.00%）" in message
    assert "成功推送" not in message
    assert "未设置" not in message
    assert "观察目标" not in message
    assert "\n\n\n" not in message
    assert report_title("international", settings.international, manual=False) == "国际金价早报"
    settings.international.time = "22:00"
    assert report_title("international", settings.international, manual=False) == "国际金价观察"
    settings.international.title = "我的金价"
    assert report_title("international", settings.international) == "我的金价"


def test_cached_historical_quote_and_warning_remain_explicit_in_custom_template(quote):
    settings = Settings()
    settings.international.template = "金价 {price}"
    q = sample(quote, datetime(2025, 12, 31, 15, 30, tzinfo=SHANGHAI), price_type="daily_close",
               trading_date="2025-12-31", warnings=["本次采集失败，使用本地历史缓存：请求失败"])
    message = render(q, settings.international, settings)
    assert "本地缓存 · 历史收盘价" in message
    assert "2025-12-31 15:30（北京时间）" in message
    assert "本次采集失败，使用本地历史缓存：请求失败" in message
    assert "来源：" in message and "单位：" in message


def test_successful_delivery_baseline_ignores_previews_and_failed_sends(store, quote):
    async def run():
        settings = Settings(webhook="https://oapi.dingtalk.com/robot/send?access_token=test")
        store.save_settings(settings)
        providers = AsyncMock()
        providers.fetch_usd_cny.side_effect = SourceError("测试未提供汇率")
        providers.fetch.side_effect = [sample(quote, AT - timedelta(days=1), "100"),
                                       sample(quote, AT - timedelta(hours=2), "120"),
                                       sample(quote, AT - timedelta(hours=1), "130"), sample(quote, price="150")]
        payloads = []

        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"errcode": 1 if len(payloads) == 2 else 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = Service(store, providers, client)
            assert (await service.deliver("international"))["status"] == "success"
            await service.preview("international")
            assert (await service.deliver("international"))["status"] == "failed"
            assert (await service.deliver("international"))["status"] == "success"
        text = payloads[-1]["markdown"]["text"]
        assert "较上一份同来源成功推送：+50.00 美元/金衡盎司（+50.00%）" in text
        assert "批次" not in text and "manual:" not in text
        assert payloads[-1]["markdown"]["title"] == "国际金价观察"
        assert "近7日" in text
    asyncio.run(run())


def test_baseline_never_crosses_source_or_uses_a_future_quote(store, quote):
    current = sample(quote)
    for index, saved in enumerate([sample(quote, source="Other"), sample(quote, AT + timedelta(minutes=1))]):
        delivery_id = store.claim("international", str(index))
        store.delivery(delivery_id, "success", quote=saved)
    assert store.previous_quote(current) is None


def test_weekly_trend_uses_daily_last_quote_and_gaps_without_crossing_series(store, quote):
    first = AT.replace(day=25, hour=0, minute=0)
    # Insertion order is deliberately different from chronological order.
    for q in [sample(quote, first.replace(hour=22), "100"),
              sample(quote, first.replace(hour=10), "90"),
              sample(quote, AT.replace(day=28), "120"),
              sample(quote, AT, "110"),
              sample(quote, AT.replace(day=26), "900", source="Other"),
              sample(quote, AT.replace(day=27), "900", price_type="daily_close"),
              sample(quote, first - timedelta(seconds=1), "800"),
              sample(quote, AT + timedelta(minutes=1), "800")]:
        store.save_quote(q)
    result = store.trend("international", sample(quote), AT)
    assert [p["price"] for p in result["points"]] == ["100", None, None, "120", None, None, "110"]
    assert result["low"] == "100" and result["high"] == "120"
    assert result["days_with_data"] == 3
    assert "+10.00%" in result["summary"] and "3/7天有记录" in result["summary"]
    assert "10-25 → 10-31" in result["summary"]


def test_trend_window_is_beijing_calendar_days_and_not_limited_to_120_rows(store, quote):
    first = AT.replace(day=25, hour=0, minute=0)
    store.save_quote(sample(quote, first.astimezone(UTC), "100"))
    for index in range(150):
        store.save_quote(sample(quote, AT - timedelta(minutes=index), "110"))
    result = store.trend("international", sample(quote), AT)
    assert result["days_with_data"] == 2
    assert result["points"][0]["price"] == "100"
    assert result["points"][-1]["price"] == "110"


def test_sparse_trend_does_not_claim_full_week_performance(store, quote):
    assert "0/7天" in store.trend("international", sample(quote), AT)["summary"]
    store.save_quote(sample(quote))
    result = store.trend("international", sample(quote), AT)
    assert "数据积累中（1/7天有记录）" in result["summary"]
    assert "%" not in result["summary"]


def test_existing_database_and_default_template_upgrade_without_overwriting_customizations(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE deliveries (id INTEGER PRIMARY KEY, kind TEXT, slot TEXT, status TEXT, "
                   "created_at TEXT, updated_at TEXT, title TEXT DEFAULT '', message TEXT DEFAULT '', "
                   "detail TEXT DEFAULT '', UNIQUE(kind,slot))")
        db.execute("INSERT INTO deliveries(kind,slot,status,created_at,updated_at) VALUES "
                   "('international','old-slot','success','2026-10-30','2026-10-30')")
    store = Store(path, Fernet.generate_key().decode())
    settings = Settings()
    settings.international.template = LEGACY_INTERNATIONAL_TEMPLATE
    settings.domestic.template = "我的自定义金价 {price}"
    settings.domestic.note = "自定义备注"
    store.save_settings(settings)
    assert store.settings().international.template == INTERNATIONAL_TEMPLATE
    assert store.settings().domestic.template == "我的自定义金价 {price}"
    assert store.settings().domestic.note == "自定义备注"
    assert store.history()[0]["slot"] == "old-slot"
    assert store.history()[0]["quote"] == ""


def test_trend_data_requires_login_and_state_exposes_history(store, quote, monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-admin-password")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store.save_quote(quote)
    with TestClient(create_app(store=store, run_scheduler=False)) as client:
        assert client.get("/api/state").status_code == 401
        data = client.get("/api/state", auth=("admin", "test-only-admin-password")).json()
        assert data["trends"]["international"]["days_with_data"] == 1
        assert data["trends"]["domestic"]["days_with_data"] == 0
