"""Слой базы данных (SQLite через aiosqlite)."""
import json
from datetime import datetime, timezone

import aiosqlite

from config import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                tg_id       INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                source      TEXT,
                quiz        TEXT DEFAULT '{}',
                status      TEXT DEFAULT 'lead',   -- lead | paid
                created_at  TEXT,
                paid_at     TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER,
                provider    TEXT,
                amount      REAL,
                currency    TEXT,
                external_id TEXT,
                status      TEXT DEFAULT 'pending', -- pending | paid | failed
                created_at  TEXT,
                paid_at     TEXT
            )"""
        )
        await db.commit()


async def upsert_user(tg_id: int, username: str | None, first_name: str | None,
                      source: str | None) -> None:
    """Создаёт пользователя при первом контакте. source пишется только один раз."""
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO users (tg_id, username, first_name, source, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tg_id, username, first_name, source, _now()),
            )
        else:
            await db.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE tg_id = ?",
                (username, first_name, tg_id),
            )
        await db.commit()


async def get_quiz(tg_id: int) -> dict:
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("SELECT quiz FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
    return json.loads(row[0]) if row and row[0] else {}


async def save_quiz_answer(tg_id: int, key: str, value: str) -> dict:
    answers = await get_quiz(tg_id)
    answers[key] = value
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE users SET quiz = ? WHERE tg_id = ?",
                         (json.dumps(answers, ensure_ascii=False), tg_id))
        await db.commit()
    return answers


async def is_paid(tg_id: int) -> bool:
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("SELECT status FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
    return bool(row and row[0] == "paid")


async def mark_user_paid(tg_id: int) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE users SET status = 'paid', paid_at = ? WHERE tg_id = ?",
                         (_now(), tg_id))
        await db.commit()


async def create_payment(tg_id: int, provider: str, amount: float, currency: str,
                         external_id: str) -> int:
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO payments (tg_id, provider, amount, currency, external_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tg_id, provider, amount, currency, external_id, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def get_payment(payment_id: int) -> dict | None:
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_payment_status(payment_id: int, status: str) -> None:
    paid_at = _now() if status == "paid" else None
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE payments SET status = ?, paid_at = ? WHERE id = ?",
                         (status, paid_at, payment_id))
        await db.commit()


async def get_pending_payments() -> list[dict]:
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments WHERE status = 'pending'")
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def all_user_ids() -> list[int]:
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("SELECT tg_id FROM users")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def stats() -> dict:
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE status = 'paid'")
        paid = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid' AND currency = 'RUB'")
        revenue = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COALESCE(source, 'direct') AS s, COUNT(*) FROM users GROUP BY s ORDER BY COUNT(*) DESC")
        by_source = await cur.fetchall()
    conv = (paid / total * 100) if total else 0
    return {
        "total": total,
        "paid": paid,
        "conversion": conv,
        "revenue": revenue,
        "by_source": list(by_source),
    }
