import asyncio
import base64
import hashlib
import hmac
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from gold_watch.app import create_app
from gold_watch.models import Schedule, Settings
from gold_watch.reports import SendFailure, render, send_dingtalk, signed_url

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=test-secret-token"


def test_config_secrets_encrypted_and_not_echoed(store):
    store.save_settings(Settings(webhook=WEBHOOK, dingtalk_secret="SEC-test-secret"))
    with store.db() as db:
        raw = db.execute("SELECT body FROM settings").fetchone()[0]
    assert "test-secret-token" not in raw
    assert "SEC-test-secret" not in raw
    assert store.settings().webhook == WEBHOOK
    assert store.public_settings()["webhook"] == ""
    assert store.public_settings()["webhook_configured"] is True


@pytest.mark.parametrize("template", ["{price.real}", "{price.__class__}", "{price[0]}", "{price!r}", "{price:>100000000}", "{unknown}"])
def test_template_rejects_expressions(template):
    with pytest.raises(ValueError):
        Schedule(time="08:00", title="test", template=template)


def test_template_always_retains_provenance(quote):
    settings = Settings()
    schedule = Schedule(time="08:00", title="test", template="金价：{price}")
    message = render(quote, schedule, settings)
    assert quote.source in message
    assert "报价：" in message
    assert "美元/金衡盎司" in message
    assert "状态：" in message


def test_no_cross_source_change(store, quote):
    store.save_quote(quote.model_copy(update={"quote_time": quote.quote_time - timedelta(days=1), "source": "Other provider"}))
    assert store.previous_quote(quote) is None


def test_signature_matches_official_algorithm():
    url = signed_url(WEBHOOK + "&sign=old&timestamp=old", "SEC-test", "123456")
    query = parse_qs(urlsplit(url).query)
    expected = base64.b64encode(hmac.new(b"SEC-test", b"123456\nSEC-test", hashlib.sha256).digest()).decode()
    assert query["sign"] == [expected]
    assert query["timestamp"] == ["123456"]


def test_http200_with_dingtalk_error_is_failure():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"errcode": 310000}))) as client:
            with pytest.raises(SendFailure):
                await send_dingtalk(client, Settings(webhook=WEBHOOK), "test", "test")
    asyncio.run(run())


def test_admin_auth_csrf_and_secret_update(store, monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-admin-password")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    app = create_app(store=store, run_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/state").status_code == 401
        client.auth = ("admin", "test-only-admin-password")
        assert client.put("/api/settings", json={"webhook": WEBHOOK}).status_code == 403
        headers = {"X-Gold-Watch": "1"}
        response = client.put("/api/settings", json={"webhook": WEBHOOK}, headers=headers)
        assert response.status_code == 200
        assert "test-secret-token" not in response.text
        response = client.put("/api/settings", json={"webhook": "", "scheduler_enabled": True}, headers=headers)
        assert response.status_code == 200
        assert store.settings().webhook == WEBHOOK
        assert store.settings().scheduler_enabled
        response = client.put("/api/settings", json={"clear_secrets": ["webhook"]}, headers=headers)
        assert response.status_code == 422  # Must pause before removing the destination.
        assert store.settings().webhook == WEBHOOK
        response = client.put("/api/settings", json={"scheduler_enabled": False, "clear_secrets": ["webhook"]}, headers=headers)
        assert response.status_code == 200
        assert store.settings().webhook == ""


def test_validation_error_never_echoes_submitted_webhook(store, monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-admin-password")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(create_app(store=store, run_scheduler=False)) as client:
        response = client.put("/api/settings", json={"webhook": "https://evil.example/secret-token"},
                              headers={"X-Gold-Watch": "1"}, auth=("admin", "test-only-admin-password"))
        assert response.status_code == 422
        assert "secret-token" not in response.text
