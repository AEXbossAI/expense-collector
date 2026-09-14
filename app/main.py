from __future__ import annotations

import asyncio
import logging

from app.collectors import telegram
from app.core import health
from app.core.config import get_settings
from app.db.session import init_db
from app.tasks import parser, reconciler, sweeper, uploader

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

# Библиотеки, которые ставят своим логгерам DEBUG явно и заливают вывод.
# fontTools тянется через WeasyPrint при рендере HTML-тела письма в PDF:
# один такой рендер даёт полсотни строк про таблицы шрифтов.
for noisy in ("fontTools", "fontTools.subset", "fontTools.ttLib", "weasyprint",
              "dropbox", "PIL", "urllib3", "asyncio",
              "alembic.runtime.plugins", "alembic.autogenerate"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("app.main")


async def main() -> None:
    await init_db()

    from app.collectors import mail  # noqa: PLC0415

    tasks = [
        telegram.run(),   # непрерывно: апдейты Telegram живут 24 часа
        mail.run(),       # интервал настраиваемый: IMAP хранит письма сам
        uploader.run(),   # разгребает локальный буфер в Dropbox
        parser.run(),     # разбирает и раскладывает по папкам компаний
        reconciler.run(),        # суточная сверка Dropbox с базой
        reconciler.registry_loop(),  # перегенерация _реестр.csv батчем
        sweeper.run(),    # подвисшие перемещения: единственное место тихой потери
        health.watchdog_loop(),
    ]

    logger.info("Запуск %d задач", len(tasks))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен по Ctrl+C")
