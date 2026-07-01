import re
import uuid
import asyncio
import logging
from datetime import datetime
from analytics_db import save_turn, mark_continued
from redis_client import (
    get_dialog_session, set_dialog_session,
    is_known_user, mark_user_known
)
logger = logging.getLogger(__name__)

# Цены GPT-4o-mini за 1 токен в USD
INPUT_TOKEN_PRICE  = 0.00000015
OUTPUT_TOKEN_PRICE = 0.0000006

def redact_pii(text: str) -> str:
    text = re.sub(r'\+?\d[\d\s\-\(\)]{7,}\d', '[phone]', text)
    text = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+', '[email]', text)
    text = re.sub(r'@\w+', '[handle]', text)
    text = re.sub(r'https?://\S+|www\.\S+', '[url]', text)
    return text

def calculate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens * INPUT_TOKEN_PRICE + output_tokens * OUTPUT_TOKEN_PRICE,
        6
    )

async def get_dialog_id(user_id: int) -> tuple[str, int]:
    """Получить или создать dialog_id и turn из Redis."""
    session = await get_dialog_session(user_id)
    if session:
        dialog_id = session["dialog_id"]
        turn = session["turn"] + 1
    else:
        dialog_id = str(uuid.uuid4())[:8]
        turn = 1

    await set_dialog_session(user_id, dialog_id, turn)
    return dialog_id, turn

def log_turn(
    user_id: int,
    language_code: str,
    user_text: str,
    bot_reply: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    model: str,
    character: str
):
    async def _run():
        try:
            dialog_id, turn = await get_dialog_id(user_id)

            # is_first_message — первое ли сообщение за всё время
            first = not await is_known_user(user_id)
            if first:
                await mark_user_known(user_id)

            # mark_continued для предыдущего хода
            if turn > 1:
                try:
                    await mark_continued(dialog_id, turn - 1)
                except Exception as e:
                    logger.exception("Ошибка mark_continued")

            redacted = redact_pii(user_text)
            timestamp = datetime.now()
            total_tokens = input_tokens + output_tokens
            tokens_cost = calculate_cost(input_tokens, output_tokens)

            await save_turn(
                dialog_id=dialog_id,
                turn=turn,
                timestamp=timestamp,
                user_id=user_id,
                language_code=language_code or "unknown",
                character=character,
                user_text=redacted,
                bot_reply=bot_reply,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                tokens_cost=tokens_cost,
                latency_ms=latency_ms,
                model=model,
                is_first_message=first
            )
        except Exception as e:
            logger.exception("Ошибка аналитики")

    asyncio.create_task(_run())