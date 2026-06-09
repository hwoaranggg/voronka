"""Платёжные провайдеры: CryptoBot (крипта) и YuKassa (карты).

Каждый провайдер умеет:
  create(...)  -> (pay_url, external_id)
  check(...)   -> "paid" | "pending" | "failed"
"""
import uuid

import aiohttp

from config import config

CRYPTO_API = "https://pay.crypt.bot/api"
YK_API = "https://api.yookassa.ru/v3/payments"


# ---------------- CryptoBot ----------------

async def crypto_create(session: aiohttp.ClientSession, amount: float,
                        description: str, payload: str) -> tuple[str, str]:
    headers = {"Crypto-Pay-API-Token": config.cryptobot_token}
    data = {
        "currency_type": "fiat",
        "fiat": config.crypto_fiat,
        "amount": str(amount),
        "description": description,
        "payload": payload,
        "expires_in": 3600,
    }
    async with session.post(f"{CRYPTO_API}/createInvoice", json=data, headers=headers) as r:
        res = await r.json()
    if not res.get("ok"):
        raise RuntimeError(f"CryptoBot error: {res}")
    inv = res["result"]
    url = inv.get("bot_invoice_url") or inv.get("mini_app_invoice_url") or inv.get("web_app_invoice_url")
    return url, str(inv["invoice_id"])


async def crypto_check(session: aiohttp.ClientSession, invoice_id: str) -> str:
    headers = {"Crypto-Pay-API-Token": config.cryptobot_token}
    async with session.get(f"{CRYPTO_API}/getInvoices",
                           params={"invoice_ids": invoice_id}, headers=headers) as r:
        res = await r.json()
    items = res.get("result", {}).get("items", [])
    if not items:
        return "pending"
    status = items[0]["status"]  # active | paid | expired
    if status == "paid":
        return "paid"
    if status == "expired":
        return "failed"
    return "pending"


# ---------------- YuKassa ----------------

async def yk_create(session: aiohttp.ClientSession, amount_rub: float,
                    description: str) -> tuple[str, str]:
    auth = aiohttp.BasicAuth(config.yk_shop_id, config.yk_secret)
    headers = {"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
    data = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": config.yk_return_url},
        "description": description,
    }
    async with session.post(YK_API, json=data, headers=headers, auth=auth) as r:
        res = await r.json()
    if "confirmation" not in res:
        raise RuntimeError(f"YuKassa error: {res}")
    return res["confirmation"]["confirmation_url"], res["id"]


async def yk_check(session: aiohttp.ClientSession, payment_id: str) -> str:
    auth = aiohttp.BasicAuth(config.yk_shop_id, config.yk_secret)
    async with session.get(f"{YK_API}/{payment_id}", auth=auth) as r:
        res = await r.json()
    status = res.get("status")  # pending | waiting_for_capture | succeeded | canceled
    if status == "succeeded":
        return "paid"
    if status == "canceled":
        return "failed"
    return "pending"


# ---------------- единый интерфейс ----------------

async def create_invoice(session: aiohttp.ClientSession, provider: str,
                         amount: float, description: str, payload: str) -> tuple[str, str]:
    if provider == "crypto":
        return await crypto_create(session, amount, description, payload)
    if provider == "yukassa":
        return await yk_create(session, amount, description)
    raise ValueError(f"Unknown provider: {provider}")


async def check_invoice(session: aiohttp.ClientSession, provider: str,
                        external_id: str) -> str:
    if provider == "crypto":
        return await crypto_check(session, external_id)
    if provider == "yukassa":
        return await yk_check(session, external_id)
    raise ValueError(f"Unknown provider: {provider}")
