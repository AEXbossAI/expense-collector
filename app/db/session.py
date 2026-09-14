from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models import Base

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,   # db-router рвёт простаивающие соединения
    pool_size=5,
    max_overflow=5,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _upgrade_sync() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


async def init_db() -> None:
    """Схему ведёт alembic, а не create_all.

    Раньше здесь был create_all, и цена этого решения материализовалась:
    добавление Source.manual потребовало ручного ALTER TYPE в проде,
    потому что create_all не меняет существующие типы и таблицы.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _upgrade_sync)
    logger.info("Схема БД приведена к head")


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
