from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import Heartbeat, PendingUpload
from app.db.session import session_scope
from app.services import alerts

logger = logging.getLogger(__name__)
settings = get_settings()

BEAT_INTERVAL_S = 60
CHECK_INTERVAL_S = 300


async def beat(component: str) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Heartbeat).where(Heartbeat.component == component)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(Heartbeat(component=component))
        else:
            row.beat_at = datetime.now(timezone.utc)


async def beat_loop(component: str) -> None:
    while True:
        try:
            await beat(component)
        except Exception:
            logger.exception("Heartbeat %s не записан", component)
        await asyncio.sleep(BEAT_INTERVAL_S)


async def _check_components(now: datetime, deadline: timedelta) -> None:
    async with session_scope() as session:
        rows = (await session.execute(select(Heartbeat))).scalars().all()

    for row in rows:
        age = now - row.beat_at
        if age > deadline:
            # Для collector-tg молчание опаснее, чем для остальных:
            # Telegram хранит непрочитанные апдейты 24 часа, дальше
            # сообщения потеряны безвозвратно и восстановить их нечем.
            extra = (
                " Апдейты Telegram живут 24 часа, после этого данные потеряны."
                if row.component == "collector-tg"
                else ""
            )
            await alerts.alert(
                f"stale:{row.component}",
                f"{row.component} молчит {int(age.total_seconds() // 60)} мин.{extra}",
            )
        else:
            alerts.reset(f"stale:{row.component}")


async def _check_upload_queue(now: datetime, deadline: timedelta) -> None:
    """Алерт по ВОЗРАСТУ очереди, а не по её размеру. Места на диске много,
    а вот час непрерывных ретраев означает протухший токен или смену прав,
    и это чинится руками, а не ожиданием."""
    async with session_scope() as session:
        oldest = (
            await session.execute(select(func.min(PendingUpload.created_at)))
        ).scalar_one_or_none()
        depth = (
            await session.execute(select(func.count(PendingUpload.id)))
        ).scalar_one()

    if oldest and now - oldest > deadline:
        await alerts.alert(
            "upload-stuck",
            f"Очередь выгрузки в Dropbox не движется {int((now - oldest).total_seconds() // 60)} мин, "
            f"файлов в буфере: {depth}. Проверьте refresh token и права приложения.",
        )
    elif not oldest:
        alerts.reset("upload-stuck")


async def watchdog_loop() -> None:
    deadline = timedelta(seconds=settings.heartbeat_alert_after_s)
    while True:
        await asyncio.sleep(CHECK_INTERVAL_S)
        now = datetime.now(timezone.utc)
        try:
            await _check_components(now, deadline)
            await _check_upload_queue(now, deadline)
        except Exception:
            logger.exception("Watchdog упал на проверке")
