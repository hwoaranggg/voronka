"""Слой базы данных (PostgreSQL через asyncpg).

Railway автоматически отдаёт строку подключения в переменной DATABASE_URL,
когда ты добавляешь в проект плагин PostgreSQL. Локально задаёшь её сам.
"""
import json
from datetime import datetime, timezone

import asyncpg

from config import config

_pool: asyncpg.Pool | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    global _pool
    _pool = await asyncpg.create_pool(config.database_url)
    async with _pool.acquire() as conn:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                tg_id       BIGINT PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                source      TEXT,
                quiz        TEXT DEFAULT '{}',
                status      TEXT DEFAULT 'lead',
                created_at  TEXT,
                paid_at     TEXT
            )"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                id          BIGSERIAL PRIMARY KEY,
                tg_id       BIGINT,
                provider    TEXT,
                amount      REAL,
                currency    TEXT,
                external_id TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT,
                paid_at     TEXT
            )"""
        )


async def close() -> None:
    if _pool:
        await _pool.close()


async def upsert_user(tg_id: int, username, first_name, source) -> None:
    """Создаёт пользователя при первом контакте. source пишется только один раз."""
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (tg_id, username, first_name, source, created_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (tg_id) DO UPDATE
                 SET username = EXCLUDED.username,
                     first_name = EXCLUDED.first_name""",
            tg_id, username, first_name, source, _now(),
        )


async def get_quiz(tg_id: int) -> dict:
    async with _pool.acquire() as conn:
        raw = await conn.fetchval("SELECT quiz FROM users WHERE tg_id = $1", tg_id)
    return json.loads(raw) if raw else {}


async def save_quiz_answer(tg_id: int, key: str, value: str) -> dict:
    answers = await get_quiz(tg_id)
    answers[key] = value
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE users SET quiz = $1 WHERE tg_id = $2",
                           json.dumps(answers, ensure_ascii=False), tg_id)
    return answers


async def is_paid(tg_id: int) -> bool:
    async with _pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM users WHERE tg_id = $1", tg_id)
    return status == "paid"


async def mark_user_paid(tg_id: int) -> None:
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'paid', paid_at = $1 WHERE tg_id = $2",
                           _now(), tg_id)


async def create_payment(tg_id: int, provider: str, amount: float, currency: str,
                         external_id: str) -> int:
    async with _pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO payments (tg_id, provider, amount, currency, external_id, created_at)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
            tg_id, provider, amount, currency, external_id, _now(),
        )


async def get_payment(payment_id: int):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM payments WHERE id = $1", payment_id)
    return dict(row) if row else None


async def set_payment_status(payment_id: int, status: str) -> None:
    paid_at = _now() if status == "paid" else None
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE payments SET status = $1, paid_at = $2 WHERE id = $3",
                           status, paid_at, payment_id)


async def get_pending_payments() -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM payments WHERE status = 'pending'")
    return [dict(r) for r in rows]


async def all_user_ids() -> list[int]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id FROM users")
    return [r["tg_id"] for r in rows]


async def stats() -> dict:
    async with _pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        paid = await conn.fetchval("SELECT COUNT(*) FROM users WHERE status = 'paid'")
        revenue = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid' AND currency = 'RUB'")
        by_source = await conn.fetch(
            """SELECT COALESCE(source, 'direct') AS s, COUNT(*) AS c
               FROM users GROUP BY COALESCE(source, 'direct') ORDER BY c DESC""")
    conv = (paid / total * 100) if total else 0
    return {
        "total": total,
        "paid": paid,
        "conversion": conv,
        "revenue": revenue,
        "by_source": [(r["s"], r["c"]) for r in by_source],
    }
