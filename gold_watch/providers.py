import asyncio
import csv
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from bs4 import BeautifulSoup

from .models import SHANGHAI, Quote, Settings, now_local

GOLD_API = "https://api.gold-api.com/price/XAU"
GOLDAPI_IO = "https://www.goldapi.io/api/price/XAU/USD"
EASTMONEY = "https://push2.eastmoney.com/api/qt/stock/get"
SINA = "https://hq.sinajs.cn/list=SGE_AU9999"
SGE_DAILY = "https://www.sge.com.cn/sjzx/quotation_daily_new"
JUHE = "https://web.juhe.cn/finance/gold/shgold"


class SourceError(Exception):
    """A sanitized source failure safe to show in the admin UI."""


def number(value) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", ""))
        if not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError) as exc:
        raise SourceError("上游价格无效或为占位值") from exc


def parse_gold_api(data: dict, at: datetime) -> Quote:
    if data.get("symbol") != "XAU" or data.get("currency", "USD") != "USD":
        raise SourceError("国际行情品种或币种不匹配")
    return Quote(kind="international", symbol="XAU/USD", name="国际现货黄金",
                 price=number(data["price"]), currency="USD", unit="美元/金衡盎司",
                 quote_time=datetime.fromisoformat(data["updatedAt"].replace("Z", "+00:00")),
                 fetched_at=at, source="Gold API（聚合现货参考）", source_url="https://gold-api.com/",
                 price_type="spot", warnings=["时间为上游提供的更新时间，不保证是最后成交时间。"])


def parse_goldapi_io(data: dict, at: datetime) -> Quote:
    if data.get("metal") != "XAU" or data.get("currency") != "USD":
        raise SourceError("国际行情品种或币种不匹配")
    if data.get("unit", "troy_ounce") != "troy_ounce":
        raise SourceError("国际行情单位不匹配")
    exchange = str(data.get("exchange", "未注明报价源"))[:60]
    return Quote(kind="international", symbol="XAU/USD", name="国际现货黄金",
                 price=number(data["price"]), currency="USD", unit="美元/金衡盎司",
                 quote_time=datetime.fromtimestamp(int(data["timestamp"]), UTC), fetched_at=at,
                 source=f"GoldAPI.io / {exchange}", source_url="https://www.goldapi.io/",
                 price_type="spot", previous_close=number(data["prev_close_price"])
                 if data.get("prev_close_price") else None)


def parse_eastmoney(payload: dict, at: datetime) -> Quote:
    data = payload.get("data") or {}
    if payload.get("rc") != 0 or data.get("f57", "").upper() != "AU9999":
        raise SourceError("国内行情返回异常或合约不匹配")
    decimals = int(data["f59"])
    if decimals not in range(5):
        raise SourceError("国内行情价格精度异常")
    scale = Decimal(10) ** decimals
    quote_time = datetime.fromtimestamp(int(data["f86"]), UTC)
    return Quote(kind="domestic", symbol="Au99.99", name="上金所 Au99.99",
                 price=number(data["f43"]) / scale, currency="CNY", unit="元/克",
                 previous_close=number(data["f60"]) / scale if data.get("f60") not in (None, "-", 0) else None,
                 quote_time=quote_time, fetched_at=at, source="东方财富（非官方延时行情）",
                 source_url="https://quote.eastmoney.com/globalfuture/AU9999.html",
                 price_type="delayed", warnings=["免费公开行情接口，无可用性保证；交易日期未由此接口明确提供。"])


def parse_sina(text: str, at: datetime) -> Quote:
    match = re.search(r'var\s+hq_str_SGE_AU9999\s*=\s*"([^"\r\n]+)"\s*;', text)
    if not match:
        raise SourceError("新浪未返回有效的 Au99.99 行情")
    values = next(csv.reader([match.group(1)]))
    if len(values) < 18 or values[0] != "AU9999" or values[2] != "Au99.99":
        raise SourceError("新浪行情字段或合约不匹配")
    return Quote(kind="domestic", symbol="Au99.99", name="上金所 Au99.99",
                 price=number(values[3]), currency="CNY", unit="元/克",
                 previous_close=number(values[5]),
                 quote_time=datetime.strptime(values[16], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI),
                 fetched_at=at, source="新浪财经（非官方延时行情）",
                 source_url="https://finance.sina.com.cn/futures/quotes/AU9999.shtml",
                 price_type="delayed", warnings=["免费公开行情接口，无可用性保证；交易日期未由此接口明确提供。"])


def parse_juhe(payload: dict, at: datetime) -> Quote:
    if str(payload.get("resultcode")) != "200" or payload.get("error_code", 0) not in (0, None):
        raise SourceError("聚合数据接口拒绝请求，请核对 API Key、额度及服务状态")
    result = payload.get("result", [])
    entries = []
    for item in result if isinstance(result, list) else [result]:
        if isinstance(item, dict):
            entries.extend(item.values() if "variety" not in item else [item])
    row = next((r for r in entries if isinstance(r, dict) and r.get("variety") == "Au99.99"), None)
    if not row:
        raise SourceError("聚合数据未返回 Au99.99")
    return Quote(kind="domestic", symbol="Au99.99", name="上金所 Au99.99",
                 price=number(row["latestpri"]), currency="CNY", unit="元/克",
                 quote_time=datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI),
                 fetched_at=at, source="聚合数据 / 上海黄金交易所行情", source_url="https://www.juhe.cn/docs/api/id/29",
                 price_type="delayed", previous_close=number(row["yespri"]) if row.get("yespri") else None)


def parse_sge_daily(html: str, at: datetime) -> Quote:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for row in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
        if len(cells) < 6 or cells[1] != "Au99.99":
            continue
        try:
            day = datetime.strptime(cells[0], "%Y-%m-%d").date()
            # The day is incomplete before the afternoon session ends.
            if day > at.date() or (day == at.date() and (at.hour, at.minute) < (15, 40)):
                continue
            price = number(cells[5])
            candidates.append((day, price))
        except (ValueError, SourceError):
            continue
    if not candidates:
        raise SourceError("上金所未返回有效的完整交易日收盘价")
    day, price = max(candidates, key=lambda row: row[0])
    previous = sorted((r for r in candidates if r[0] < day), reverse=True)
    return Quote(kind="domestic", symbol="Au99.99", name="上金所 Au99.99 收盘价",
                 price=price, currency="CNY", unit="元/克",
                 quote_time=datetime.combine(day, datetime.min.time(), SHANGHAI).replace(hour=15, minute=30),
                 fetched_at=at, source="上海黄金交易所（官方每日行情）", source_url=SGE_DAILY,
                 price_type="daily_close", trading_date=day.isoformat(),
                 previous_close=previous[0][1] if previous else None,
                 warnings=["时间标记为该交易日常规日盘收市时间，并非网页发布时间。"])


class Providers:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get(self, url, *, headers=None, params=None):
        # Fixed source URLs only; never accept arbitrary admin-provided fetch URLs.
        for attempt in range(3):
            try:
                response = await self.client.get(url, headers=headers, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                if attempt == 2:
                    raise SourceError("行情请求超时或网络不可用") from None
                await asyncio.sleep(0.5 * (2 ** attempt))
            except httpx.HTTPStatusError as exc:
                raise SourceError(f"行情服务返回 HTTP {exc.response.status_code}") from None
        raise SourceError("行情暂不可用")

    async def fetch_one(self, name: str, settings: Settings) -> Quote:
        at = now_local()
        try:
            if name == "gold_api":
                return parse_gold_api((await self.get(GOLD_API)).json(), at)
            if name == "goldapi_io":
                return parse_goldapi_io((await self.get(GOLDAPI_IO, headers={"x-access-token": settings.goldapi_key})).json(), at)
            if name == "eastmoney":
                return parse_eastmoney((await self.get(EASTMONEY, params={
                    "secid": "118.Au9999", "fields": "f43,f57,f58,f59,f60,f86",
                })).json(), at)
            if name == "sina":
                response = await self.get(SINA, headers={"Referer": "https://finance.sina.com.cn/"})
                return parse_sina(response.content.decode("gb18030", errors="replace"), at)
            if name == "juhe":
                return parse_juhe((await self.get(JUHE, params={"key": settings.juhe_key, "v": "1"})).json(), at)
            if name == "sge_daily":
                return parse_sge_daily((await self.get(SGE_DAILY, params={
                    "start_date": (at.date() - timedelta(days=28)).isoformat(), "end_date": at.date().isoformat(),
                })).text, at)
        except SourceError:
            raise
        except (AttributeError, KeyError, ValueError, TypeError, OverflowError, OSError):
            raise SourceError("行情字段异常，请检查上游接口是否改版") from None
        raise SourceError("未知数据源")

    async def fetch(self, kind: str, settings: Settings):
        primary = settings.international_provider if kind == "international" else settings.domestic_provider
        sources = [primary]
        if settings.allow_fallback:
            if kind == "international" and primary != "gold_api":
                sources.append("gold_api")
            if kind == "domestic" and primary != "sge_daily":
                if primary != "sina":
                    sources.append("sina")
                sources.append("sge_daily")
        errors = []
        for source in sources:
            try:
                quote = await self.fetch_one(source, settings)
                if errors:
                    quote.warnings.insert(0, "主数据源不可用，当前使用备用来源；" + "；".join(errors))
                return quote
            except SourceError as exc:
                errors.append(f"{source}：{exc}")
        raise SourceError("；".join(errors))
