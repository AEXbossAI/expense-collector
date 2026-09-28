from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from app.core.config import get_settings
from app.db.models import Base

config = context.config

# Логирование настраиваем ТОЛЬКО когда alembic запущен своей командой.
#
# init_db() гоняет upgrade внутри приложения, а fileConfig ставит
# logger_root на WARNING из alembic.ini — и затирает basicConfig(INFO),
# который main.py применил раньше при импорте. Итог: с коммита 703c980,
# то есть с появления alembic, все logger.info в приложении глохли.
# В `docker logs` оставались одни ошибки почты, и живость приходилось
# проверять пульсами в базе вместо лога.
#
# Признак «приложение уже настроило логи» — наличие хендлеров у корневого
# логгера. При запуске из CLI их нет, и конфиг из ini применяется как надо.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

# Пароль живёт в .env, а не в alembic.ini: ini уезжает в git.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
