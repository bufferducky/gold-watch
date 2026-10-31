import hashlib
import hmac
import re
from base64 import b64encode
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .currency import conversion_details
from .models import SHANGHAI, Quote, Schedule, Settings, now_local
from .targets import target_details


def report_title(kind: str, schedule: Schedule, *, manual=True):
    if kind == "international" and schedule.title == "国际金价早报":
        if manual or int(schedule.time[:2]) >= 12:
            return "国际金价观察"
    return schedule.title


def price_change(price, baseline, unit):
    delta = price - baseline
    if delta == 0:
        return "持平（0.00%）"
    return f"{delta:+.2f} {unit}（{delta / baseline * 100:+.2f}%）"


def render(quote: Quote, schedule: Schedule, settings: Settings, previous: Quote | None = None,
           *, manual=True, trend=""):
    at = now_local()
    conversion = conversion_details(quote, at)
    change = "暂无同口径对比基准"
    if quote.previous_close:
        change = (f"较来源上一交易日收盘：{price_change(quote.price, quote.previous_close, quote.unit)}"
                  f"；基准 {quote.previous_close:.2f}")
    elif previous:
        change = (f"较上一份同来源成功推送：{price_change(quote.price, previous.price, quote.unit)}"
                  f"；基准 {previous.quote_time.astimezone(SHANGHAI):%m-%d %H:%M}")
    target = target_details(quote, schedule, at)
    premium = ""
    if quote.kind == "domestic" and settings.domestic_premium > 0:
        premium = (f"按手动加价 {settings.domestic_premium:.2f} 元/克估算："
                   f"{quote.price + settings.domestic_premium:.2f} 元/克（非商家报价）")
    status = quote.status(at)
    short_warnings = {
        "时间为上游提供的更新时间，不保证是最后成交时间。": "上游更新时间不代表成交时间。",
        "免费公开行情接口，无可用性保证；交易日期未由此接口明确提供。": "公开延时行情，交易日未由来源提供。",
        "时间标记为该交易日常规日盘收市时间，并非网页发布时间。": "时间为常规日盘收市时间。",
    }
    warnings = "\n".join(dict.fromkeys(short_warnings.get(warning, warning) for warning in quote.warnings))
    local_time = quote.quote_time.astimezone(SHANGHAI)
    time_format = "%Y-%m-%d %H:%M" if local_time.year != at.year else "%m-%d %H:%M"
    fields = {
        "date": at.strftime("%m月%d日") + " 周" + "一二三四五六日"[at.weekday()], "name": quote.name,
        "price": f"{quote.price:.2f}", "unit": quote.unit,
        "quote_time": local_time.strftime(time_format) + "（北京时间）",
        "source": quote.source, "source_url": quote.source_url, "status": status,
        "change": change, "previous": f"{quote.previous_close:.2f}" if quote.previous_close else "暂无",
        "target": target["text"] if target else "", "premium": premium, "note": schedule.note,
        "title": report_title(quote.kind, schedule, manual=manual), "trend": trend, "warnings": warnings,
        "conversion": conversion["text"] if conversion else "",
    }
    lines = []
    for line in schedule.template.splitlines():
        if any(not fields[name] and ("{" + name + "}") in line
               and not line.replace("{" + name + "}", "").strip(" -*•")
               for name in ("target", "premium", "warnings", "trend", "conversion")):
            continue
        lines.append(line)
    message = "\n".join(lines).format_map(fields)
    # Keep provenance even when a user removes its placeholders from the custom template.
    required = {"quote_time": f"报价：{fields['quote_time']}", "unit": f"单位：{quote.unit}",
                "source": f"来源：[{quote.source}]({quote.source_url})", "status": f"状态：{status}",
                "warnings": warnings, "conversion": fields["conversion"]}
    missing = [text for field, text in required.items() if text and "{" + field + "}" not in schedule.template]
    if missing:
        message += "\n\n" + "\n\n".join(missing)
    message = re.sub(r"\n{3,}", "\n\n", message).strip()
    if len(message.encode("utf-8")) > 15000:
        raise ValueError("模板渲染后过长，请缩短模板或备注")
    return message


def signed_url(webhook: str, secret: str, timestamp: str):
    parts = urlsplit(webhook)
    params = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("timestamp", "sign")]
    if secret:
        signature = b64encode(hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()).decode()
        params.extend([("timestamp", timestamp), ("sign", signature)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))


class SendFailure(Exception):
    pass


class SendUncertain(Exception):
    pass


async def send_dingtalk(client: httpx.AsyncClient, settings: Settings, title: str, message: str):
    if not settings.webhook:
        raise SendFailure("尚未配置钉钉 Webhook")
    timestamp = str(int(now_local().timestamp() * 1000))
    url = signed_url(settings.webhook, settings.dingtalk_secret, timestamp)
    try:
        response = await client.post(url, json={
            "msgtype": "markdown", "markdown": {"title": title, "text": message},
            "at": {"isAtAll": True},
        })
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise SendFailure("连接钉钉失败，请检查网络后手动重发") from None
    except httpx.HTTPError:
        raise SendUncertain("请求中断，钉钉可能已收到消息；请先检查群消息，不自动重发") from None
    if response.status_code >= 500:
        raise SendUncertain(f"钉钉返回 HTTP {response.status_code}，结果不确定，请核对群消息")
    try:
        data = response.json()
    except ValueError:
        raise SendUncertain("钉钉响应无法解析，发送结果不确定") from None
    if response.status_code != 200 or data.get("errcode") != 0:
        # Do not echo the upstream response: it could contain a token or signed URL.
        raise SendFailure("钉钉拒绝请求，请核对签名、关键词、出口 IP 白名单和频率限制")
