import hashlib
import hmac
from base64 import b64encode
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .models import SHANGHAI, Quote, Schedule, Settings, now_local


def render(quote: Quote, schedule: Schedule, settings: Settings, previous: Quote | None = None):
    change = "暂无同口径对比数据"
    if quote.previous_close:
        delta = quote.price - quote.previous_close
        change = f"较来源昨收：{delta:+.2f}（{delta / quote.previous_close * 100:+.2f}%），基准 {quote.previous_close:.2f}"
    elif previous:
        delta = quote.price - previous.price
        change = (f"较上次同来源记录：{delta:+.2f}（{delta / previous.price * 100:+.2f}%）；"
                  f"基准时间 {previous.quote_time.astimezone(SHANGHAI):%m-%d %H:%M}")
    if schedule.target_price:
        target = f"观察目标：{schedule.target_price:.2f} {quote.unit} · " + (
            "当前参考价已达到或低于目标" if quote.price <= schedule.target_price else "当前参考价高于目标")
    else:
        target = "未设置观察目标"
    premium = "未设置金条销售加价，不估算实物买入成本"
    if quote.kind == "domestic" and settings.domestic_premium > 0:
        premium = (f"按手动加价 {settings.domestic_premium:.2f} 元/克估算："
                   f"{quote.price + settings.domestic_premium:.2f} 元/克（非商家报价）")
    status = quote.status()
    if quote.warnings:
        status += "；" + "；".join(quote.warnings)
    fields = {
        "date": now_local().strftime("%Y-%m-%d"), "name": quote.name,
        "price": f"{quote.price:.2f}", "unit": quote.unit,
        "quote_time": quote.quote_time.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S +08:00"),
        "source": quote.source, "source_url": quote.source_url, "status": status,
        "change": change, "previous": f"{quote.previous_close:.2f}" if quote.previous_close else "暂无",
        "target": target, "premium": premium, "note": schedule.note,
    }
    message = schedule.template.format_map(fields)
    # Keep provenance even when a user removes its placeholders from the custom template.
    required = f"\n\n---\n报价：{fields['quote_time']} · {quote.unit}\n\n来源：[{quote.source}]({quote.source_url})\n\n状态：{status}"
    if any("{" + field + "}" not in schedule.template for field in ("quote_time", "source", "status", "unit")):
        message += required
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
