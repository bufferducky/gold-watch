import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from gold_watch.app import create_app
from gold_watch.currency import GRAMS_PER_TROY_OUNCE
from gold_watch.models import ExchangeRate, Settings
from gold_watch.reports import render
from gold_watch.targets import target_details


def with_rate(quote, rate="1"):
    at = quote.fetched_at
    return quote.model_copy(update={"exchange_rate": ExchangeRate(
        rate=Decimal(rate), updated_at=at, next_update=at + timedelta(days=1), fetched_at=at,
    )})


@pytest.mark.parametrize(("price", "reached", "expected"), [
    ("831.22", False, "距观察目标还差 31.22 元/克，约 3.90%"),
    ("800", True, "当前参考价已达到目标"),
    ("799", True, "当前参考价已低于目标 1.00 元/克，约 0.12%"),
])
def test_yuan_target_compares_converted_gold_without_premium(quote, price, reached, expected):
    q = with_rate(quote)
    q.price = Decimal(price) * GRAMS_PER_TROY_OUNCE
    settings = Settings(domestic_premium="99")
    schedule = settings.international
    schedule.target_price = Decimal("800")
    schedule.target_unit = "cny_per_gram"
    result = target_details(q, schedule)
    assert result["reached"] is reached
    assert "观察目标：800.00 元/克（国际金价人民币换算）" in result["text"]
    assert expected in result["text"]
    assert result["text"] in render(q, schedule, settings)
    assert "商家" not in result["text"] and "美元" not in result["text"]


def test_exchange_rate_changes_target_comparison(quote):
    settings = Settings()
    settings.international.target_price = Decimal("800")
    settings.international.target_unit = "cny_per_gram"
    q = with_rate(quote, "6")
    q.price = Decimal("4000")
    assert target_details(q, settings.international)["reached"] is True
    q.exchange_rate.rate = Decimal("7")
    assert target_details(q, settings.international)["reached"] is False


def test_missing_or_expired_rate_never_compares_usd_against_yuan(quote):
    settings = Settings()
    settings.international.target_price = Decimal("800")
    settings.international.target_unit = "cny_per_gram"
    for q in [quote, with_rate(quote)]:
        if q.exchange_rate:
            q.exchange_rate.updated_at -= timedelta(days=8)
        result = target_details(q, settings.international)
        assert result["reached"] is None and result["gap"] is None
        assert "汇率不可用，暂无法判断" in result["text"]
        assert "距观察目标还差" not in result["text"]


def test_rounding_does_not_falsely_reach_target(quote):
    q = with_rate(quote)
    q.price = Decimal("800.004") * GRAMS_PER_TROY_OUNCE
    schedule = Settings().international
    schedule.target_price = Decimal("800")
    schedule.target_unit = "cny_per_gram"
    result = target_details(q, schedule)
    assert result["reached"] is False
    assert "略高于目标" in result["text"]
    assert "差距不足0.01 元/克" in result["text"]


def test_legacy_targets_retain_original_units_and_domestic_ignores_premium(store, quote):
    old = Settings().model_dump(mode="json")
    for kind in ("international", "domestic"):
        old[kind].pop("target_unit")
        old[kind]["target_price"] = "800"
    with store.db() as db:
        stored = json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])
        stored.update({kind: old[kind] for kind in ("international", "domestic")})
        db.execute("UPDATE settings SET body=? WHERE id=1", (json.dumps(stored),))
    settings = store.settings()
    assert settings.international.target_unit == "original"
    assert "800.00 美元/金衡盎司" in target_details(quote, settings.international)["text"]
    q = quote.model_copy(update={"kind": "domestic", "price": Decimal("810"), "unit": "元/克"})
    settings.domestic_premium = Decimal("99")
    assert "还差 10.00 元/克" in target_details(q, settings.domestic)["text"]


def test_target_unit_api_persists_and_state_matches_preview(store, quote, monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-admin-password")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    q = with_rate(quote)
    q.price = Decimal("831.22") * GRAMS_PER_TROY_OUNCE
    store.save_quote(q)
    store.save_exchange_rate(q.exchange_rate)
    with TestClient(create_app(store=store, run_scheduler=False)) as client:
        client.auth = ("admin", "test-only-admin-password")
        schedule = store.settings().international.model_dump(mode="json")
        schedule.update(target_price="800", target_unit="cny_per_gram")
        response = client.put("/api/settings", json={"international": schedule}, headers={"X-Gold-Watch": "1"})
        assert response.status_code == 200
        assert store.settings().international.target_unit == "cny_per_gram"
        state = client.get("/api/state").json()
        app_service = client.app.state.service
        app_service.providers.fetch = AsyncMock(return_value=q)
        preview = client.post("/api/preview/international", headers={"X-Gold-Watch": "1"}).json()
        assert state["latest"]["international"]["target"] == preview["target"]
        assert preview["target"]["text"] in preview["message"]
        assert "还差 31.22 元/克" in preview["message"]
        schedule["target_unit"] = "invalid_unit"
        rejected = client.put("/api/settings", json={"international": schedule}, headers={"X-Gold-Watch": "1"})
        assert rejected.status_code == 422
        assert store.settings().international.target_unit == "cny_per_gram"
