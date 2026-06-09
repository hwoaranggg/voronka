"""Бот-продажник мануала по чаттингу. Aiogram 3.

Запуск: python bot.py  (после заполнения .env)
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import payments
import texts
from config import config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

router = Router()
bot: Bot = None  # type: ignore[assignment]  # создаётся в main()
http: aiohttp.ClientSession | None = None  # создаётся в main()


class Broadcast(StatesGroup):
    waiting_message = State()


class AgencyFlow(StatesGroup):
    waiting_nick = State()


# ---------------- утилиты ----------------

async def notify_admins(text: str) -> None:
    for admin in config.admin_ids:
        try:
            await bot.send_message(admin, text)
        except Exception:  # noqa: BLE001
            pass


def is_admin(tg_id: int) -> bool:
    return tg_id in config.admin_ids


def next_question(answers: dict) -> dict | None:
    """Первый ещё не отвеченный вопрос квиза, либо None если квиз пройден."""
    for q in texts.QUIZ:
        if q["id"] not in answers:
            return q
    return None


async def send_question(chat_id: int, question: dict) -> None:
    kb = InlineKeyboardBuilder()
    for label, value in question["options"]:
        kb.button(text=label, callback_data=f"q:{question['id']}:{value}")
    kb.adjust(1)
    await bot.send_message(chat_id, question["text"], reply_markup=kb.as_markup())


async def send_offer(chat_id: int) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BUY_BTN, callback_data="buy")
    await bot.send_message(chat_id, texts.offer_text(), reply_markup=kb.as_markup())


async def grant_access(tg_id: int) -> None:
    """Помечает оплату, выдаёт одноразовую инвайт-ссылку, уведомляет админов."""
    await db.mark_user_paid(tg_id)
    expire = datetime.now(timezone.utc) + timedelta(hours=config.invite_expire_hours)
    try:
        link = await bot.create_chat_invite_link(
            config.channel_id, member_limit=1, expire_date=expire)
        await bot.send_message(tg_id, texts.access_text(link.invite_link),
                               disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        log.error("Не удалось создать инвайт-ссылку: %s", e)
        await bot.send_message(
            tg_id, "Оплата получена ✅ Но не вышло выдать ссылку автоматически — "
                   "напиши в поддержку, выдам вручную.")
    for admin in config.admin_ids:
        try:
            await bot.send_message(admin, f"💰 Новая оплата! Пользователь <code>{tg_id}</code>")
        except Exception:  # noqa: BLE001
            pass


# ---------------- воронка агентства / рефералы ----------------

@router.message(Command("agency"))
async def cmd_agency(message: Message) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.AGENCY_WANT_BTN, callback_data="ag:want")
    await message.answer(texts.agency_pitch(), reply_markup=kb.as_markup())


@router.callback_query(F.data == "ag:want")
async def agency_want(call: CallbackQuery) -> None:
    await db.start_referral(call.from_user.id, call.from_user.username)
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.AGENCY_WROTE_BTN, callback_data="ag:wrote")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(texts.agency_instruction(), reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "ag:wrote")
async def agency_wrote(call: CallbackQuery) -> None:
    await db.set_referral_stage(call.from_user.id, "wrote")
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.AGENCY_WEBINAR_BTN, callback_data="ag:webinar")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(texts.AGENCY_AFTER_WROTE, reply_markup=kb.as_markup())
    uname = f"@{call.from_user.username}" if call.from_user.username else call.from_user.id
    await notify_admins(f"📝 Реферал {uname} написал в агентство (ожидает вебинар)")
    await call.answer()


@router.callback_query(F.data == "ag:webinar")
async def agency_webinar(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.edit_reply_markup(reply_markup=None)
    await state.set_state(AgencyFlow.waiting_nick)
    await call.message.answer(texts.AGENCY_ASK_NICK)
    await call.answer()


@router.message(AgencyFlow.waiting_nick)
async def agency_nick(message: Message, state: FSMContext) -> None:
    await state.clear()
    nick = message.text.strip()
    await db.set_referral_nick(message.from_user.id, nick)
    # выдаём счётчик смен
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.agency_shift_btn(0), callback_data="ag:shift")
    await message.answer(texts.agency_curator(config.curator_username),
                         reply_markup=kb.as_markup())
    uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.id
    await notify_admins(
        f"🎓 Реферал {uname} прошёл вебинар!\n"
        f"Ник в агентстве: <b>{nick}</b>\nПроверь, что он реально от тебя.")


@router.callback_query(F.data == "ag:shift")
async def agency_shift(call: CallbackQuery) -> None:
    ref = await db.get_referral(call.from_user.id)
    if ref and ref["shifts"] >= 5:
        await call.answer("Стажировка уже пройдена 🎉")
        return
    shifts = await db.inc_referral_shift(call.from_user.id)
    uname = f"@{call.from_user.username}" if call.from_user.username else call.from_user.id
    await notify_admins(f"📅 Реферал {uname} завершил смену {shifts}/5")

    if shifts >= 5:
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(texts.AGENCY_DONE)
        await notify_admins(f"💰 {uname} прошёл 5 смен — время забрать свои $50!")
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text=texts.agency_shift_btn(shifts), callback_data="ag:shift")
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    await call.answer(f"Смена {shifts}/5 засчитана ✅")


# ---------------- старт + квиз ----------------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    source = command.args  # диплинк-метка: t.me/bot?start=tiktok1
    await db.upsert_user(message.from_user.id, message.from_user.username,
                         message.from_user.first_name, source)

    if await db.is_paid(message.from_user.id):
        await message.answer(texts.ALREADY_PAID)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text=texts.START_BTN, callback_data="quiz_start")
    await message.answer(texts.WELCOME, reply_markup=kb.as_markup())


@router.callback_query(F.data == "quiz_start")
async def quiz_start(call: CallbackQuery) -> None:
    await call.message.delete()
    q = next_question(await db.get_quiz(call.from_user.id))
    if q:
        await send_question(call.from_user.id, q)
    else:
        await send_offer(call.from_user.id)
    await call.answer()


@router.callback_query(F.data.startswith("q:"))
async def quiz_answer(call: CallbackQuery) -> None:
    _, key, value = call.data.split(":", 2)
    answers = await db.save_quiz_answer(call.from_user.id, key, value)
    await call.message.edit_reply_markup(reply_markup=None)  # убираем кнопки у отвеченного

    q = next_question(answers)
    if q:
        await send_question(call.from_user.id, q)
    else:
        pitch = texts.build_pitch(answers)
        await bot.send_message(call.from_user.id, texts.QUIZ_DONE)
        if pitch:
            await bot.send_message(call.from_user.id, pitch)
        await send_offer(call.from_user.id)
    await call.answer()


# ---------------- оплата ----------------

@router.callback_query(F.data == "buy")
async def buy(call: CallbackQuery) -> None:
    if await db.is_paid(call.from_user.id):
        await call.message.answer(texts.ALREADY_PAID)
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.PAY_CARD_BTN, callback_data="pay:card")
    kb.button(text=texts.PAY_CRYPTO_BTN, callback_data="pay:crypto")
    kb.adjust(2)
    await call.message.answer("Выбери способ оплаты:", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("pay:"))
async def pay(call: CallbackQuery) -> None:
    method = call.data.split(":", 1)[1]
    tg_id = call.from_user.id

    # Tribute: просто отдаём ссылку на товар, доступ выдаёт сама Tribute / подтверждаем вручную
    if method == "card" and config.card_provider == "tribute":
        kb = InlineKeyboardBuilder()
        kb.button(text=texts.PAY_BTN, url=config.tribute_link)
        await call.message.answer(
            "Оплати картой по кнопке ниже 👇 После оплаты доступ придёт автоматически.",
            reply_markup=kb.as_markup())
        await call.answer()
        return

    provider = "crypto" if method == "crypto" else "yukassa"
    desc = f"Мануал по чаттингу (uid {tg_id})"
    try:
        url, ext_id = await payments.create_invoice(
            http, provider, config.price_rub, desc, payload=str(tg_id))
    except Exception as e:  # noqa: BLE001
        log.error("create_invoice failed: %s", e)
        await call.message.answer(texts.PAY_ERROR)
        await call.answer()
        return

    pay_id = await db.create_payment(tg_id, provider, config.price_rub, "RUB", ext_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.PAY_BTN, url=url)
    kb.button(text=texts.CHECK_BTN, callback_data=f"check:{pay_id}")
    kb.adjust(1)
    await call.message.answer(texts.INVOICE_CREATED, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("check:"))
async def check(call: CallbackQuery) -> None:
    pay_id = int(call.data.split(":", 1)[1])
    payment = await db.get_payment(pay_id)
    if not payment or payment["tg_id"] != call.from_user.id:
        await call.answer("Счёт не найден", show_alert=True)
        return
    if payment["status"] == "paid" or await db.is_paid(call.from_user.id):
        await call.answer("Доступ уже выдан ✅", show_alert=True)
        return

    status = await payments.check_invoice(http, payment["provider"], payment["external_id"])
    if status == "paid":
        await db.set_payment_status(pay_id, "paid")
        await grant_access(call.from_user.id)
        await call.answer("Оплата получена ✅")
    else:
        await call.answer(texts.NOT_PAID_YET, show_alert=True)


# ---------------- админка ----------------

@router.message(Command("admin"))
async def admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.button(text="👥 Рефералы", callback_data="adm:refs")
    kb.button(text="📣 Рассылка", callback_data="adm:broadcast")
    kb.adjust(1)
    await message.answer("Админ-панель:", reply_markup=kb.as_markup())


@router.callback_query(F.data == "adm:stats")
async def admin_stats(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    s = await db.stats()
    sources = "\n".join(f"  • {src}: {cnt}" for src, cnt in s["by_source"]) or "  —"
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Всего пользователей: <b>{s['total']}</b>\n"
        f"Оплатили: <b>{s['paid']}</b>\n"
        f"Конверсия: <b>{s['conversion']:.1f}%</b>\n"
        f"Выручка (₽): <b>{s['revenue']:.0f}</b>\n\n"
        f"По источникам:\n{sources}"
    )
    await call.message.answer(text)
    await call.answer()


@router.callback_query(F.data == "adm:refs")
async def admin_refs(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    rs = await db.referral_stats()
    refs = await db.referral_list()

    stage_names = {
        "started": "начал", "wrote": "написал в агентство",
        "webinar": "прошёл вебинар", "working": "на стажировке",
        "completed": "5 смен ✅",
    }
    lines = []
    for r in refs[:30]:
        uname = f"@{r['username']}" if r["username"] else r["tg_id"]
        nick = f" / {r['agency_nick']}" if r["agency_nick"] else ""
        stage = stage_names.get(r["stage"], r["stage"])
        paid = " 💵забрано" if r["paid_out"] else ""
        lines.append(f"• {uname}{nick} — {stage} ({r['shifts']}/5){paid}")
    body = "\n".join(lines) or "Пока нет рефералов."

    potential = (rs["completed"] - rs["paid_out"]) * 50
    text = (
        "👥 <b>Рефералы</b>\n\n"
        f"Всего: <b>{rs['total']}</b>\n"
        f"Прошли 5 смен: <b>{rs['completed']}</b>\n"
        f"Выплат забрано: <b>{rs['paid_out']}</b>\n"
        f"К получению: <b>${potential}</b>\n\n"
        f"{body}"
    )
    kb = InlineKeyboardBuilder()
    has_btn = False
    for r in refs:
        if r["shifts"] >= 5 and not r["paid_out"]:
            label = r["agency_nick"] or (f"@{r['username']}" if r["username"] else str(r["tg_id"]))
            kb.button(text=f"💵 Забрал $50: {label}", callback_data=f"adm:payout:{r['tg_id']}")
            has_btn = True
    kb.adjust(1)
    await call.message.answer(text, reply_markup=kb.as_markup() if has_btn else None)
    await call.answer()


@router.callback_query(F.data.startswith("adm:payout:"))
async def admin_payout(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    tg_id = int(call.data.rsplit(":", 1)[1])
    await db.mark_referral_paidout(tg_id)
    await call.answer("Отмечено как выплаченное 💵", show_alert=True)
    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "adm:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(Broadcast.waiting_message)
    await call.message.answer("Пришли сообщение для рассылки (текст/фото/видео). /cancel — отмена.")
    await call.answer()


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(Broadcast.waiting_message)
async def do_broadcast(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    ids = await db.all_user_ids()
    ok = fail = 0
    await message.answer(f"Начинаю рассылку на {len(ids)} пользователей...")
    for uid in ids:
        try:
            await message.copy_to(uid)
            ok += 1
        except Exception:  # noqa: BLE001 — заблокировали бота и т.п.
            fail += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек, бережём лимиты
    await message.answer(f"Готово ✅ Доставлено: {ok}, не дошло: {fail}")


# ---------------- фоновая проверка оплат ----------------

async def payment_poller() -> None:
    """Периодически проверяет висящие счета и выдаёт доступ без действий юзера."""
    while True:
        await asyncio.sleep(config.poll_interval)
        try:
            for p in await db.get_pending_payments():
                try:
                    status = await payments.check_invoice(http, p["provider"], p["external_id"])
                except Exception:  # noqa: BLE001
                    continue
                if status == "paid":
                    await db.set_payment_status(p["id"], "paid")
                    if not await db.is_paid(p["tg_id"]):
                        await grant_access(p["tg_id"])
                elif status == "failed":
                    await db.set_payment_status(p["id"], "failed")
        except Exception as e:  # noqa: BLE001
            log.error("poller error: %s", e)


# ---------------- запуск ----------------

async def main() -> None:
    global http, bot
    if not config.bot_token:
        raise SystemExit("Заполни BOT_TOKEN в .env")
    bot = Bot(token=config.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await db.init_db()
    http = aiohttp.ClientSession()
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(payment_poller())
    log.info("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        await http.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
