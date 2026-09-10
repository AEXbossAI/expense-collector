from __future__ import annotations

import asyncio
import logging

from app.collectors import telegram
from app.core import health
from app.core.config import get_settings
from app.db.session import init_db
from app.tasks import uploader

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("app.main")


async def main() -> None:
    await init_db()

    tasks = [
        telegram.run(),   # непрерывно: апдейты Telegram живут 24 часа
        uploader.run(),   # разгребает локальный буфер в Dropbox
        health.watchdog_loop(),
    ]

    # collector-mail появится следующим коммитом. Его периодичность
    # настраиваемая вплоть до раза в месяц: IMAP хранит письма сам,
    # в отличие от Telegram.
    try:
        from app.collectors import mail  # noqa: PLC0415
    except ImportError:
        logger.info("Почтовый коллектор ещё не реализован, пропускаю")
    else:
        tasks.append(mail.run())

    logger.info("Запуск %d задач", len(tasks))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен по Ctrl+C")
