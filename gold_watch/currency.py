from datetime import datetime, timedelta
from decimal import Decimal

from .models import SHANGHAI, ExchangeRate, Quote, now_local

GRAMS_PER_TROY_OUNCE = Decimal("31.1034768")
MAX_RATE_AGE = timedelta(days=7)


def usable_rate(rate: ExchangeRate, at: datetime) -> bool:
    return at - MAX_RATE_AGE <= rate.updated_at <= at + timedelta(minutes=5)


def conversion_details(quote: Quote, at: datetime | None = None):
    if quote.kind != "international":
        return None
    at = at or now_local()
    rate = quote.exchange_rate
    if quote.currency != "USD" or quote.unit != "美元/金衡盎司":
        return {"available": False, "price": None, "text": "人民币换算暂不可用：国际报价币种或单位不匹配。"}
    if rate is None or not usable_rate(rate, at):
        reason = quote.conversion_error or "暂无有效美元兑人民币汇率"
        return {"available": False, "price": None, "text": f"人民币换算暂不可用：{reason}。"}
    price = quote.price * rate.rate / GRAMS_PER_TROY_OUNCE
    local_time = rate.updated_at.astimezone(SHANGHAI)
    time_format = "%Y-%m-%d %H:%M" if local_time.year != at.year else "%m-%d %H:%M"
    rate_time = local_time.strftime(time_format) + "（北京时间）"
    notes = list(rate.warnings)
    if at - rate.updated_at > timedelta(hours=48):
        notes.append("历史参考汇率，距今已超过48小时。")
    # A cached gold quote can have a very different timestamp from the FX snapshot.
    if abs((quote.quote_time - rate.updated_at).total_seconds()) > 48 * 3600:
        notes.append("金价与汇率时间相差超过48小时。")
    text = (f"**折合人民币约 {price:.2f} 元/克（国际金价换算参考）**\n\n"
            f"汇率：1 美元 ≈ {rate.rate:f} 元人民币；更新于 {rate_time}\n\n"
            f"汇率来源：[{rate.source}]({rate.source_url})")
    if notes:
        text += "\n\n" + "；".join(dict.fromkeys(notes))
    return {"available": True, "price": f"{price:.2f}", "unit": "元/克", "rate": str(rate.rate),
            "updated_at": rate.updated_at.isoformat(), "source": rate.source, "source_url": rate.source_url,
            "notes": notes, "text": text}
