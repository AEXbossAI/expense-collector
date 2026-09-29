from __future__ import annotations

import asyncio
import logging
import mimetypes
from datetime import datetime, timezone

from sqlalchemy import select

from app.core import health, naming
from app.core.config import get_settings
from app.db.models import Document, Kind, Source, Status, SyncCursor
from app.db.session import session_scope
from app.services import registry
from app.services.dropbox_client import get_dropbox

logger = logging.getLogger(__name__)
settings = get_settings()

INTERVAL_S = 24 * 3600
SKIP_NAMES = {settings.registry_filename}

_MONTH_NUM = {m: i + 1 for i, m in enumerate(naming._MONTHS_EN)}


def _month_in_scope(folder_name: str) -> bool:
    """Папки раньше RECONCILE_FROM не трогаем совсем."""
    parts = folder_name.split("_")
    if len(parts) != 2 or parts[0] not in _MONTH_NUM or not parts[1].isdigit():
        return False
    key = f"{int(parts[1]):04d}-{_MONTH_NUM[parts[0]]:02d}"
    return key >= settings.reconcile_from


def _path_in_scope(path: str) -> bool:
    """Лежит ли путь в папке месяца, которую мы вообще смотрим."""
    return any(_month_in_scope(part) for part in path.split("/"))


async def _list_company(company: str, root: str) -> dict[str, tuple[str, str]]:
    """{content_hash: (путь, имя)} по всем папкам месяцев компании."""
    dbx = get_dropbox()
    loop = asyncio.get_running_loop()
    found: dict[str, tuple[str, str]] = {}

    try:
        years = await loop.run_in_executor(None, dbx._dbx.files_list_folder, root)
    except Exception as exc:
        logger.warning("Не открыть %s: %s", root, str(exc)[:80])
        return found

    for year in years.entries:
        if getattr(year, "size", None) is not None or not year.name.isdigit():
            continue
        try:
            months = await loop.run_in_executor(None, dbx._dbx.files_list_folder, year.path_display)
        except Exception:
            continue
        for month in months.entries:
            if getattr(month, "size", None) is not None:
                continue
            if not _month_in_scope(month.name):
                continue
            try:
                files = await loop.run_in_executor(
                    None, dbx._dbx.files_list_folder, month.path_display
                )
            except Exception:
                continue
            for entry in files.entries:
                h = getattr(entry, "content_hash", None)
                if h and entry.name not in SKIP_NAMES:
                    found[h] = (entry.path_display, entry.name)
    return found


async def reconcile_once(dry: bool = False) -> dict[str, int]:
    """Сверка Dropbox с базой.

    dry=True не пишет в базу НИЧЕГО. Это не формальность: первая версия
    навесила флаг только на создание записей, а ветки "перемещено" и
    "удалено" продолжали писать — и сухой прогон испортил четыре
    корректно разложенных документа.

    Источник правды по файлам — Dropbox, поэтому расхождения решаются
    в его пользу: человек передвинул или удалил файл, значит он прав,
    а база подстраивается. Реконсилер сам ничего не перемещает и не
    удаляет.
    """
    stats = {"новых": 0, "перемещено": 0, "удалено": 0, "снято_с_очереди": 0}

    in_dropbox: dict[str, tuple[str, str]] = {}
    for company, root in settings.dropbox_company_roots.items():
        for h, (path, name) in (await _list_company(company, root)).items():
            in_dropbox[h] = (path, name)

    async with session_scope() as session:
        docs = (await session.execute(select(Document))).scalars().all()
        by_hash = {d.content_hash: d for d in docs if d.content_hash}

        # 1. Файл есть в Dropbox, в базе нет — положил человек
        for h, (path, name) in in_dropbox.items():
            if h in by_hash:
                continue
            company = next(
                (c for c, r in settings.dropbox_company_roots.items() if path.startswith(r)),
                None,
            )
            if dry:
                stats["новых"] += 1
                logger.info("[сухой] завёл бы: %s", path)
                continue
            session.add(
                Document(
                    source=Source.manual,
                    source_key=h,
                    content_hash=h,
                    kind=Kind.file,
                    original_name=name,
                    mime_type=mimetypes.guess_type(name)[0],
                    dropbox_path=path,
                    uploaded_at=datetime.now(timezone.utc),
                    received_at=datetime.now(timezone.utc),
                    company=company,
                    status=Status.new,     # разборщик достанет сумму и дату
                    status_reason="найден реконсилером: разложен вручную",
                )
            )
            stats["новых"] += 1

        # 2-4. Расхождения по уже известным документам
        for doc in docs:
            if not doc.content_hash:
                continue
            hit = in_dropbox.get(doc.content_hash)

            if hit and doc.status == Status.needs_review:
                if dry:
                    stats["снято_с_очереди"] += 1
                    continue
                # Человек уже закрыл вопрос сам — снимаем с очереди.
                # Ровно случай Gamma.
                doc.dropbox_path = hit[0]
                doc.status = Status.filed
                doc.status_reason = "разложено вручную, вопрос снят реконсилером"
                doc.resolved_at = datetime.now(timezone.utc)
                stats["снято_с_очереди"] += 1
                continue

            if hit and doc.dropbox_path != hit[0]:
                if dry:
                    stats["перемещено"] += 1
                    logger.info("[сухой] отметил бы перемещение %s -> %s", doc.id, hit[0])
                    continue
                new_company = next(
                    (c for c, r in settings.dropbox_company_roots.items() if hit[0].startswith(r)),
                    doc.company,
                )
                logger.info("Документ %s передвинут человеком: %s -> %s",
                            doc.id, doc.dropbox_path, hit[0])
                doc.dropbox_path = hit[0]
                doc.company = new_company
                stats["перемещено"] += 1
                continue

            if (
                not hit
                and doc.status == Status.filed
                and doc.dropbox_path
                and not doc.dropbox_path.startswith(settings.dropbox_inbox_path)
                # Документ вне области сверки отсутствует в выборке не потому,
                # что его удалили, а потому что мы туда не смотрели. Без этой
                # проверки сухой прогон пометил бы удалёнными 4 корректно
                # разложенных документа за август.
                and _path_in_scope(doc.dropbox_path)
            ):
                stats["удалено"] += 1
                if dry:
                    logger.info("[сухой] пометил бы удалённым %s (%s)", doc.id, doc.dropbox_path)
                    continue
                doc.status = Status.ignored
                doc.status_reason = "удалён человеком из папки компании"

    return stats


CURSOR_NAME = "reconcile_run"


async def _last_run() -> datetime | None:
    async with session_scope() as session:
        row = (
            await session.execute(select(SyncCursor).where(SyncCursor.name == CURSOR_NAME))
        ).scalar_one_or_none()
    if row is None or not row.value:
        return None
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


async def _remember_run(when: datetime) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(select(SyncCursor).where(SyncCursor.name == CURSOR_NAME))
        ).scalar_one_or_none()
        if row is None:
            session.add(SyncCursor(name=CURSOR_NAME, value=when.isoformat()))
        else:
            row.value = when.isoformat()


def _due_in(last: datetime | None, now: datetime) -> float:
    """Сколько ещё ждать до сверки, зная время прошлой.

    Раньше цикл просто спал сутки перед первой сверкой, а время прогона
    нигде не хранилось. Поэтому КАЖДЫЙ перезапуск контейнера обнулял отсчёт:
    28.09.2026 контейнер пересобирали восемь раз, и сверка не случилась ни
    разу — последняя была 25-го. При ежедневных выкатах она не случилась бы
    никогда, а это тихая потеря: файлы, которые фин директор кладёт в папки
    руками, попадают в систему только через реконсилер.
    """
    if last is None:
        return 0.0
    return max(0.0, INTERVAL_S - (now - last).total_seconds())


async def _seconds_until_due() -> float:
    return _due_in(await _last_run(), datetime.now(timezone.utc))


async def run() -> None:
    asyncio.create_task(health.beat_loop("reconciler"))
    logger.info("Реконсилер запущен, интервал %d ч", INTERVAL_S // 3600)
    while True:
        wait = await _seconds_until_due()
        if wait:
            logger.info("Реконсилер: следующая сверка через %d мин", int(wait // 60))
            await asyncio.sleep(wait)
        try:
            stats = await reconcile_once()
            await _remember_run(datetime.now(timezone.utc))
            if any(stats.values()):
                logger.info("Реконсилер: %s", stats)
            else:
                logger.info("Реконсилер: расхождений нет")
        except Exception:
            logger.exception("Реконсилер упал")
            # Время прогона не отмечаем: упавшая сверка не считается
            # состоявшейся, иначе одна ошибка отложит следующую на сутки.
            await asyncio.sleep(3600)


# ── задача реестра ────────────────────────────────────────────────────────


async def _dirty_pairs(session, since: datetime) -> set[tuple[str, str]]:
    docs = (
        await session.execute(
            select(Document).where(
                Document.status == Status.filed,
                Document.updated_at > since,
            )
        )
    ).scalars().all()
    pairs = set()
    for doc in docs:
        if doc.company and doc.doc_date:
            month = naming.month_folder(datetime(doc.doc_date.year, doc.doc_date.month, 1))
            pairs.add((doc.company, month))
    return pairs


async def registry_loop() -> None:
    """Перегенерация реестров батчем.

    Дебаунс обязателен: у фин директора стоит десктопный клиент Dropbox,
    и перегенерация на каждый файл при открытом реестре плодит
    conflicted copy.
    """
    asyncio.create_task(health.beat_loop("registry"))
    logger.info("Реестр: перегенерация раз в %dс", settings.registry_debounce_s)

    while True:
        await asyncio.sleep(settings.registry_debounce_s)
        try:
            async with session_scope() as session:
                row = (
                    await session.execute(
                        select(SyncCursor).where(SyncCursor.name == "registry_run")
                    )
                ).scalar_one_or_none()
                since = (
                    datetime.fromisoformat(row.value)
                    if row and row.value
                    else datetime(2000, 1, 1, tzinfo=timezone.utc)
                )
                pairs = await _dirty_pairs(session, since)
                for company, month in pairs:
                    await registry.regenerate(session, company, month)
                stamp = datetime.now(timezone.utc).isoformat()
                if row is None:
                    session.add(SyncCursor(name="registry_run", value=stamp))
                else:
                    row.value = stamp
        except Exception:
            logger.exception("Перегенерация реестра упала")
