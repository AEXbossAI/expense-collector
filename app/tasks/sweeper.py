from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core import health
from app.db.models import Document, MoveIntent, Status
from app.db.session import session_scope
from app.services import alerts
from app.services.dropbox_client import get_dropbox

logger = logging.getLogger(__name__)

INTERVAL_S = 600
# Намерение, не закрытое за это время, означает сбой между операцией
# в Dropbox и подтверждением в базе. Живой move укладывается в секунды.
STALE_AFTER = timedelta(minutes=10)


async def _resolve(intent: MoveIntent) -> str:
    """Чем на самом деле кончилось подвисшее перемещение.

    Транзакции между Dropbox и базой нет, поэтому после сбоя возможны
    два состояния: файл уже в цели (move прошёл, подтверждение — нет)
    либо ещё в источнике (move не прошёл). Различаем по факту.
    """
    dbx = get_dropbox()
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(None, dbx._dbx.files_get_metadata, intent.to_path)
        return "в цели"
    except Exception:
        pass

    try:
        await loop.run_in_executor(None, dbx._dbx.files_get_metadata, intent.from_path)
        return "в источнике"
    except Exception:
        return "нигде"


async def sweep_once() -> dict[str, int]:
    stats = {"закрыто": 0, "переделано": 0, "потеряно": 0}
    deadline = datetime.now(timezone.utc) - STALE_AFTER

    async with session_scope() as session:
        stale = (
            await session.execute(
                select(MoveIntent).where(
                    MoveIntent.completed_at.is_(None),
                    MoveIntent.created_at < deadline,
                )
            )
        ).scalars().all()

        for intent in stale:
            where = await _resolve(intent)
            doc = await session.get(Document, intent.document_id)

            if where == "в цели":
                # Перемещение прошло, подтверждение не записалось.
                # Доводим до конца, а не переделываем.
                if doc is not None:
                    doc.dropbox_path = intent.to_path
                    if doc.status != Status.filed:
                        doc.status = Status.filed
                        doc.resolved_at = datetime.now(timezone.utc)
                intent.completed_at = datetime.now(timezone.utc)
                stats["закрыто"] += 1
                logger.info("Подвисшее намерение %s: файл в цели, запись довершена", intent.id)

            elif where == "в источнике":
                # Move не прошёл. Возвращаем документ на раскладку, а не
                # трогаем файл сами: перекладывать будет разборщик.
                if doc is not None and doc.status == Status.filed:
                    doc.status = Status.new
                    doc.status_reason = "перемещение не завершилось, документ вернулся на раскладку"
                intent.completed_at = datetime.now(timezone.utc)
                intent.attempts += 1
                stats["переделано"] += 1
                logger.warning("Подвисшее намерение %s: файл в источнике, повтор", intent.id)

            else:
                # Файла нет ни там, ни там. Сами ничего не восстанавливаем:
                # это ровно тот случай, когда нужен человек.
                intent.attempts += 1
                stats["потеряно"] += 1
                await alerts.alert(
                    f"move-lost:{intent.id}",
                    f"Документ {intent.document_id} не найден ни в {intent.from_path}, "
                    f"ни в {intent.to_path}. Нужна проверка вручную.",
                )

    return stats


async def run() -> None:
    asyncio.create_task(health.beat_loop("sweeper"))
    logger.info("Sweeper запущен, интервал %dс, порог %s", INTERVAL_S, STALE_AFTER)
    while True:
        await asyncio.sleep(INTERVAL_S)
        try:
            stats = await sweep_once()
            if any(stats.values()):
                logger.info("Sweeper: %s", stats)
        except Exception:
            logger.exception("Sweeper упал")
