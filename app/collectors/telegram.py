from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from sqlalchemy import select

from app.core import health, naming
from app.core.config import get_settings
from app.db.models import Document, Kind, PendingUpload, Source, Status
from app.db.session import session_scope
from app.services import buffer

logger = logging.getLogger(__name__)
settings = get_settings()

router = Router()

# Альбом приходит несколькими апдейтами, подпись есть только у первого.
# Копим по media_group_id и разбираем группу целиком по таймауту.
_groups: dict[str, list[Message]] = {}
_group_tasks: dict[str, asyncio.Task] = {}


# ── извлечение файла из сообщения ─────────────────────────────────────────


def _extract(message: Message) -> tuple[str, str, str | None] | None:
    """(file_id, file_unique_id, имя файла) либо None, если файла нет."""
    if message.photo:
        # Telegram отдаёт лесенку размеров, последний — максимальный.
        # Фото, отправленное "как фото", уже сжато, и для OCR это хуже,
        # чем то же самое, присланное документом.
        photo = message.photo[-1]
        return photo.file_id, photo.file_unique_id, f"{photo.file_unique_id}.jpg"
    if message.document:
        doc = message.document
        return doc.file_id, doc.file_unique_id, doc.file_name or f"{doc.file_unique_id}.bin"
    return None


async def _already_seen(session, source_key: str, original_name: str) -> bool:
    stmt = select(Document.id).where(
        Document.source == Source.telegram,
        Document.source_key == source_key,
        Document.original_name == original_name,
    )
    return (await session.execute(stmt)).first() is not None


# ── запись документа ──────────────────────────────────────────────────────


async def _store_file(bot: Bot, message: Message, caption: str | None) -> None:
    extracted = _extract(message)
    if extracted is None:
        return
    file_id, file_unique_id, original_name = extracted
    original_name = original_name or "file"
    received_at = message.date or datetime.now(timezone.utc)

    async with session_scope() as session:
        # Ключ — file_unique_id, а не message_id: то же фото, отправленное
        # повторно, получит новый message_id, но тот же file_unique_id.
        if await _already_seen(session, file_unique_id, original_name):
            logger.info("Пропуск дубля по file_unique_id: %s", file_unique_id)
            return

        try:
            data = (await bot.download(file_id)).read()
        except TelegramBadRequest as exc:
            # Bot API не отдаёт файлы больше 20 МБ. Документ не теряем:
            # пишем строку, человек достанет файл руками.
            logger.warning("Не скачать %s: %s", file_unique_id, exc)
            session.add(
                Document(
                    source=Source.telegram,
                    source_key=file_unique_id,
                    kind=Kind.file,
                    original_name=original_name,
                    raw_caption=caption,
                    sender=str(message.from_user.id) if message.from_user else None,
                    received_at=received_at,
                    company=settings.telegram_company,
                    status=Status.needs_review,
                    status_reason=f"файл не скачан из Telegram: {exc}",
                )
            )
            return

        # Порядок критичен: байты на диск → строка в БД → и только потом
        # aiogram подтвердит offset. Падение на любом шаге даёт повтор.
        local = buffer.write("telegram", original_name, data)

        doc = Document(
            source=Source.telegram,
            source_key=file_unique_id,
            kind=Kind.file,
            original_name=buffer.safe_name(original_name),
            local_path=str(local),
            size_bytes=len(data),
            raw_caption=caption,
            sender=str(message.from_user.id) if message.from_user else None,
            received_at=received_at,
            # Расходы из этого чата относятся ровно к одной компании,
            # разбирать её из документа не нужно.
            company=settings.telegram_company,
            status=Status.new,
        )
        session.add(doc)
        await session.flush()

        session.add(
            PendingUpload(
                document_id=doc.id,
                local_path=str(local),
                target_path=naming.inbox_path(received_at, f"{doc.id}_{doc.original_name}"),
            )
        )
        logger.info("Телеграм: документ %s (%s, %d байт)", doc.id, original_name, len(data))


async def _store_text(message: Message) -> None:
    """Сообщение без файла. Может быть расходом без документа, а может
    быть обычным обсуждением в чате. Коллектор не различает: решение
    принимает разборщик из спека №2."""
    text = (message.text or message.caption or "").strip()
    if not text:
        return

    async with session_scope() as session:
        key = str(message.message_id)
        if await _already_seen(session, key, ""):
            return
        session.add(
            Document(
                source=Source.telegram,
                source_key=key,
                kind=Kind.text_only,
                original_name="",
                raw_caption=text,
                sender=str(message.from_user.id) if message.from_user else None,
                received_at=message.date or datetime.now(timezone.utc),
                company=settings.telegram_company,
                status=Status.new,
            )
        )


# ── обработка альбомов ────────────────────────────────────────────────────


async def _flush_group(bot: Bot, group_id: str) -> None:
    await asyncio.sleep(settings.telegram_media_group_window_s)
    messages = _groups.pop(group_id, [])
    _group_tasks.pop(group_id, None)
    if not messages:
        return
    # Подпись есть только у первого сообщения альбома, применяем ко всем.
    caption = next((m.caption for m in messages if m.caption), None)
    for message in messages:
        try:
            await _store_file(bot, message, caption)
        except Exception:
            logger.exception("Альбом %s: сбой на сообщении %s", group_id, message.message_id)


# ── хендлеры ──────────────────────────────────────────────────────────────
#
# Ни один хендлер не имеет права бросить исключение наружу: aiogram двигает
# offset после обработки батча, и упавший апдейт будет потерян навсегда
# (Telegram хранит непрочитанное 24 часа и историю чата не отдаёт).


def _mine(message: Message) -> bool:
    return not settings.telegram_chat_id or message.chat.id == settings.telegram_chat_id


@router.message(F.photo | F.document)
async def on_file(message: Message, bot: Bot) -> None:
    try:
        if not _mine(message):
            return
        if message.media_group_id:
            _groups.setdefault(message.media_group_id, []).append(message)
            if message.media_group_id not in _group_tasks:
                _group_tasks[message.media_group_id] = asyncio.create_task(
                    _flush_group(bot, message.media_group_id)
                )
            return
        await _store_file(bot, message, message.caption)
    except Exception:
        logger.exception("Сбой на сообщении %s", message.message_id)


@router.message(F.text)
async def on_text(message: Message) -> None:
    try:
        if _mine(message):
            await _store_text(message)
    except Exception:
        logger.exception("Сбой на текстовом сообщении %s", message.message_id)


@router.edited_message()
async def on_edited(message: Message) -> None:
    """Подпись правят после отправки. Без этого хендлера сумма, дописанная
    вторым заходом, до разборщика не доедет."""
    try:
        if not _mine(message):
            return
        extracted = _extract(message)
        key = extracted[1] if extracted else str(message.message_id)
        new_caption = message.caption or message.text
        if not new_caption:
            return
        async with session_scope() as session:
            stmt = select(Document).where(
                Document.source == Source.telegram, Document.source_key == key
            )
            for doc in (await session.execute(stmt)).scalars():
                doc.raw_caption = new_caption
                logger.info("Обновлена подпись документа %s", doc.id)
    except Exception:
        logger.exception("Сбой на правке сообщения %s", message.message_id)


# ── точка входа задачи ────────────────────────────────────────────────────


async def run() -> None:
    if settings.telegram_bot_token == "changeme":
        logger.error("TELEGRAM_BOT_TOKEN не задан, коллектор телеграма не запущен")
        return

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    if me.can_read_all_group_messages is False:
        # Privacy mode включён: в группе бот увидит только команды и реплаи
        # на себя. Молчаливый режим, при котором всё "работает", но данных нет.
        logger.error(
            "У бота @%s включён privacy mode. Отключите: @BotFather -> /setprivacy "
            "-> Disable, затем переподключите бота к чату.",
            me.username,
        )

    asyncio.create_task(health.beat_loop("collector-tg"))
    logger.info("Телеграм-коллектор запущен как @%s", me.username)
    await dp.start_polling(bot, allowed_updates=["message", "edited_message"])
