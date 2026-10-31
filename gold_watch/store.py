import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .models import Quote, Settings, now_local

SECRET_FIELDS = ("webhook", "dingtalk_secret", "goldapi_key", "juhe_key")


class Store:
    def __init__(self, path: Path, encryption_key: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.cipher = Fernet(encryption_key.encode())
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
                    quote_time TEXT NOT NULL, body TEXT NOT NULL,
                    UNIQUE(kind, source, quote_time)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, slot TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '', UNIQUE(kind, slot)
                );
            """)
        if not self.has_settings():
            self.save_settings(Settings())

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def has_settings(self):
        with self.db() as db:
            return db.execute("SELECT 1 FROM settings WHERE id=1").fetchone() is not None

    def settings(self) -> Settings:
        with self.db() as db:
            data = json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])
        try:
            for name in SECRET_FIELDS:
                data[name] = self.cipher.decrypt(data[name].encode()).decode() if data[name] else ""
        except InvalidToken as exc:
            raise RuntimeError("配置解密失败，请恢复原 APP_ENCRYPTION_KEY；不要重新生成密钥") from exc
        return Settings.model_validate(data)

    def public_settings(self):
        data = self.settings().model_dump(mode="json")
        for name in SECRET_FIELDS:
            data[f"{name}_configured"] = bool(data[name])
            data[name] = ""
        return data

    def save_settings(self, settings: Settings):
        data = settings.model_dump(mode="json")
        for name in SECRET_FIELDS:
            data[name] = self.cipher.encrypt(data[name].encode()).decode() if data[name] else ""
        with self.db() as db:
            db.execute("INSERT INTO settings VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                       (json.dumps(data, ensure_ascii=False),))

    def save_quote(self, quote: Quote):
        with self.db() as db:
            db.execute("INSERT INTO quotes(kind,source,quote_time,body) VALUES (?,?,?,?) "
                       "ON CONFLICT(kind,source,quote_time) DO UPDATE SET body=excluded.body",
                       (quote.kind, quote.source, quote.quote_time.isoformat(), quote.model_dump_json()))

    def recent_quotes(self, kind: str, limit=120):
        with self.db() as db:
            rows = db.execute("SELECT body FROM quotes WHERE kind=? ORDER BY id DESC LIMIT ?",
                              (kind, limit)).fetchall()
        return [Quote.model_validate_json(row[0]) for row in rows]

    def previous_quote(self, quote: Quote):
        # A change in source, contract, currency, or price type is a break in the series.
        for old in self.recent_quotes(quote.kind):
            if (old.source == quote.source and old.symbol == quote.symbol and old.unit == quote.unit
                    and old.currency == quote.currency and old.price_type == quote.price_type
                    and old.quote_time < quote.quote_time):
                return old
        return None

    def claim(self, kind: str, slot: str) -> int | None:
        now = now_local().isoformat()
        with self.db() as db:
            cursor = db.execute("INSERT OR IGNORE INTO deliveries(kind,slot,status,created_at,updated_at) "
                                "VALUES (?,?,'collecting',?,?)", (kind, slot, now, now))
            return cursor.lastrowid if cursor.rowcount else None

    def delivery(self, delivery_id: int, status: str, *, title="", message="", detail=""):
        with self.db() as db:
            db.execute("UPDATE deliveries SET status=?,updated_at=?,title=?,message=?,detail=? WHERE id=?",
                       (status, now_local().isoformat(), title, message, detail, delivery_id))

    def recover(self):
        # A process may have died after the recipient accepted the message. Never auto-resend it.
        with self.db() as db:
            db.execute("UPDATE deliveries SET status=CASE WHEN status='sending' THEN 'uncertain' "
                       "ELSE 'failed' END,detail='进程曾中断，请核对记录后手动决定是否重发',updated_at=? "
                       "WHERE status IN ('collecting','sending')", (now_local().isoformat(),))

    def history(self, limit=40):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def prune(self):
        cutoff = (now_local() - timedelta(days=365)).isoformat()
        with self.db() as db:
            db.execute("DELETE FROM deliveries WHERE created_at < ?", (cutoff,))
            # UTC-normalized quote timestamps are compared in Python to avoid offset ordering errors.
            rows = db.execute("SELECT id,quote_time FROM quotes").fetchall()
            old_ids = [(r["id"],) for r in rows if datetime.fromisoformat(r["quote_time"]) < now_local() - timedelta(days=365)]
            db.executemany("DELETE FROM quotes WHERE id=?", old_ids)
