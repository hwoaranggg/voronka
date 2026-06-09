"""Конфиг бота. Все значения берутся из переменных окружения (.env)."""
import os
from dataclasses import dataclass, field


def _admins() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


@dataclass
class Config:
    # --- основное ---
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=_admins)
    channel_id: int = int(os.getenv("CHANNEL_ID", "0"))      # ID закрытого канала, формата -100xxxxxxxxxx
    price_rub: int = int(os.getenv("PRICE_RUB", "1500"))

    # --- оплата картой ---
    # card_provider: "yukassa" (полноценное API) или "tribute" (ссылка на товар Tribute)
    card_provider: str = os.getenv("CARD_PROVIDER", "yukassa")
    yk_shop_id: str = os.getenv("YUKASSA_SHOP_ID", "")
    yk_secret: str = os.getenv("YUKASSA_SECRET_KEY", "")
    yk_return_url: str = os.getenv("YUKASSA_RETURN_URL", "https://t.me/")
    tribute_link: str = os.getenv("TRIBUTE_LINK", "")        # если card_provider="tribute"

    # --- оплата криптой (CryptoBot / @CryptoBot) ---
    cryptobot_token: str = os.getenv("CRYPTOBOT_TOKEN", "")
    crypto_fiat: str = os.getenv("CRYPTO_FIAT", "RUB")       # инвойс в рублях, плательщик сам выбирает монету

    # --- база данных ---
    # Railway сам подставит DATABASE_URL при добавлении плагина PostgreSQL.
    database_url: str = os.getenv("DATABASE_URL", "")

    # --- прочее ---
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "30"))  # фоновая проверка оплат, сек
    invite_expire_hours: int = int(os.getenv("INVITE_EXPIRE_HOURS", "24"))


config = Config()
