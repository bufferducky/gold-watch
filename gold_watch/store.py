import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .models import (
    DOMESTIC_TEMPLATE,
    INTERNATIONAL_TEMPLATE,
    LEGACY_DOMESTIC_TEMPLATE,
    LEGACY_INTERNATIONAL_TEMPLATE,
    PRE_CONVERSION_INTERNATIONAL_TEMPLATE,
    SHANGHAI,
    ExchangeRate,
    Quote,
    Settings,
    now_local,
)
from .trends import same_series, weekly_trend

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
                CREATE TABLE IF NOT EXISTS exchange_rates (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
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
            if "quote" not in {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}:
                db.execute("ALTER TABLE deliveries ADD COLUMN quote TEXT NOT NULL DEFAULT ''")
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
        # Upgrade only untouched defaults; preserve user-authored templates and notes.
        for kind, old, new in (("international", LEGACY_INTERNATIONAL_TEMPLATE, INTERNATIONAL_TEMPLATE),
                               ("international", PRE_CONVERSION_INTERNATIONAL_TEMPLATE, INTERNATIONAL_TEMPLATE),
                               ("domestic", LEGACY_DOMESTIC_TEMPLATE, DOMESTIC_TEMPLATE)):
            if data[kind]["template"] == old:
                data[kind]["template"] = new
            if data[kind]["note"] == "行情用于观察，实物金条成交价需另加销售溢价及费用。":
                data[kind]["note"] = "实物金条价格另含溢价及费用。"
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

    def exchange_rate(self) -> ExchangeRate | None:
        with self.db() as db:
            row = db.execute("SELECT body FROM exchange_rates WHERE id=1").fetchone()
        return ExchangeRate.model_validate_json(row[0]) if row else None

    def save_exchange_rate(self, rate: ExchangeRate):
        with self.db() as db:
            db.execute("INSERT INTO exchange_rates(id,body) VALUES (1,?) "
                       "ON CONFLICT(id) DO UPDATE SET body=excluded.body", (rate.model_dump_json(),))

    def previous_quote(self, quote: Quote):
        # Only confirmed successful deliveries are a comparison baseline, never previews.
        with self.db() as db:
            rows = db.execute("SELECT quote FROM deliveries WHERE kind=? AND status='success' "
                              "AND quote!='' ORDER BY id DESC", (quote.kind,)).fetchall()
        for row in rows:
            old = Quote.model_validate_json(row[0])
            if same_series(old, quote) and old.quote_time <= quote.quote_time:
                return old
        return None

    def trend(self, kind: str, reference: Quote | None = None, at: datetime | None = None):
        at = at or now_local()
        cutoff = datetime.combine(at.astimezone(SHANGHAI).date() - timedelta(days=6),
                                  datetime.min.time(), SHANGHAI)
        with self.db() as db:
            rows = db.execute("SELECT body FROM quotes WHERE kind=? AND julianday(quote_time)>=julianday(?) "
                              "AND julianday(quote_time)<=julianday(?)", (kind, cutoff.isoformat(), at.isoformat())).fetchall()
        quotes = [Quote.model_validate_json(row[0]) for row in rows]
        if reference is None:
            recent = self.recent_quotes(kind, limit=1)
            reference = recent[0] if recent else None
        return weekly_trend(quotes, reference, at)

    def claim(self, kind: str, slot: str) -> int | None:
        now = now_local().isoformat()
        with self.db() as db:
            cursor = db.execute("INSERT OR IGNORE INTO deliveries(kind,slot,status,created_at,updated_at) "
                                "VALUES (?,?,'collecting',?,?)", (kind, slot, now, now))
            return cursor.lastrowid if cursor.rowcount else None

    def delivery(self, delivery_id: int, status: str, *, title="", message="", detail="", quote: Quote | None = None):
        with self.db() as db:
            db.execute("UPDATE deliveries SET status=?,updated_at=?,title=?,message=?,detail=?, "
                       "quote=COALESCE(?,quote) WHERE id=?",
                       (status, now_local().isoformat(), title, message, detail,
                        quote.model_dump_json() if quote else None, delivery_id))

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
