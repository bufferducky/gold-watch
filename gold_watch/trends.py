from datetime import datetime, timedelta

from .models import SHANGHAI, Quote, now_local


def same_series(left: Quote, right: Quote) -> bool:
    return all(getattr(left, key) == getattr(right, key) for key in (
        "kind", "source", "symbol", "currency", "unit", "price_type",
    ))


def weekly_trend(quotes: list[Quote], reference: Quote | None, at: datetime | None = None):
    at = (at or now_local()).astimezone(SHANGHAI)
    start = at.date() - timedelta(days=6)
    daily = {}
    for quote in quotes:
        day = quote.quote_time.astimezone(SHANGHAI).date()
        if (reference is None or not same_series(quote, reference)
                or not start <= day <= at.date() or quote.quote_time > at):
            continue
        if day not in daily or quote.quote_time > daily[day].quote_time:
            daily[day] = quote
    points = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        quote = daily.get(day)
        points.append({"date": day.isoformat(), "price": str(quote.price) if quote else None,
                       "quote_time": quote.quote_time.isoformat() if quote else None})
    samples = [daily[day] for day in sorted(daily)]
    count = len(samples)
    summary = f"近7日：数据积累中（{count}/7天有记录）"
    if count >= 2:
        first, last = samples[0], samples[-1]
        delta = last.price - first.price
        span = (f"{first.quote_time.astimezone(SHANGHAI):%m-%d} → "
                f"{last.quote_time.astimezone(SHANGHAI):%m-%d}")
        summary = (f"近7日采样：{delta:+.2f} {reference.unit}（{delta / first.price * 100:+.2f}%）"
                   f"；{span}，{count}/7天有记录")
    return {"points": points, "days_with_data": count, "summary": summary,
            "source": reference.source if reference else None,
            "unit": reference.unit if reference else None,
            "low": str(min(q.price for q in samples)) if samples else None,
            "high": str(max(q.price for q in samples)) if samples else None,
            "note": "每天取最后一条采集报价，缺失日期留空；采样变化不等于完整市场涨跌。"}
