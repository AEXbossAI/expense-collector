"""Синхронизация разложенных расходов с доской Monday.

Отдельная задача, а не часть разборщика, и это принципиально: у расхода уже
есть файл в папке компании и строка в реестре, доска — производное. Monday,
отвечающий 500, не имеет права останавливать сбор и раскладку.

Очередь `monday_pushes` устроена как `pending_uploads`: бесконечные ретраи
с экспоненциальным бэкоффом, алерт по ВОЗРАСТУ старейшей строки. Намерение
пишется до вызова, как `MoveIntent` в спеке №2 — транзакции между Monday и
Postgres нет, и без намерения падение между `create_item` и записью
`monday_item_id` тихо удваивает сумму месяца на доске.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import health
from app.core.config import get_settings
from app.db.models import (
    Document,
    MondayIntent,
    MondayPush,
    MondayTask,
    ProjectSource,
    Status,
)
from app.db.session import session_scope
from app.services import alerts, dedupe, fx, monday, monday_map, projects

logger = logging.getLogger(__name__)
settings = get_settings()


def _backoff(attempts: int) -> timedelta:
    """1с, 2, 4, 8 ... до потолка. Как в аплоадере: сдаваться нечего,
    документ уже в папке и в реестре."""
    return timedelta(seconds=min(2 ** attempts, settings.monday_backoff_cap_s))


def reconcile_from() -> date:
    """С какого месяца вообще смотрим. Август не трогаем — решение владельца,
    то же самое, что у реконсилера."""
    raw = (settings.reconcile_from or "2026-09").strip()
    year, month = raw.split("-")[:2]
    return date(int(year), int(month), 1)


# ── старт: проверки, без которых синк не имеет права работать ─────────────


async def verify_columns() -> None:
    """Колонки из MONDAY_COLUMNS обязаны существовать И быть нужного типа.

    Опечатка в id иначе вылезет только на доске — пустой ячейкой без
    объяснений, через неделю, когда никто уже не помнит про правку .env.
    Перепутанный тип хуже: значение дропдауна, записанное в status-колонку,
    Monday либо отвергнет, либо запишет мусором.
    """
    if not settings.monday_columns:
        raise RuntimeError("MONDAY_COLUMNS пуст: синку нечем адресовать колонки")

    actual = await monday.board_columns(settings.monday_board_id)

    problems: list[str] = []
    for key, column_id in sorted(settings.monday_columns.items()):
        if not column_id:
            continue          # колонку ещё не завели — поле просто не пишется
        if column_id not in actual:
            problems.append(f"{key} -> {column_id}: колонки нет на доске")
            continue
        expected = monday_map.EXPECTED_TYPES.get(key)
        if expected and actual[column_id] != expected:
            problems.append(
                f"{key} -> {column_id}: тип {actual[column_id]}, ожидался {expected}"
            )

    if problems:
        raise RuntimeError("MONDAY_COLUMNS не сходится с доской: " + "; ".join(problems))

    logger.info("Колонки доски сверены: %d штук", len(settings.monday_columns))


async def ensure_group(session: AsyncSession, doc: Document) -> str:
    """Группа месяца, при необходимости создаётся.

    Ключ группы совпадает с именем папки Dropbox (`September_2026`), чтобы
    доска и папка сверялись глазами без пересчётов.
    """
    if settings.monday_group_mode != "month":
        return settings.monday_single_group_id

    title = monday_map.group_title(doc, settings.monday_group_prefix)
    groups = await monday.board_groups(settings.monday_board_id)
    if title in groups:
        return groups[title]
    group_id = await monday.create_group(settings.monday_board_id, title)
    logger.info("Создана группа %s (%s)", title, group_id)
    return group_id


# ── наполнение очереди ────────────────────────────────────────────────────


async def _enqueue(session: AsyncSession) -> int:
    """Ставит в очередь всё, что должно быть на доске и чего там нет.

    Условия намеренно строгие: только `filed`, только представители групп
    дублей, только с нужного месяца. `ignored`, `needs_review` и
    `probable_duplicate` на доску не едут — иначе фин директор увидит
    расходы, которых, возможно, не было.
    """
    since = reconcile_from()
    queued = (
        await session.execute(select(MondayPush.document_id))
    ).scalars().all()
    queued_set = set(queued)

    candidates = (
        await session.execute(
            select(Document).where(
                Document.status == Status.filed,
                Document.merged_into_id.is_(None),
                Document.doc_date >= since,
            )
        )
    ).scalars().all()

    added = 0
    for doc in candidates:
        if doc.id in queued_set:
            continue
        # Уже на доске и с тех пор не менялся — трогать нечего.
        if doc.monday_item_id and doc.monday_synced_at and doc.monday_synced_at >= doc.updated_at:
            continue
        session.add(MondayPush(document_id=doc.id))
        added += 1

    if added:
        logger.info("В очередь на доску добавлено %d документов", added)
    return added


# ── обработка одного документа ────────────────────────────────────────────


async def _collapse_duplicates(session: AsyncSession, doc: Document) -> Document | None:
    """Один документ на расход.

    Возвращает представителя группы, либо None, если этот документ им не
    является — тогда он получает `merged_into_id` и на доску не едет вовсе.
    """
    group = await dedupe.group_of(session, doc)
    if len(group) == 1:
        return doc

    winner = dedupe.choose_representative(group)
    for other in group:
        if other.id != winner.id:
            other.merged_into_id = winner.id
            logger.info("Документ %s схлопнут в %s", other.id, winner.id)

    return winner if winner.id == doc.id else None


async def _convert(doc: Document) -> None:
    """Сумма в евро. Курс не получен — сумма остаётся пустой, и это лучше,
    чем правдоподобная выдумка."""
    if doc.amount_eur is not None and doc.fx_rate is not None:
        return
    when = doc.doc_date or doc.received_at.date()
    try:
        converted = await fx.to_eur(doc.amount, doc.currency, when)
    except fx.RateUnavailable as exc:
        doc.monday_error = f"курс не получен: {exc}"
        logger.info("Документ %s поедет без суммы: %s", doc.id, exc)
        return
    doc.amount_eur = converted.amount_eur
    doc.fx_rate = converted.rate
    doc.fx_rate_date = converted.rate_date
    doc.fx_source = converted.source


async def _read_back(session: AsyncSession, doc: Document) -> dict[str, str] | None:
    """Читает айтем с доски и забирает правки человека обратно в базу.

    Проект, изменённый руками, становится ответом человека: следующий разбор
    не будет его переигрывать, а журнал ответов получит материал для правила.

    Сумма, вендор и дата, изменённые руками, дают алерт: иначе в базе и в
    `_реестр.csv` навсегда останется неверная цифра, а на доске верная,
    и два источника правды разойдутся молча.
    """
    if not doc.monday_item_id:
        return None

    current = await monday.item_values(doc.monday_item_id)
    if not current:
        return None

    columns = settings.monday_columns
    last = doc.monday_last_written or {}

    project_col = columns.get(monday_map.PROJECT)
    if project_col:
        on_board = (current.get(project_col) or "").strip()
        if on_board and on_board != (doc.project_name or ""):
            logger.info("Проект документа %s поправлен человеком: %s", doc.id, on_board)
            doc.project_name = on_board
            doc.project_source = ProjectSource.human

    for key, label in (
        (monday_map.AMOUNT_EUR, "сумма"),
        (monday_map.VENDOR, "вендор"),
        (monday_map.DOC_DATE, "дата документа"),
    ):
        column_id = columns.get(key)
        if not column_id:
            continue
        on_board = (current.get(column_id) or "").strip()
        was_written = last.get(key)
        if on_board and was_written is not None and not monday_map._same(on_board, was_written):
            await alerts.alert(
                f"monday-drift:{doc.id}",
                f"На доске поправлена {label} документа {doc.id}: на доске «{on_board}», "
                f"в реестре «{was_written}». Реестр в Dropbox не обновится сам.",
            )

    return current


async def _push(session: AsyncSession, doc: Document) -> None:
    """Создаёт или обновляет айтем. Всё, что может упасть, падает наружу —
    очередь поставит бэкофф и повторит."""
    values = monday_map.fields(doc)
    current = await _read_back(session, doc)
    # После чтения правок значения могли поменяться (проект от человека),
    # поэтому пересобираем.
    values = monday_map.fields(doc)

    if doc.monday_item_id:
        allowed = monday_map.writable(
            values, doc.monday_last_written, current, settings.monday_columns
        )
        if not allowed:
            doc.monday_synced_at = datetime.now(timezone.utc)
            return
        payload, written = monday_map.column_values(allowed, settings.monday_columns)
        await monday.update_item(settings.monday_board_id, doc.monday_item_id, payload)
        doc.monday_last_written = {**(doc.monday_last_written or {}), **written}
        doc.monday_synced_at = datetime.now(timezone.utc)
        doc.monday_error = None
        logger.info("Айтем %s обновлён (документ %s)", doc.monday_item_id, doc.id)
        return

    group_id = await ensure_group(session, doc)
    name = monday_map.item_name(doc)
    payload, written = monday_map.column_values(values, settings.monday_columns)

    # Намерение ДО вызова: падение между create_item и записью id иначе
    # заведёт второй айтем на тот же расход при следующем проходе.
    intent = MondayIntent(document_id=doc.id, group_id=group_id, item_name=name)
    session.add(intent)
    await session.flush()

    doc_id_col = settings.monday_columns.get(monday_map.DOC_ID)
    if doc_id_col:
        # Проба восстановления: айтем мог быть создан в прошлый раз, а id
        # не записаться. Точечный лукап по Doc ID, а не скан доски.
        existing = await monday.find_by_doc_id(settings.monday_board_id, doc_id_col, doc.id)
        if existing:
            logger.info("Айтем документа %s уже есть на доске (%s), подбираю", doc.id, existing)
            doc.monday_item_id = existing
            intent.completed_at = datetime.now(timezone.utc)
            await monday.update_item(settings.monday_board_id, existing, payload)
            doc.monday_last_written = written
            doc.monday_synced_at = datetime.now(timezone.utc)
            doc.monday_error = None
            return

    item_id = await monday.create_item(settings.monday_board_id, group_id, name, payload)
    doc.monday_item_id = item_id
    doc.monday_last_written = written
    doc.monday_synced_at = datetime.now(timezone.utc)
    doc.monday_error = None
    intent.completed_at = datetime.now(timezone.utc)
    logger.info("Айтем %s создан (документ %s, группа %s)", item_id, doc.id, group_id)

    body = await _update_body(session, doc)
    if body:
        await monday.post_update(item_id, body)


async def _update_body(session: AsyncSession, doc: Document) -> str | None:
    """Что не поместилось в колонки: остальные проекты многопроектного счёта
    и вторые экземпляры того же расхода."""
    extra: list[str] = []
    if doc.task_codes and doc.project_source == ProjectSource.task_code:
        codes = [c.strip() for c in doc.task_codes.split(",") if c.strip()]
        if len(codes) > 1:
            rows = (
                await session.execute(select(MondayTask).where(MondayTask.code.in_(codes)))
            ).scalars().all()
            extra = [
                r.project_name for r in rows
                if r.project_name and r.project_monday_id != doc.project_monday_id
            ]

    merged = (
        await session.execute(select(Document).where(Document.merged_into_id == doc.id))
    ).scalars().all()
    duplicates = [f"{d.original_name} ({d.source.value})" for d in merged]

    return monday_map.update_body(doc, extra, duplicates)


# ── цикл очереди ──────────────────────────────────────────────────────────


async def _drain_once() -> int:
    now = datetime.now(timezone.utc)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MondayPush)
                .where(MondayPush.next_attempt_at <= now)
                .order_by(MondayPush.next_attempt_at)
                .limit(settings.monday_batch_size)
            )
        ).scalars().all()
        pending = [(r.id, r.document_id, r.attempts) for r in rows]

    if not pending:
        return 0

    done = 0
    for row_id, doc_id, attempts in pending:
        try:
            async with session_scope() as session:
                doc = await session.get(Document, doc_id)
                if doc is None or doc.status != Status.filed or doc.merged_into_id is not None:
                    row = await session.get(MondayPush, row_id)
                    if row is not None:
                        await session.delete(row)
                    continue

                winner = await _collapse_duplicates(session, doc)
                if winner is None:
                    row = await session.get(MondayPush, row_id)
                    if row is not None:
                        await session.delete(row)
                    logger.info("Документ %s не представитель группы, на доску не едет", doc.id)
                    continue

                await _convert(doc)
                await _push(session, doc)

                row = await session.get(MondayPush, row_id)
                if row is not None:
                    await session.delete(row)
                done += 1

        except monday.MondayRateLimited as exc:
            logger.warning("Бюджет Monday исчерпан, пауза %dс", exc.retry_after)
            await _reschedule(row_id, attempts, str(exc), delay=exc.retry_after)
            await asyncio.sleep(min(exc.retry_after, 120))
            return done
        except monday.MondayAuthBroken as exc:
            await alerts.alert(
                "monday-auth",
                f"Monday отклоняет токен: {exc}. Синк расходов встал. "
                f"Токен общий с шестью приложениями — возможно, его перевыпустили "
                f"в другом проекте. Файлы и реестр не затронуты.",
            )
            await _reschedule(row_id, attempts, str(exc))
            return done
        except Exception as exc:
            logger.exception("Не удалось синхронизировать документ %s", doc_id)
            await _reschedule(row_id, attempts, str(exc))

    if done:
        alerts.reset("monday-auth")
    return done


async def _reschedule(row_id: int, attempts: int, error: str, delay: int | None = None) -> None:
    async with session_scope() as session:
        row = await session.get(MondayPush, row_id)
        if row is None:
            return
        row.attempts = attempts + 1
        wait = timedelta(seconds=delay) if delay else _backoff(attempts + 1)
        row.next_attempt_at = datetime.now(timezone.utc) + wait
        row.last_error = error[:2000]

        doc = await session.get(Document, row.document_id)
        if doc is not None:
            doc.monday_error = error[:2000]


async def tick() -> int:
    """Один проход: наполнить очередь и разгрести её. Вынесено отдельно,
    чтобы скрипт бэкфилла гонял ровно тот же код, что и фоновая задача."""
    async with session_scope() as session:
        await _enqueue(session)
    return await _drain_once()


async def run() -> None:
    if not settings.monday_sync_enabled:
        logger.info("Синк с Monday выключен (MONDAY_SYNC_ENABLED=false)")
        return
    if not settings.monday_api_token:
        logger.error("MONDAY_API_TOKEN не задан, синк не запущен")
        return

    try:
        await verify_columns()
    except Exception as exc:
        await alerts.alert("monday-columns", f"Синк расходов не стартовал: {exc}")
        logger.error("Синк не стартовал: %s", exc)
        return

    async with session_scope() as session:
        try:
            await projects.refresh_projects(session)
        except Exception:
            logger.exception("Прогрев справочника проектов не удался, продолжаю без него")

    asyncio.create_task(health.beat_loop("monday-sync"))
    logger.info(
        "Синк с Monday запущен, доска %s, интервал %dс%s",
        settings.monday_board_id, settings.monday_sync_interval_s,
        ", СУХОЙ ПРОГОН" if settings.monday_dry_run else "",
    )

    while True:
        try:
            if settings.monday_dry_run:
                logger.info("[сухой прогон] синк ничего не пишет, используйте monday_dry_run.py")
            else:
                await tick()
                async with session_scope() as session:
                    await projects.refresh_projects(session)
        except Exception:
            logger.exception("Синк упал на цикле")
        await asyncio.sleep(settings.monday_sync_interval_s)
