import asyncio
import os
import yaml
from datetime import datetime
from dotenv import load_dotenv
import logging
from openai import AsyncOpenAI
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    PreCheckoutQuery, LabeledPrice
)
from aiogram.filters import CommandStart, Command
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from analytics import log_turn
from analytics_db import init_db as init_analytics_db
from database import init_db as init_users_db, close_db, get_user_balance, get_or_create_user, check_and_spend_message, activate_subscription, give_channel_bonus, track_referral_click, track_referral_conversion, MSK
from redis_client import (
    init_redis, close_redis,
    get_user_character, set_user_character,
    get_chat_history, set_chat_history, clear_chat_history
)
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

CHANNEL_USERNAME = "@po_nyting"

SUBSCRIPTIONS = {
    "sub_week":  {"stars": 250, "days": 7,  "label": "Неделя безлимита — 250 ⭐"},
    "sub_month": {"stars": 650, "days": 30, "label": "Месяц безлимита — 650 ⭐"},
}

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


def load_character(filename: str) -> dict:
    path = os.path.join("Characters", filename)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
        if not data:
            raise ValueError(f"Файл {filename} пустой или поврежден")
        return data


CHARACTER_KEY = "ponyting"

CHARACTERS = {
    CHARACTER_KEY: {"yaml": "ponyting.yaml", "label": "Нытинг"},
}

CHARACTER_DATA: dict = {}

def preload_characters():
    for key, meta in CHARACTERS.items():
        CHARACTER_DATA[key] = load_character(meta["yaml"])
        
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# ── Кнопки ────────────────────────────────────────────────────────────────────

def get_subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sub["label"], callback_data=key)]
        for key, sub in SUBSCRIPTIONS.items()
    ])

    
def get_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url="https://t.me/po_nyting")],
        [InlineKeyboardButton(text="✅ Я подписался!", callback_data="check_subscription")],
    ])

def get_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Оформить подписку", callback_data="show_subscribe")],
        [InlineKeyboardButton(text="📢 Канал Нытика",      url="https://t.me/po_nyting")],
        [InlineKeyboardButton(text="💌 Support",           url="https://t.me/BestieSupport_Bot")],
    ])

# ── Команды ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user_row, is_new = await get_or_create_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )

    args = message.text.split()
    ref_code = args[1][4:] if len(args) > 1 and args[1].startswith("ref_") else None

    if ref_code:
        await track_referral_click(ref_code, user_id)
        if is_new:
            await track_referral_conversion(ref_code, user_id)

    await _set_character(user_id, CHARACTER_KEY, message.from_user.first_name)

    user_row, _ = await get_or_create_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    if not user_row["channel_bonus_given"]:
        await message.answer(
            "🎁 Подпишись на наш канал и получи 3 дня подписки бесплатно!",
            reply_markup=get_channel_keyboard()
        )

    name = message.from_user.first_name
    greeting_line = f"Привет, {name}! 👋" if name else "Привет! 👋"

    await message.answer(
        f"{greeting_line} Я здесь, чтобы выслушать и поддержать тебя. Расскажи — как ты сегодня? "
       )


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    await _send_profile(message.from_user.id, reply_to=message)

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    await clear_chat_history(message.from_user.id)
    await message.answer("История очищена 🧹 Давай начнём с чистого листа.")

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    await message.answer(
        "Выбери подписку:",
        reply_markup=get_subscription_keyboard()
    )

@dp.callback_query(F.data == "show_subscribe")
async def show_subscription_callback(callback: CallbackQuery):
    await callback.message.answer("Выбери подписку:", reply_markup=get_subscription_keyboard())
    try:
        await callback.answer()
    except Exception:
        pass

# ── Оплата ────────────────────────────────────────────────────────────────────

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload

    try:
        sub_key, user_id_str = payload.split(":")
        user_id = int(user_id_str)
    except Exception:
        return

    sub = SUBSCRIPTIONS.get(sub_key)
    if not sub:
        return

    success = await activate_subscription(
        user_id=user_id,
        sub_type=sub_key,
        stars_amount=sub["stars"],
        telegram_charge_id=payment.telegram_payment_charge_id,
        duration_days=sub["days"]
    )

    if success:
        await message.answer(
            "Подписка активирована, спасибо! 🎉 Теперь можно общаться без ограничений."
        )

@dp.callback_query(F.data.in_(SUBSCRIPTIONS.keys()))
async def buy_subscription(callback: CallbackQuery):
    sub = SUBSCRIPTIONS.get(callback.data)
    if not sub:
        try:
            await callback.answer("подписка не найдена")
        except Exception:
            pass
        return

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Подписка Нытинг",
        description=sub["label"],
        payload=f"{callback.data}:{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=sub["label"], amount=sub["stars"])],
    )
    try:
        await callback.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        is_subscribed = False
 
    if not is_subscribed:
        await callback.answer(
            "Ты ещё не подписан на канал 🌦️",
            show_alert=True
        )
        return
 
    given = await give_channel_bonus(user_id)
    if given:
        await callback.answer(
            "Спасибо за подписку на канал! +3 дня безлимита активированы 💖",
            show_alert=True
        )
        await callback.message.delete()
    else:
        await callback.answer(
            "Бонус уже был получен ранее 😊",
            show_alert=True
        )
        await callback.message.delete()

# ── Основной обработчик сообщений ─────────────────────────────────────────────

@dp.message()
async def handle_message(message: Message):
    user_text = message.text
    if not user_text:
        return

    user_id = message.from_user.id

    # ── Проверяем персонажа в Redis ──
    character = await get_user_character(user_id)

    if not character:
        character = await _set_character(user_id, CHARACTER_KEY, message.from_user.first_name)

    # ── Проверка лимита ДО запроса к OpenAI ──
    # check_and_spend_message уже гарантирует существование пользователя (INSERT ... ON CONFLICT),
    # отдельный get_or_create_user() здесь был лишним запросом в БД на каждое сообщение
    spend_result = await check_and_spend_message(user_id)

    if spend_result == "banned":
        return

    if spend_result == "no_messages":
        await message.answer(
            "Твои 10 дневных бесплатных сообщений на сегодня кончились 🌙\n"
            "Оформи подписку — и общайся без ограничений:",
            reply_markup=get_subscription_keyboard()
        )
        return
    
    # ── Запрос к OpenAI ──
    reply = "ой, что-то мне нехорошо, давай попозже поболтаем?"
    try:
        stop_typing = asyncio.Event()

        async def keep_typing():
            while not stop_typing.is_set():
                await bot.send_chat_action(message.chat.id, "typing")
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(keep_typing())

        try:
            history = await get_chat_history(user_id)
            history.append({"role": "user", "content": user_text})
            history = history[-20:]

            messages = [
                {"role": "system", "content": character["prompt"]}
            ] + history

            start_time = datetime.now()

            response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.9,
            max_completion_tokens=400,
        )
        finally:
            stop_typing.set()
            typing_task.cancel()

        latency_ms = int((datetime.now() - start_time).total_seconds() * 1000)
        reply = response.choices[0].message.content.strip()

        log_turn(
            user_id=user_id,
            language_code=message.from_user.language_code,
            user_text=user_text,
            bot_reply=reply,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            latency_ms=latency_ms,
            model="gpt-4o-mini",
            character=character["name"]
        )

        # сохраняем историю обратно в Redis
        history.append({"role": "assistant", "content": reply})
        history = history[-20:]
        await set_chat_history(user_id, history)

    except Exception as e:
        logger.exception("Ошибка Главного обработчика")

    await message.answer(reply)

    # ── Уведомление после последнего бесплатного ──
    if spend_result == "last_free":
        await message.answer(
            "Это было последнее бесплатное сообщение на сегодня 🌙\n"
            "Завтра в полночь (UTC+3) снова 10 бесплатных — или оформи подписку прямо сейчас:",
            reply_markup=get_subscription_keyboard()
        )


# ── Вспомогательные функции ───────────────────────────────────────────────────

async def _set_character(user_id: int, char_key: str, first_name: str) -> dict:
    char = CHARACTERS[char_key]
    character_data = CHARACTER_DATA[char_key]
    character = {
        "prompt": character_data["system_instruction"],
        "name": char["label"],
        "user_name": first_name or "друг"
    }
    await set_user_character(user_id, character)
    await clear_chat_history(user_id)
    return character

async def _send_profile(user_id: int, reply_to: Message = None):
    balance_data = await get_user_balance(user_id)

    if balance_data["subscription_active"]:
        expires = balance_data["subscription_expires_at"].astimezone(MSK).strftime("%d.%m.%Y")
        status_line = f"⭐ Подписка активна до {expires}\n\n"
    else:
        free_left = balance_data["free_left"]
        status_line = f"💬 Бесплатных сегодня: {free_left} из 10\n\n"

    text = (
        f"{status_line}"
        f"Иногда нужно просто выговориться ✨ Без осуждения, без непрошеных советов. "
        f"Здесь всегда есть кто-то, готовый выслушать и поддержать ❤️"
    )

    if reply_to:
        await reply_to.answer(text, reply_markup=get_profile_keyboard())
    else:
        await bot.send_message(user_id, text, reply_markup=get_profile_keyboard())

# ── HTTP эндпоинты ────────────────────────────────────────────────────────────

async def handle_set_webhook(request: web.Request):
    token = request.headers.get("X-Secret", "")
    if not WEBHOOK_SECRET or token != WEBHOOK_SECRET:
        return web.json_response({"ok": False}, status=403)
    await bot.delete_webhook(drop_pending_updates=True)
    result = await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    return web.json_response({"ok": True, "webhook": WEBHOOK_URL, "result": str(result)})


# ── Запуск ────────────────────────────────────────────────────────────────────
async def set_webhook_delayed():
    await asyncio.sleep(10)
    for attempt in range(5):
        try:
            await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
            print(f"Webhook установлен: {WEBHOOK_URL}")
            break
        except Exception as e:
            print(f"Webhook попытка {attempt + 1} не удалась: {e}")
            await asyncio.sleep(5)
    else:
        logger.error("Webhook не удалось установить после 5 попыток")
            
async def on_startup(app: web.Application):
    preload_characters()
    await init_analytics_db()
    await init_users_db()
    await init_redis()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="начать заново"),
        BotCommand(command="clear", description="очистить историю"),
        BotCommand(command="subscribe", description="подписка"),
        BotCommand(command="profile", description="профиль"),
    ])
    asyncio.create_task(set_webhook_delayed())
    print("Бот запущен")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()
    await close_db()
    await close_redis()

def main():
    if not WEBHOOK_SECRET:
        raise RuntimeError(
            "WEBHOOK_SECRET не задан — запуск вебхука без секрета небезопасен: "
            "входящие апдейты (включая successful_payment) не будут проверяться."
        )

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    app.router.add_get('/set_webhook', handle_set_webhook)

    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()