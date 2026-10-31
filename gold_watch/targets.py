from datetime import datetime

from .currency import cny_per_gram
from .models import Quote, Schedule


def target_details(quote: Quote, schedule: Schedule, at: datetime | None = None):
    if schedule.target_price is None:
        return None
    target = schedule.target_price
    converted = quote.kind == "international" and schedule.target_unit == "cny_per_gram"
    price = cny_per_gram(quote, at) if converted else quote.price
    unit = "元/克" if converted else quote.unit
    prefix = f"观察目标：{target:.2f} {unit}"
    if converted:
        prefix += "（国际金价人民币换算）"
    if price is None:
        return {"text": prefix + "；汇率不可用，暂无法判断是否达到目标", "unit": unit,
                "price": None, "target_price": str(target), "gap": None, "reached": None}
    # Compare unrounded Decimal amounts so display rounding never triggers a false target hit.
    gap = price - target
    if gap > 0:
        result = f"距观察目标还差 {gap:.2f} {unit}，约 {gap / target * 100:.2f}%（以目标价为基准）"
    elif gap == 0:
        result = "当前参考价已达到目标"
    else:
        result = (f"当前参考价已低于目标 {-gap:.2f} {unit}，"
                  f"约 {-gap / target * 100:.2f}%（以目标价为基准）")
    if gap != 0 and abs(gap) < 0.005:
        result = "当前参考价略高于目标（差距不足0.01 " + unit + "）" if gap > 0 else (
            "当前参考价已低于目标（差距不足0.01 " + unit + "）")
    return {"text": prefix + "；" + result, "unit": unit, "price": str(price),
            "target_price": str(target), "gap": str(gap), "reached": gap <= 0}
