from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.core import health
from app.core.config import get_settings
from app.db.models import SyncCursor
from app.db.session import session_scope
from app.services import review

logger = logging.getLogger(__name__)
settings = get_settings()


async def finance_chat_id() -> int | None:
    """chat_id фин директора узнаётся в рантайме по её /start, а не
    прописывается в конфиг: требовать ради этого правки .env и
    перезапуска контейнера незачем."""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(SyncCursor).where(SyncCursor.name == "finance_chat")
            )
        ).scalar_one_or_none()
    if not row or not row.value:
        return None
    try:
        return int(row.value.split("|")[0])
    except ValueError:
        return None


def keyboard(item: review.QueueItem) -> InlineKeyboardMarkup:
    if item.kind == "duplicate":
        buttons = [
            InlineKeyboardButton(text="Один расход", callback_data=f"dup:same:{item.document_ids[0]}"),
            InlineKeyboardButton(text="Разные", callback_data=f"dup:diff:{item.document_ids[0]}"),
        ]
    else:
        buttons = [
            InlineKeyboardButton(text="Эстонская", callback_data=f"co:orlanda_engineering:{item.document_ids[0]}"),
            InlineKeyboardButton(text="Израильская", callback_data=f"co:orlanda_israel:{item.document_ids[0]}"),
            InlineKeyboardButton(text="Не распределять", callback_data=f"co:skip:{item.document_ids[0]}"),
        ]
    # Кнопки в столбик: в мобильном телеграме три кнопки в строку
    # обрезаются до нечитаемого.
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


async def send_digest(bot: Bot, *, force: bool = False) -> int:
    """Возвращает число отправленных вопросов. Пустая очередь — ноль
    сообщений: дайджест "всё в порядке" читать перестают быстрее всего."""
    chat_id = await finance_chat_id()
    if not chat_id:
        logger.warning("Фин директор не зарегистрирована, дайджест некуда слать")
        return 0

    async with session_scope() as session:
        items = await review.build(session)

    if not items:
        if force:
            await bot.send_message(chat_id, "Вопросов нет, всё разложено.")
        return 0

    await bot.send_message(chat_id, review.render(items), parse_mode="HTML")
    for n, item in enumerate(items, 1):
        amount = f"{item.total:.2f} {item.currency or ''}".strip()
        await bot.send_message(
            chat_id,
            f"<b>{n}. {item.title}</b> — {amount}",
            reply_markup=keyboard(item),
            parse_mode="HTML",
        )
    logger.info("Дайджест отправлен: %d вопросов", len(items))
    return len(items)


def _seconds_until_hour(hour: int) -> float:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run(bot: Bot) -> None:
    asyncio.create_task(health.beat_loop("digest"))
    logger.info("Дайджест запущен, час отправки %d (%s)", settings.digest_hour, settings.timezone)
    while True:
        await asyncio.sleep(_seconds_until_hour(settings.digest_hour))
        try:
            await send_digest(bot)
        except Exception:
            logger.exception("Дайджест не отправлен")
        await asyncio.sleep(60)  # чтобы не сработать дважды в тот же час
