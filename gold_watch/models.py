from datetime import datetime
from decimal import Decimal
from string import Formatter
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

SHANGHAI = ZoneInfo("Asia/Shanghai")
Kind = Literal["international", "domestic"]
FIELDS = {
    "date", "name", "price", "unit", "quote_time", "source", "source_url",
    "status", "change", "previous", "target", "premium", "note", "title", "trend", "warnings", "conversion",
}

LEGACY_INTERNATIONAL_TEMPLATE = """### 国际金价早报 · {date}
**{name}：{price} {unit}**

- 数据状态：{status}
- 报价时间：{quote_time}
- {change}
- {target}

来源：[{source}]({source_url})

{note}
"""

LEGACY_DOMESTIC_TEMPLATE = """### 国内金价观察 · {date}
**{name}：{price} {unit}**

- 数据状态：{status}
- 报价时间：{quote_time}
- {change}
- {target}
- {premium}

来源：[{source}]({source_url})

{note}
"""

PRE_CONVERSION_INTERNATIONAL_TEMPLATE = """### {title} · {date}
**{price} {unit}**

{name} · {status}

{change}

{target}

{trend}

上游更新：{quote_time}

来源：[{source}]({source_url})

{warnings}

{note}
"""

INTERNATIONAL_TEMPLATE = PRE_CONVERSION_INTERNATIONAL_TEMPLATE.replace(
    "**{price} {unit}**", "**{price} {unit}**\n\n{conversion}",
)
DOMESTIC_TEMPLATE = PRE_CONVERSION_INTERNATIONAL_TEMPLATE.replace("{trend}\n", "{premium}\n\n{trend}\n")


def now_local() -> datetime:
    return datetime.now(SHANGHAI)


class Schedule(BaseModel):
    enabled: bool = True
    time: str
    title: str = Field(min_length=1, max_length=60)
    template: str = Field(min_length=1, max_length=3000)
    target_price: Decimal | None = Field(default=None, gt=0, le=1000000)
    note: str = Field(default="实物金条价格另含溢价及费用。", max_length=300)

    @field_validator("time")
    @classmethod
    def valid_time(cls, value):
        import re
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间格式应为 HH:MM")
        return value

    @field_validator("template")
    @classmethod
    def valid_template(cls, value):
        try:
            for _, name, spec, conversion in Formatter().parse(value):
                if name is not None and (name not in FIELDS or spec or conversion):
                    raise ValueError("模板只允许使用列出的变量，不支持属性或格式表达式")
        except ValueError as exc:
            raise ValueError(f"模板格式错误：{exc}") from exc
        return value


class Settings(BaseModel):
    scheduler_enabled: bool = False
    catchup_minutes: int = Field(default=30, ge=1, le=180)
    international: Schedule = Field(default_factory=lambda: Schedule(
        time="08:00", title="国际金价早报", template=INTERNATIONAL_TEMPLATE,
    ))
    domestic: Schedule = Field(default_factory=lambda: Schedule(
        time="09:30", title="国内金价观察", template=DOMESTIC_TEMPLATE,
    ))
    international_provider: Literal["gold_api", "goldapi_io"] = "gold_api"
    domestic_provider: Literal["sina", "eastmoney", "juhe", "sge_daily"] = "sina"
    allow_fallback: bool = True
    domestic_premium: Decimal = Field(default=Decimal("0"), ge=0, le=10000)
    webhook: str = Field(default="", max_length=500)
    dingtalk_secret: str = Field(default="", max_length=200)
    goldapi_key: str = Field(default="", max_length=300)
    juhe_key: str = Field(default="", max_length=300)

    @field_validator("webhook")
    @classmethod
    def valid_webhook(cls, value):
        from urllib.parse import parse_qs, urlsplit
        if value:
            parts = urlsplit(value)
            if (parts.scheme != "https" or parts.netloc != "oapi.dingtalk.com"
                    or parts.path != "/robot/send" or not parse_qs(parts.query).get("access_token")):
                raise ValueError("请填写钉钉自定义机器人 HTTPS Webhook")
        return value

    @model_validator(mode="after")
    def keys_required(self):
        if self.international_provider == "goldapi_io" and not self.goldapi_key:
            raise ValueError("使用 GoldAPI.io 需要填写 API Key")
        if self.domestic_provider == "juhe" and not self.juhe_key:
            raise ValueError("使用聚合数据需要填写 API Key")
        if self.scheduler_enabled and not self.webhook:
            raise ValueError("启用定时推送前请先配置钉钉 Webhook")
        return self


class ExchangeRate(BaseModel):
    base: Literal["USD"] = "USD"
    currency: Literal["CNY"] = "CNY"
    rate: Decimal = Field(gt=0, le=100)
    updated_at: datetime
    next_update: datetime
    fetched_at: datetime
    source: str = "ExchangeRate-API（日更参考）"
    source_url: str = "https://www.exchangerate-api.com/"
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_times(self):
        if any(value.tzinfo is None for value in (self.updated_at, self.next_update, self.fetched_at)):
            raise ValueError("汇率时间必须包含时区")
        if (self.updated_at - self.fetched_at).total_seconds() > 300:
            raise ValueError("汇率更新时间不能在未来")
        if not 0 < (self.next_update - self.updated_at).total_seconds() <= 2 * 86400:
            raise ValueError("汇率下次更新时间异常")
        return self


class Quote(BaseModel):
    kind: Kind
    symbol: str
    name: str
    price: Decimal = Field(gt=0, le=1000000)
    currency: str
    unit: str
    quote_time: datetime
    fetched_at: datetime
    source: str
    source_url: str
    price_type: Literal["spot", "delayed", "daily_close"]
    previous_close: Decimal | None = Field(default=None, gt=0)
    trading_date: str | None = None
    warnings: list[str] = Field(default_factory=list)
    exchange_rate: ExchangeRate | None = None
    conversion_error: str = ""

    @field_validator("quote_time", "fetched_at")
    @classmethod
    def aware_datetime(cls, value):
        if value.tzinfo is None:
            raise ValueError("报价时间必须包含时区")
        return value

    @model_validator(mode="after")
    def not_future(self):
        if (self.quote_time - self.fetched_at).total_seconds() > 300:
            raise ValueError("上游返回了未来报价时间")
        return self

    def status(self, at: datetime | None = None) -> str:
        at = at or now_local()
        if self.price_type == "daily_close":
            status = f"历史收盘价 · 交易日 {self.trading_date}，非当前盘中价"
        elif (at - self.quote_time).total_seconds() > 20 * 60:
            status = "最近有效报价 · 超过 20 分钟未更新"
        elif at.weekday() >= 5:
            status = "周末参考"
        else:
            status = "现货参考" if self.price_type == "spot" else "延时参考"
        if any("本地历史缓存" in warning for warning in self.warnings):
            status = "本地缓存 · " + status
        elif any("备用来源" in warning for warning in self.warnings):
            status = "备用来源 · " + status
        return status
