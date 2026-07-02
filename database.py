import os
import asyncpg
from datetime import datetime, timezone, timedelta

DATABASE_URL = os.getenv("DATABASE_URL")

# МСК = UTC+3
MSK = timezone(timedelta(hours=3))

pool: asyncpg.Pool | None = None


async def init_db():
    """Инициализация пула соединений и создание таблиц."""
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                messages_balance INT DEFAULT 0,
                free_used_today INT DEFAULT 0,
                free_reset_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                is_banned BOOLEAN DEFAULT FALSE,
                channel_bonus_given BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        # на случай если таблица users уже была создана раньше без этой колонки
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS channel_bonus_given BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                type TEXT NOT NULL,
                messages_amount INT NOT NULL,
                stars_amount INT,
                telegram_charge_id TEXT UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                character TEXT,
                source TEXT,
                clicks INT DEFAULT 0,
                conversions INT DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_events (
                id SERIAL PRIMARY KEY,
                code TEXT NOT NULL,
                user_id BIGINT,
                event_type TEXT NOT NULL,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referral_events_code ON referral_events (code)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referral_events_user_id ON referral_events (user_id)
        """)


async def close_db():
    """Закрыть пул соединений при остановке бота."""
    global pool
    if pool:
        await pool.close()


async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> tuple:
    """Получить пользователя или создать если не существует."""
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1", user_id
        )
        if not user:
            user = await conn.fetchrow("""
                INSERT INTO users (user_id, username, first_name)
                VALUES ($1, $2, $3)
                RETURNING *
            """, user_id, username, first_name)
            return user, True  # новый пользователь
        return user, False  # уже существует


def _next_midnight_msk() -> datetime:
    """Следующая полночь по МСК."""
    now_msk = datetime.now(MSK)
    next_midnight = (now_msk + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return next_midnight


async def check_and_spend_message(user_id: int) -> str:
    """
    Проверить и списать сообщение.
    Возвращает:
      'spend_free'   — списано бесплатное
      'last_free'    — списано последнее бесплатное (3-е из 3)
      'spend_paid'   — списано платное сообщение
      'no_messages'  — нет ни бесплатных ни платных
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # блокируем строку чтобы исключить двойное списание
            user = await conn.fetchrow("""
            INSERT INTO users (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
            RETURNING *
        """, user_id)

            if user["is_banned"]:
                return "banned"

            now = datetime.now(MSK)

            # сбросить бесплатные если наступила новая МСК-дата
            free_used = user["free_used_today"]
            reset_at = user["free_reset_at"]
            if reset_at and reset_at.astimezone(MSK).date() < now.date():
                free_used = 0
                await conn.execute("""
                    UPDATE users
                    SET free_used_today = 0, free_reset_at = $1
                    WHERE user_id = $2
                """, _next_midnight_msk(), user_id)

            FREE_LIMIT = 3

            # сначала тратим бесплатные
            if free_used < FREE_LIMIT:
                new_free_used = free_used + 1
                await conn.execute("""
                    UPDATE users SET free_used_today = $1 WHERE user_id = $2
                """, new_free_used, user_id)

                if new_free_used == FREE_LIMIT:
                    return "last_free"
                return "spend_free"

            # бесплатные кончились — тратим платные
            balance = user["messages_balance"]
            if balance > 0:
                await conn.execute("""
                    UPDATE users SET messages_balance = messages_balance - 1
                    WHERE user_id = $1
                """, user_id)
                await conn.execute("""
                    INSERT INTO transactions (user_id, type, messages_amount)
                    VALUES ($1, 'spend', 1)
                """, user_id)
                return "spend_paid"

            # ничего нет
            return "no_messages"


async def add_messages(
    user_id: int,
    messages_amount: int,
    stars_amount: int,
    telegram_charge_id: str
) -> bool:
    """
    Зачислить сообщения после оплаты.
    Возвращает False если charge_id уже использован (защита от дублей).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                await conn.execute("""
                    INSERT INTO transactions (user_id, type, messages_amount, stars_amount, telegram_charge_id)
                    VALUES ($1, 'purchase', $2, $3, $4)
                """, user_id, messages_amount, stars_amount, telegram_charge_id)
            except asyncpg.UniqueViolationError:
                # дубль — уже зачислено
                return False

            await conn.execute("""
                UPDATE users SET messages_balance = messages_balance + $1
                WHERE user_id = $2
            """, messages_amount, user_id)

            return True


async def get_user_balance(user_id: int) -> dict:
    """Получить баланс и остаток бесплатных для отображения в профиле/мини-апке."""
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT messages_balance, free_used_today, free_reset_at FROM users WHERE user_id = $1",
            user_id
        )
        if not user:
            return {"messages_balance": 0, "free_left": 3, "free_total": 3}

        now = datetime.now(MSK)
        free_used = user["free_used_today"]
        reset_at = user["free_reset_at"]

        # если новый день — лимит ещё не сброшен но показываем 3
        if reset_at and reset_at.astimezone(MSK).date() < now.date():
            free_used = 0

        return {
            "messages_balance": user["messages_balance"],
            "free_left": max(0, 3 - free_used),
            "free_total": 3
        }


async def ban_user(user_id: int):
    """Забанить пользователя."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_banned = TRUE WHERE user_id = $1", user_id
        )


async def unban_user(user_id: int):
    """Разбанить пользователя."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_banned = FALSE WHERE user_id = $1", user_id
        )


async def grant_messages(user_id: int, amount: int):
    """Начислить сообщения вручную (для админки)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE users SET messages_balance = messages_balance + $1
                WHERE user_id = $2
            """, amount, user_id)
            await conn.execute("""
                INSERT INTO transactions (user_id, type, messages_amount)
                VALUES ($1, 'grant', $2)
            """, user_id, amount)

async def give_channel_bonus(user_id: int) -> bool:
    """
    Начислить бонус за подписку на канал (3 сообщения, один раз).
    Возвращает True если бонус выдан, False если уже был выдан ранее.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT channel_bonus_given FROM users WHERE user_id = $1",
                user_id
            )
            if not user or user["channel_bonus_given"]:
                return False
 
            await conn.execute("""
                UPDATE users
                SET messages_balance = messages_balance + 3,
                    channel_bonus_given = TRUE
                WHERE user_id = $1
            """, user_id)
            await conn.execute("""
                INSERT INTO transactions (user_id, type, messages_amount)
                VALUES ($1, 'channel_bonus', 3)
            """, user_id)
            return True

async def track_referral_click(code: str, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE referrals SET clicks = clicks + 1
            WHERE code = $1
        """, code)
        await conn.execute("""
            INSERT INTO referral_events (code, user_id, event_type)
            VALUES ($1, $2, 'click')
        """, code, user_id)

async def track_referral_conversion(code: str, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE referrals SET conversions = conversions + 1
            WHERE code = $1
        """, code)
        await conn.execute("""
            INSERT INTO referral_events (code, user_id, event_type)
            VALUES ($1, $2, 'conversion')
        """, code, user_id)

async def get_referral(code: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM referrals WHERE code = $1", code
        )