from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core import health
from app.core.config import get_settings
from app.db.models import Document, PendingUpload
from app.db.session import session_scope
from app.services import alerts, buffer
from app.services.dropbox_client import DropboxAuthBroken, get_dropbox

logger = logging.getLogger(__name__)
settings = get_settings()

BATCH = 20


def _backoff(attempts: int) -> timedelta:
    """1с, 2, 4, 8 ... до потолка. Попытки не ограничены: Dropbox лежит
    редко, но долго, и сдаваться тут нечего — файл уже в буфере."""
    return timedelta(seconds=min(2 ** attempts, settings.uploader_backoff_cap_s))


async def _drain_once() -> None:
    now = datetime.now(timezone.utc)

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(PendingUpload)
                    .where(PendingUpload.next_attempt_at <= now)
                    .order_by(PendingUpload.next_attempt_at)
                    .limit(BATCH)
                )
            )
            .scalars()
            .all()
        )
        pending = [(r.id, r.document_id, r.local_path, r.target_path, r.attempts) for r in rows]

    if not pending:
        return

    dbx = get_dropbox()

    for row_id, doc_id, local_path, target_path, attempts in pending:
        try:
            content_hash = await dbx.upload(local_path, target_path)
        except DropboxAuthBroken as exc:
            # Ретраить бессмысленно: нужен человек. Бэкофф всё равно ставим,
            # чтобы не молотить API, но алерт уходит немедленно.
            await alerts.alert(
                "dropbox-auth",
                f"Dropbox отклоняет авторизацию: {exc}. Очередь выгрузки встала, "
                f"файлы в буфере целы. Нужен новый refresh token.",
            )
            await _reschedule(row_id, attempts, str(exc))
            return
        except Exception as exc:
            logger.warning("Выгрузка %s не удалась (попытка %d): %s", target_path, attempts + 1, exc)
            await _reschedule(row_id, attempts, str(exc))
            continue

        async with session_scope() as session:
            doc = await session.get(Document, doc_id)
            if doc is not None:
                doc.dropbox_path = target_path
                doc.content_hash = content_hash
                doc.uploaded_at = datetime.now(timezone.utc)
                doc.local_path = None
            row = await session.get(PendingUpload, row_id)
            if row is not None:
                await session.delete(row)

        # Файл удаляется из буфера ТОЛЬКО после успеха и проставленного
        # uploaded_at: иначе сбой между шагами оставит документ без байтов.
        buffer.discard(local_path)
        alerts.reset("dropbox-auth")
        logger.info("Выгружен документ %s -> %s", doc_id, target_path)


async def _reschedule(row_id: int, attempts: int, error: str) -> None:
    async with session_scope() as session:
        row = await session.get(PendingUpload, row_id)
        if row is None:
            return
        row.attempts = attempts + 1
        row.next_attempt_at = datetime.now(timezone.utc) + _backoff(attempts + 1)
        row.last_error = error[:2000]


async def run() -> None:
    asyncio.create_task(health.beat_loop("uploader"))
    logger.info("Аплоадер запущен, интервал %dс", settings.uploader_interval_s)
    while True:
        try:
            await _drain_once()
        except Exception:
            logger.exception("Аплоадер упал на цикле")
        await asyncio.sleep(settings.uploader_interval_s)
