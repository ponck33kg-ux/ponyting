import os
import json
import redis.asyncio as aioredis
from datetime import timedelta

REDIS_URL = os.getenv("REDIS_URL")
redis: aioredis.Redis | None = None

# TTL
TTL_CHARACTER      = timedelta(days=7)        # персонаж хранится 7 дней
TTL_HISTORY        = timedelta(minutes=30)    # история сбрасывается через 30 минут неактивности
TTL_DIALOG_SESSION = timedelta(minutes=30)    # сессия диалога для аналитики
TTL_KNOWN_USER     = timedelta(days=365)      # был ли пользователь вообще

async def init_redis():
    """Инициализация подключения к Redis."""
    global redis
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.ping()
    print("Redis подключён")

async def close_redis():
    """Закрыть подключение."""
    global redis
    if redis:
        await redis.aclose()

# ── Персонаж ──────────────────────────────────────────────────────────────────

async def get_user_character(user_id: int) -> dict | None:
    """Получить выбранного персонажа пользователя."""
    data = await redis.get(f"char:{user_id}")
    if data:
        return json.loads(data)
    return None

async def set_user_character(user_id: int, character: dict):
    """Сохранить персонажа пользователя."""
    await redis.setex(
        f"char:{user_id}",
        int(TTL_CHARACTER.total_seconds()),
        json.dumps(character, ensure_ascii=False)
    )

async def delete_user_character(user_id: int):
    """Удалить персонажа (при смене)."""
    await redis.delete(f"char:{user_id}")

# ── История диалога ───────────────────────────────────────────────────────────

async def get_chat_history(user_id: int) -> list:
    """Получить историю диалога."""
    data = await redis.get(f"history:{user_id}")
    if data:
        return json.loads(data)
    return []

async def set_chat_history(user_id: int, history: list):
    """Сохранить историю диалога. TTL обновляется при каждом сообщении."""
    await redis.setex(
        f"history:{user_id}",
        int(TTL_HISTORY.total_seconds()),
        json.dumps(history, ensure_ascii=False)
    )

async def clear_chat_history(user_id: int):
    """Сбросить историю диалога."""
    await redis.delete(f"history:{user_id}")

# ── Аналитика: сессии диалога ─────────────────────────────────────────────────

async def get_dialog_session(user_id: int) -> dict | None:
    """Получить текущую сессию диалога для аналитики.
    Возвращает {'dialog_id': str, 'turn': int} или None если сессия истекла.
    """
    data = await redis.get(f"dialog_session:{user_id}")
    if data:
        return json.loads(data)
    return None

async def set_dialog_session(user_id: int, dialog_id: str, turn: int):
    """Сохранить сессию диалога. TTL обновляется при каждом сообщении."""
    await redis.setex(
        f"dialog_session:{user_id}",
        int(TTL_DIALOG_SESSION.total_seconds()),
        json.dumps({"dialog_id": dialog_id, "turn": turn})
    )

# ── Аналитика: известные пользователи ────────────────────────────────────────

async def is_known_user(user_id: int) -> bool:
    """Проверить — писал ли пользователь боту раньше (для is_first_message)."""
    return await redis.exists(f"known_user:{user_id}") == 1

async def mark_user_known(user_id: int):
    """Пометить пользователя как известного."""
    await redis.setex(
        f"known_user:{user_id}",
        int(TTL_KNOWN_USER.total_seconds()),
        "1"
    )