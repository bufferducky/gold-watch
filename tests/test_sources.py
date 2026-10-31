import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from gold_watch.models import SHANGHAI, Settings
from gold_watch.providers import (
    EASTMONEY,
    SGE_DAILY,
    SINA,
    Providers,
    SourceError,
    parse_eastmoney,
    parse_gold_api,
    parse_goldapi_io,
    parse_juhe,
    parse_sge_daily,
    parse_sina,
)

AT = datetime(2026, 10, 30, 10, tzinfo=SHANGHAI)


def eastmoney(price=83300):
    return {"rc": 0, "data": {"f57": "AU9999", "f43": price, "f59": 2,
                              "f60": 85476, "f86": int(AT.timestamp())}}


def daily_row(day, price):
    return f"<tr><td>{day}</td><td>Au99.99</td><td>800</td><td>810</td><td>790</td><td>{price}</td></tr>"


def test_domestic_price_scale_and_close():
    quote = parse_eastmoney(eastmoney(), AT)
    assert quote.price == Decimal("833.00")
    assert quote.previous_close == Decimal("854.76")
    assert quote.quote_time == AT


def test_sina_contract_price_and_timestamp():
    text = 'var hq_str_SGE_AU9999="AU9999,黄金99,Au99.99,837.10,846.07,854.76,851.00,855.00,835.00,854.50,836.30,839.69,29.00,2.00,26872.00,22735498000.00,2026-10-30 10:00:00,-2.04%";'
    quote = parse_sina(text, AT)
    assert quote.price == Decimal("837.10")
    assert quote.previous_close == Decimal("854.76")
    assert quote.quote_time == AT
    with pytest.raises(SourceError):
        parse_sina('var hq_str_SGE_AU9999="";', AT)


@pytest.mark.parametrize("bad_price", [0, -1, "-", "NaN", "Infinity", None])
def test_reject_placeholder_and_nonfinite_price(bad_price):
    with pytest.raises(SourceError):
        parse_eastmoney(eastmoney(bad_price), AT)


def test_reject_wrong_contract_and_future_timestamp():
    data = eastmoney()
    data["data"]["f57"] = "AU(T+D)"
    with pytest.raises(SourceError):
        parse_eastmoney(data, AT)
    data = eastmoney()
    data["data"]["f86"] += 3600
    with pytest.raises(ValidationError):
        parse_eastmoney(data, AT)


def test_international_timezone_and_currency():
    data = {"symbol": "XAU", "price": 3900.12, "updatedAt": "2026-10-30T02:00:00Z"}
    quote = parse_gold_api(data, AT)
    assert quote.quote_time == AT
    assert quote.unit == "美元/金衡盎司"
    with pytest.raises(SourceError):
        parse_gold_api({**data, "currency": "CNY"}, AT)


def test_goldapi_bid_is_not_mislabelled_as_last_price():
    quote = parse_goldapi_io({"metal": "XAU", "currency": "USD", "price": "3900.12", "bid": "3899.0",
                             "timestamp": int(AT.timestamp()), "exchange": "FOREXCOM"}, AT)
    assert quote.price == Decimal("3900.12")
    assert "FOREXCOM" in quote.source


def test_juhe_documented_v1_response():
    quote = parse_juhe({"resultcode": "200", "error_code": 0, "result": [{"1": {
        "variety": "Au99.99", "latestpri": "830.2", "yespri": "840.5", "time": "2026-10-30 10:00:00",
    }}]}, AT)
    assert quote.price == Decimal("830.2")
    assert quote.quote_time == AT


def test_sge_excludes_incomplete_current_day_and_placeholders():
    html = "<table>" + daily_row("2026-10-30", "900") + daily_row("2026-10-29", "850.5") + daily_row("2026-10-28", "840") + "</table>"
    quote = parse_sge_daily(html, AT)
    assert quote.trading_date == "2026-10-29"
    assert quote.previous_close == Decimal("840")
    assert "非当前盘中价" in quote.status()
    with pytest.raises(SourceError):
        parse_sge_daily("<table>" + daily_row("2026-10-29", "0.0") + "</table>", AT)


def test_stale_primary_returns_explicit_age():
    quote = parse_gold_api({"symbol": "XAU", "price": 3900, "updatedAt": "2026-10-29T02:00:00Z"}, AT)
    assert "超过 20 分钟" in quote.status(AT)


def test_domestic_fallback_does_not_pretend_to_be_live():
    async def run():
        def handler(request):
            if str(request.url).startswith(EASTMONEY):
                return httpx.Response(200, json=eastmoney(0))
            if str(request.url).startswith(SINA):
                return httpx.Response(200, text='var hq_str_SGE_AU9999="";')
            assert str(request.url).startswith(SGE_DAILY)
            yesterday = (datetime.now(SHANGHAI) - timedelta(days=1)).strftime("%Y-%m-%d")
            return httpx.Response(200, text="<table>" + daily_row(yesterday, "850") + "</table>")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            quote = await Providers(client).fetch("domestic", Settings(domestic_provider="eastmoney"))
        assert quote.price_type == "daily_close"
        assert "备用来源" in quote.warnings[0]
    asyncio.run(run())


def test_provider_error_does_not_expose_api_key():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as client:
            providers = Providers(client)
            with pytest.raises(SourceError) as exc:
                await providers.fetch_one("juhe", Settings(juhe_key="very-secret-api-key"))
            assert "very-secret-api-key" not in str(exc.value)
            assert "HTTP 401" in str(exc.value)
    asyncio.run(run())


def test_disconnected_primary_falls_back_to_official_close(monkeypatch):
    async def run():
        calls = []
        async def no_delay(_):
            return None
        monkeypatch.setattr("gold_watch.providers.asyncio.sleep", no_delay)
        def handler(request):
            if str(request.url).startswith(EASTMONEY):
                calls.append(request)
                raise httpx.RemoteProtocolError("Server disconnected", request=request)
            if str(request.url).startswith(SINA):
                return httpx.Response(200, text='var hq_str_SGE_AU9999="";')
            day = (datetime.now(SHANGHAI) - timedelta(days=1)).strftime("%Y-%m-%d")
            return httpx.Response(200, text="<table>" + daily_row(day, "850") + "</table>")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            quote = await Providers(client).fetch("domestic", Settings(domestic_provider="eastmoney"))
        assert len(calls) == 3
        assert quote.price_type == "daily_close"
        assert "备用来源" in quote.warnings[0]
    asyncio.run(run())
