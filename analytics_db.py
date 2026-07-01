import os
import asyncpg

_pool = None

async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS analytics (
                id SERIAL PRIMARY KEY,
                dialog_id TEXT NOT NULL,
                turn INT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                user_id BIGINT NOT NULL,
                language_code TEXT,
                character TEXT,
                user_text TEXT,
                bot_reply TEXT,
                input_tokens INT,
                output_tokens INT,
                total_tokens INT,
                tokens_cost NUMERIC,
                latency_ms INT,
                model TEXT,
                is_first_message BOOLEAN DEFAULT FALSE,
                ended_after_reply INT DEFAULT 1
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analytics_dialog_id ON analytics (dialog_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analytics_user_id ON analytics (user_id)
        """)

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    return _pool

async def save_turn(
    dialog_id: str,
    turn: int,
    timestamp: str,
    user_id: int,
    language_code: str,
    character: str,
    user_text: str,
    bot_reply: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    tokens_cost: float,
    latency_ms: int,
    model: str,
    is_first_message: bool
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO analytics (
                dialog_id, turn, timestamp,
                user_id, language_code, character,
                user_text, bot_reply,
                input_tokens, output_tokens, total_tokens,
                tokens_cost, latency_ms, model, is_first_message
            ) VALUES (
                $1, $2, $3::timestamptz,
                $4, $5, $6,
                $7, $8,
                $9, $10, $11,
                $12, $13, $14, $15
            )
        """,
            dialog_id, turn, timestamp,
            user_id, language_code, character,
            user_text, bot_reply,
            input_tokens, output_tokens, total_tokens,
            tokens_cost, latency_ms, model, is_first_message
        )

async def mark_continued(dialog_id: str, turn: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE analytics
            SET ended_after_reply = 0
            WHERE dialog_id = $1 AND turn = $2
        """, dialog_id, turn)