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
from database import init_db as init_users_db, close_db, get_user_balance, get_or_create_user, check_and_spend_message, add_messages, give_channel_bonus, track_referral_click, track_referral_conversion
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

PACKAGES = {
    "pack_30":  {"stars": 30,  "messages": 10,  "label": "30 ⭐ — 10 сообщений"},
    "pack_140": {"stars": 140, "messages": 50,  "label": "140 ⭐ — 50 сообщений"},
    "pack_250": {"stars": 250, "messages": 100, "label": "250 ⭐ — 100 сообщений"},
    "pack_550": {"stars": 550, "messages": 250, "label": "550 ⭐ — 250 сообщений"},
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

def get_topup_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=pack["label"], callback_data=key)]
        for key, pack in PACKAGES.items()
    ])
    
def get_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url="https://t.me/po_nyting")],
        [InlineKeyboardButton(text="✅ Я подписался!", callback_data="check_subscription")],
    ])

def get_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Пополнить баланс", callback_data="show_topup")],
        [InlineKeyboardButton(text="📢 Канал Нытика",     url="https://t.me/po_nyting")],
        [InlineKeyboardButton(text="💌 Support",          url="https://t.me/BestieSupport_Bot")],
    ])

# ── Команды ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    _, is_new = await get_or_create_user(
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

    await message.answer(
        "🎁 Подпишись на наш канал и получи +10 сообщений бесплатно!",
        reply_markup=get_channel_keyboard()
    )

    name = message.from_user.first_name
    greeting_line = f"Привет, {name}! 👋" if name else "Привет! 👋"

    await message.answer(
        f"{greeting_line} Здесь можно поныть сколько влезет. Я буду слушать и поддерживать тебя. "
        f"Расскажи - как ты сегодня?\n\n"
        f"/start — начать заново\n"
        f"/clear — очистить историю\n"
        f"/subscribe — подписка"
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
        "Подписка скоро появится здесь 🚧 А пока можно пополнить баланс сообщений:",
        reply_markup=get_topup_button()
    )

@dp.callback_query(F.data == "show_topup")
async def show_topup_callback(callback: CallbackQuery):
    await callback.message.answer("Выбери пакет:", reply_markup=get_topup_button())
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
        pack_key, user_id_str = payload.split(":")
        user_id = int(user_id_str)
    except Exception:
        return

    pack = PACKAGES.get(pack_key)
    if not pack:
        return

    success = await add_messages(
        user_id=user_id,
        messages_amount=pack["messages"],
        stars_amount=pack["stars"],
        telegram_charge_id=payment.telegram_payment_charge_id
    )

    if success:
        await message.answer(
            f"Ура! на твой счёт зачислено {pack['messages']} сообщений 💬"
        )

@dp.callback_query(F.data.in_(PACKAGES.keys()))
async def buy_package(callback: CallbackQuery):
    pack = PACKAGES.get(callback.data)
    if not pack:
        try:
            await callback.answer("пакет не найден")
        except Exception:
            pass
        return

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Сообщения для Нытика",
        description=pack["label"],
        payload=f"{callback.data}:{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=pack["label"], amount=pack["stars"])],
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
            "Спасибо за подписку! +10 сообщений зачислено 💖",
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

    await get_or_create_user(user_id, username=message.from_user.username, first_name=message.from_user.first_name)

    # ── Проверка лимита ДО запроса к OpenAI ──
    spend_result = await check_and_spend_message(user_id)

    if spend_result == "banned":
        return

    if spend_result == "no_messages":
        await message.answer(
            "Ой, похоже у тебя кончились сообщения 🌸\n"
            "пополни баланс — и продолжим болтать!",
            reply_markup=get_topup_button()
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
        balance_data = await get_user_balance(message.from_user.id)
        if balance_data["messages_balance"] == 0:
            await message.answer(
                "Ой, это было последнее бесплатное сообщение на сегодня 🌸\n"
                "завтра в полночь (UTC+3) снова 3 бесплатных — или пополни баланс прямо сейчас!",
                reply_markup=get_topup_button()
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
    balance = balance_data["messages_balance"]
    free_left = balance_data["free_left"]

    text = (
        f"💬 Бесплатных сегодня: {free_left} из 3\n"
        f"⭐ Куплено сообщений: {balance}\n\n"
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
    ])
    asyncio.create_task(set_webhook_delayed())
    print("Бот запущен")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()
    await close_db()
    await close_redis()

def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    app.router.add_get('/set_webhook', handle_set_webhook)

    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()