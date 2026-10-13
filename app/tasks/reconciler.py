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
from app.services import alerts, registry
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


DELETED_REASON = "удалён человеком из папки компании"

# Больше стольких «удалений» за одну сверку — почти наверняка сбой выборки,
# а не человек. 10.10.2026 сверка не видела подпапок и сочла удалёнными
# 51 документ разом; реестр сентября просел с 86 строк до 42.
MAX_DELETIONS = 10


def _collect(entries) -> dict[str, list[tuple[str, str]]]:
    """{content_hash: [(путь, имя), ...]} по файлам месяца.

    Список, а не одно значение: одинаковые копии (`x (1).pdf`, `x (2).pdf`)
    дают один хеш. Словарь с одним значением оставлял случайную копию,
    и документ каждый день «перемещался» между ними — 9.10.2026 таких
    перемещений было 91.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    for entry in entries:
        h = getattr(entry, "content_hash", None)
        if h and entry.name not in SKIP_NAMES:
            found.setdefault(h, []).append((entry.path_display, entry.name))
    return found


def _pick(current: str | None, copies: list[tuple[str, str]]) -> tuple[str, str]:
    """Какую из одинаковых копий считать файлом документа.

    Текущий путь, если он среди копий: документ никуда не двигался.
    Иначе первая по алфавиту — детерминированно, чтобы следующая
    сверка выбрала ту же.
    """
    if current:
        for copy in copies:
            if copy[0].lower() == current.lower():
                return copy
    return sorted(copies)[0]


def _company_of(path: str, fallback: str | None) -> str | None:
    return next(
        (c for c, r in settings.dropbox_company_roots.items() if path.startswith(r)),
        fallback,
    )


async def _list_company(company: str, root: str) -> dict[str, list[tuple[str, str]]] | None:
    """{content_hash: [(путь, имя), ...]} по всем папкам месяцев компании,
    включая подпапки внутри месяца. None — папку компании открыть не
    удалось, и об её файлах сверка не знает ничего."""
    dbx = get_dropbox()
    loop = asyncio.get_running_loop()
    found: dict[str, list[tuple[str, str]]] = {}

    try:
        years = await loop.run_in_executor(None, dbx._dbx.files_list_folder, root)
    except Exception as exc:
        logger.warning("Не открыть %s: %s", root, str(exc)[:80])
        return None

    for year in years.entries:
        if getattr(year, "size", None) is not None or not year.name.isdigit():
            continue
        # Падение листинга года или месяца роняет всю сверку: молча
        # пропущенная папка выглядит как «все её файлы удалены».
        months = await loop.run_in_executor(None, dbx._dbx.files_list_folder, year.path_display)
        for month in months.entries:
            if getattr(month, "size", None) is not None:
                continue
            if not _month_in_scope(month.name):
                continue
            files = await dbx.list_files(month.path_display)
            for h, copies in _collect(files).items():
                found.setdefault(h, []).extend(copies)
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
    stats = {"новых": 0, "перемещено": 0, "удалено": 0, "снято_с_очереди": 0,
             "восстановлено": 0}

    in_dropbox: dict[str, list[tuple[str, str]]] = {}
    unlisted_roots: list[str] = []
    for company, root in settings.dropbox_company_roots.items():
        listed = await _list_company(company, root)
        if listed is None:
            unlisted_roots.append(root)
            continue
        for h, copies in listed.items():
            in_dropbox.setdefault(h, []).extend(copies)

    async with session_scope() as session:
        docs = (await session.execute(select(Document))).scalars().all()
        by_hash = {d.content_hash: d for d in docs if d.content_hash}
        # Хеши, у которых уже есть живой документ. Восстанавливать второй
        # на тот же файл нельзя — получится два расхода из одного.
        active = {d.content_hash for d in docs
                  if d.content_hash and d.status != Status.ignored}

        # 1. Файл есть в Dropbox, в базе нет — положил человек
        for h, copies in in_dropbox.items():
            if h in by_hash:
                continue
            path, name = _pick(None, copies)
            company = _company_of(path, None)
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

        # 2-5. Расхождения по уже известным документам
        to_delete: list[Document] = []
        for doc in docs:
            if not doc.content_hash:
                continue
            copies = in_dropbox.get(doc.content_hash)
            hit = _pick(doc.dropbox_path, copies) if copies else None

            if doc.status == Status.ignored:
                # Отброшенный документ за файлом не следует: его путь ни
                # на что не влияет, а указывать на чужой живой файл опасно —
                # удаление «лишнего экземпляра» снесло бы его.
                #
                # Исключение — наша же ошибочная пометка «удалён»: файл
                # нашёлся, значит его не удаляли, а переложили.
                if (
                    hit
                    and doc.status_reason == DELETED_REASON
                    and doc.content_hash not in active
                ):
                    stats["восстановлено"] += 1
                    if dry:
                        logger.info("[сухой] восстановил бы %s -> %s", doc.id, hit[0])
                        continue
                    logger.info("Документ %s нашёлся, пометка «удалён» снята: %s",
                                doc.id, hit[0])
                    doc.dropbox_path = hit[0]
                    doc.company = _company_of(hit[0], doc.company)
                    doc.status = Status.filed
                    doc.status_reason = "восстановлен сверкой: файл переложен человеком"
                    active.add(doc.content_hash)
                continue

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
                new_company = _company_of(hit[0], doc.company)
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
                and not any(doc.dropbox_path.startswith(r) for r in unlisted_roots)
            ):
                to_delete.append(doc)

        stats["удалено"] = len(to_delete)
        if dry:
            for doc in to_delete:
                logger.info("[сухой] пометил бы удалённым %s (%s)", doc.id, doc.dropbox_path)
        elif _deletions_suspicious(len(to_delete)):
            # Перемещения и восстановления выше применяются: они опираются
            # на найденные файлы. Не применяются только удаления — они
            # опираются на НЕнайденные, а выборка могла быть неполной.
            logger.error("Сверка сочла удалёнными %d документов, пометка не применена: %s",
                         len(to_delete), [d.id for d in to_delete])
            await alerts.alert(
                "reconcile-mass-delete",
                f"Сверка не нашла в Dropbox {len(to_delete)} разложенных документов "
                f"(порог {MAX_DELETIONS}). Удалёнными их НЕ пометила: похоже на сбой "
                f"выборки, а не на ручное удаление. Документы: "
                f"{', '.join(str(d.id) for d in to_delete[:30])}",
            )
        else:
            for doc in to_delete:
                doc.status = Status.ignored
                doc.status_reason = DELETED_REASON

    return stats


def _deletions_suspicious(count: int) -> bool:
    return count > MAX_DELETIONS


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
