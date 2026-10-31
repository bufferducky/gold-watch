#!/usr/bin/env python3
"""Generate deployment secrets without third-party dependencies or console disclosure."""
import base64
import os
import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / ".env"
body = ("ADMIN_USERNAME=admin\n"
        f"ADMIN_PASSWORD={secrets.token_urlsafe(24)}\n"
        f"APP_ENCRYPTION_KEY={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}\n"
        "PORT=8080\nBIND_ADDRESS=127.0.0.1\nTZ=Asia/Shanghai\n")
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env 已存在，保留原密码与加密密钥，未作修改。") from None
with os.fdopen(fd, "w") as stream:
    stream.write(body)
print("已生成 .env（权限 600）。登录用户名为 admin，请在本机查看 .env 中的密码。")
