import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from gold_watch.currency import GRAMS_PER_TROY_OUNCE, conversion_details
from gold_watch.models import (
    INTERNATIONAL_TEMPLATE,
    PRE_CONVERSION_INTERNATIONAL_TEMPLATE,
    SHANGHAI,
    ExchangeRate,
    Settings,
)
from gold_watch.providers import GOLD_API, USD_CNY, Providers, SourceError, parse_usd_cny
from gold_watch.reports import render
from gold_watch.service import Service

AT = datetime(2026, 10, 31, 23, 30, tzinfo=SHANGHAI)


def fx(rate="7", age=timedelta(hours=12)):
    updated_at = AT - age
    return ExchangeRate(rate=Decimal(rate), updated_at=updated_at,
                        next_update=updated_at + timedelta(days=1), fetched_at=AT)


def test_conversion_uses_troy_ounce_and_preserves_currency_provenance(quote, monkeypatch):
    monkeypatch.setattr("gold_watch.reports.now_local", lambda: AT)
    q = quote.model_copy(update={"price": GRAMS_PER_TROY_OUNCE, "exchange_rate": fx()})
    data = conversion_details(q, AT)
    assert data["price"] == "7.00"
    assert data["rate"] == "7"
    assert "折合人民币约 7.00 元/克（国际金价换算参考）" in data["text"]
    assert "1 美元 ≈ 7 元人民币" in data["text"]
    assert "更新于 10-31 11:30（北京时间）" in data["text"]
    assert "https://www.exchangerate-api.com/" in data["text"]
    settings = Settings()
    message = render(q, settings.international, settings)
    assert message.index("折合人民币") < message.index("国际现货黄金")
    settings.international.template = "金价：{price} {unit}"
    assert "折合人民币约 7.00 元/克" in render(q, settings.international, settings)


def test_unavailable_and_stale_rates_are_never_presented_as_current(quote):
    assert conversion_details(quote, AT)["available"] is False
    q = quote.model_copy(update={"exchange_rate": fx(age=timedelta(days=8))})
    assert conversion_details(q, AT)["price"] is None
    q.exchange_rate = fx(age=timedelta(days=3))
    assert "历史参考汇率" in conversion_details(q, AT)["text"]
    q.kind = "domestic"
    assert conversion_details(q, AT) is None


@pytest.mark.parametrize("update", [
    {"base_code": "CNY"}, {"result": "error"}, {"rates": {}},
    {"rates": {"CNY": 0}}, {"rates": {"CNY": "NaN"}},
    {"time_last_update_unix": int((AT + timedelta(hours=1)).timestamp())},
    {"time_next_update_unix": int((AT + timedelta(days=10)).timestamp())},
])
def test_invalid_exchange_rate_payload_rejected(update):
    payload = {"result": "success", "base_code": "USD", "rates": {"CNY": 7},
               "time_last_update_unix": int((AT - timedelta(hours=1)).timestamp()),
               "time_next_update_unix": int((AT + timedelta(hours=23)).timestamp()), **update}
    with pytest.raises(SourceError):
        parse_usd_cny(payload, AT)


def test_valid_exchange_rate_payload_retains_upstream_time():
    rate = parse_usd_cny({"result": "success", "base_code": "USD", "rates": {"CNY": "6.669566"},
                          "time_last_update_unix": int((AT - timedelta(hours=1)).timestamp()),
                          "time_next_update_unix": int((AT + timedelta(hours=23)).timestamp())}, AT)
    assert rate.rate == Decimal("6.669566")
    assert rate.updated_at == AT - timedelta(hours=1)


@pytest.mark.parametrize(("allow_fallback", "age", "available"), [
    (True, timedelta(days=3), True), (False, timedelta(days=3), False),
    (True, timedelta(days=8), False),
])
def test_rate_failure_respects_fallback_and_cache_age(store, quote, monkeypatch, allow_fallback, age, available):
    monkeypatch.setattr("gold_watch.service.now_local", lambda: AT)
    store.save_exchange_rate(fx(age=age))
    provider = AsyncMock()
    provider.fetch_usd_cny.side_effect = SourceError("网络异常")
    q = quote.model_copy(deep=True)
    asyncio.run(Service(store, provider, None).add_exchange_rate(q, allow_fallback))
    result = conversion_details(q, AT)
    assert result["available"] is available
    if available:
        assert "本地缓存" in result["text"] and "历史参考汇率" in result["text"]
    else:
        assert "换算暂不可用" in result["text"]


def test_fx_failure_does_not_discard_successful_gold_quote(store, quote):
    provider = AsyncMock()
    provider.fetch.return_value = quote
    provider.fetch_usd_cny.side_effect = SourceError("网络异常")
    result = asyncio.run(Service(store, provider, None).preview("international"))
    assert result["quote"]["price"] == str(quote.price)
    assert "人民币换算暂不可用" in result["message"]
    assert result["conversion"]["price"] is None


def test_live_pipeline_sends_both_units_and_reuses_daily_rate(store, monkeypatch):
    monkeypatch.setattr("gold_watch.service.now_local", lambda: AT)
    monkeypatch.setattr("gold_watch.providers.now_local", lambda: AT)
    monkeypatch.setattr("gold_watch.currency.now_local", lambda: AT)
    monkeypatch.setattr("gold_watch.reports.now_local", lambda: AT)
    store.save_settings(Settings(webhook="https://oapi.dingtalk.com/robot/send?access_token=test"))
    calls, messages = [], []

    def handler(request):
        url = str(request.url).split("?")[0]
        calls.append(url)
        if url == GOLD_API:
            return httpx.Response(200, json={"symbol": "XAU", "price": "3876.40", "updatedAt": AT.isoformat()})
        if url == USD_CNY:
            return httpx.Response(200, json={"result": "success", "base_code": "USD", "rates": {"CNY": "6.669566"},
                                           "time_last_update_unix": int((AT - timedelta(hours=12)).timestamp()),
                                           "time_next_update_unix": int((AT + timedelta(hours=12)).timestamp())})
        assert request.method == "POST" and request.url.host == "oapi.dingtalk.com"
        messages.append(json.loads(request.content)["markdown"]["text"])
        return httpx.Response(200, json={"errcode": 0})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = Service(store, Providers(client), client)
            await service.preview("international")
            assert (await service.deliver("international"))["status"] == "success"

    asyncio.run(run())
    assert "3876.40 美元/金衡盎司" in messages[0]
    assert "折合人民币约 831.22 元/克" in messages[0]
    assert calls.count(USD_CNY) == 1
    assert store.recent_quotes("international", 1)[0].exchange_rate.rate == Decimal("6.669566")


def test_previous_default_template_is_upgraded_to_show_conversion(store):
    settings = Settings()
    settings.international.template = PRE_CONVERSION_INTERNATIONAL_TEMPLATE
    store.save_settings(settings)
    assert store.settings().international.template == INTERNATIONAL_TEMPLATE
