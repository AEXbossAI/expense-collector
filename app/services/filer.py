from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import naming
from app.core.config import get_settings
from app.db.models import Document, MonthState, MoveIntent, Source
from app.services import buffer
from app.services.dropbox_client import SourceMissing, get_dropbox

logger = logging.getLogger(__name__)
settings = get_settings()


async def is_month_closed(session: AsyncSession, company: str, month: str) -> bool:
    stmt = select(MonthState).where(
        MonthState.company == company, MonthState.month == month
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return bool(row and row.closed_at)


def target_filename(doc: Document, vendor: str | None, amount: float | None,
                    currency: str | None, doc_date: date | None) -> str:
    """`YYYY-MM-DD_вендор_сумма-валюта_shortid.pdf`

    Имя самодостаточно: папка и реестр должны читаться человеком без
    доступа к базе. Неизвестные поля заменяются заглушками, а не
    выкидываются — иначе позиции в имени разъезжаются.
    """
    ext = ""
    if "." in doc.original_name:
        ext = "." + doc.original_name.rsplit(".", 1)[1].lower()

    parts = [
        doc_date.isoformat() if doc_date else "nodate",
        buffer.safe_name(vendor, fallback="novendor").rsplit(".", 1)[0][:40] if vendor else "novendor",
        f"{amount:.2f}-{currency.upper()}" if amount is not None and currency else "noamount",
        (doc.content_hash or str(doc.id))[:8],
    ]
    return buffer.safe_name("_".join(parts) + ext, fallback=f"doc{doc.id}{ext}")


async def file_document(
    session: AsyncSession,
    doc: Document,
    *,
    company: str,
    vendor: str | None,
    amount: float | None,
    currency: str | None,
    doc_date: date | None,
    folder: str | None = None,
) -> tuple[str, str | None]:
    """Перекладывает документ в папку компании и месяца.

    `folder` задаёт папку явно — для подмены экземпляра дубля: лучший
    файл встаёт туда, где лежал основной, в том числе в подпапку банка,
    куда его разложил фин директор.

    Если файла по записанному пути нет, он ищется по содержимому. Нашёлся
    в папке той же компании — его туда положил человек, и файл остаётся
    на месте. Не нашёлся нигде — `SourceMissing`.

    Возвращает (новый путь, пометка про закрытый месяц).
    """
    dbx = get_dropbox()
    closed_note: str | None = None

    # Файл уже в папке компании — туда его положил человек: система кладёт
    # во входящие и сама из папок компании ничего не раскладывает. Тот же
    # принцип, что у разборщика для ручных документов. 13.10.2026 подмена
    # дубля без этой проверки вынесла два файла из подпапок банков в корень
    # месяца и переименовала.
    source = doc.dropbox_path
    if source and in_company_folder(source, company) and await dbx.exists(source):
        logger.info("Документ %s уже в папке компании, остаётся на месте: %s", doc.id, source)
        return source, None

    if folder is None:
        # Поездка — в месяц вылета или заезда, даже будущий: папка месяца
        # создаётся ниже, если её ещё нет.
        effective = naming.travel_date(doc) or doc_date or doc.received_at.date()
        month = naming.month_folder(datetime(effective.year, effective.month, 1))
        if await is_month_closed(session, company, month):
            # Молча дописывать в сданный отчёт нельзя: доверие к реестру
            # кончается на первом же случае. Кладём в текущий месяц с пометкой.
            closed_note = month
            effective = datetime.now(timezone.utc).date()
            logger.info("Месяц %s закрыт, документ %s уходит в текущий", month, doc.id)
        folder = naming.company_path(company, datetime(effective.year, effective.month, 1))
    await dbx.ensure_folder(folder)

    filename = target_filename(doc, vendor, amount, currency, doc_date)
    destination = f"{folder}/{filename}"
    if not source:
        raise RuntimeError(f"У документа {doc.id} нет пути в Dropbox")

    # Намерение пишется ДО операции: падение между шагами оставит след,
    # по которому sweeper доведёт дело до конца, а не потеряет файл.
    intent = MoveIntent(document_id=doc.id, from_path=source, to_path=destination)
    session.add(intent)
    await session.flush()

    try:
        final = await dbx.move(source, destination)
    except SourceMissing:
        # Исход известен точно: move не случился. Намерение закрываем
        # сразу — иначе sweeper будет слать алерт про него вечно.
        intent.completed_at = datetime.now(timezone.utc)
        found = await locate(doc)
        if found is None:
            raise
        logger.info("Документ %s не по записанному пути, найден: %s", doc.id, found)
        doc.dropbox_path = found
        if in_company_folder(found, company):
            # Из входящих в папку компании файл мог попасть только руками.
            return found, closed_note
        intent = MoveIntent(document_id=doc.id, from_path=found, to_path=destination)
        session.add(intent)
        await session.flush()
        final = await dbx.move(found, destination)

    intent.completed_at = datetime.now(timezone.utc)
    return final, closed_note


def in_company_folder(path: str, company: str) -> bool:
    """Файл лежит в папке компании, а не во входящих. Если записанный
    путь вёл не туда, значит файл разложил человек, и перекладывать его
    по нашему соглашению об именах нельзя."""
    root = settings.dropbox_company_roots.get(company)
    return bool(root) and path.startswith(root + "/") and not path.startswith(
        settings.dropbox_inbox_path
    )


async def locate(doc: Document) -> str | None:
    """Где файл документа лежит на самом деле.

    Записанный путь, если файл там. Иначе поиск по content_hash во входящих
    и в папках месяцев всех компаний: фин директор перекладывает файлы
    руками, а сверка узнаёт об этом только раз в несколько часов.
    """
    dbx = get_dropbox()
    if doc.dropbox_path and await dbx.exists(doc.dropbox_path):
        return doc.dropbox_path
    if not doc.content_hash:
        return None

    from app.tasks import reconciler  # сверка импортирует сервисы, не наоборот

    candidates: list[tuple[str, str]] = []
    try:
        inbox = await dbx.list_files(settings.dropbox_inbox_path)
        candidates += reconciler._collect(inbox).get(doc.content_hash, [])
    except Exception:
        logger.warning("Не удалось просмотреть входящие при поиске документа %s", doc.id)
    for company, root in settings.dropbox_company_roots.items():
        listed = await reconciler._list_company(company, root)
        if listed:
            candidates += listed.get(doc.content_hash, [])
    if not candidates:
        return None
    return reconciler._pick(doc.dropbox_path, candidates)[0]


def ignored_path(doc: Document) -> str:
    """Куда откладывается отброшенный файл: `_inbox/_ignored/<Месяц_Год>`.

    Внутри `_inbox`, потому что move_v2 через границу namespace запрещён,
    а сверка во входящие не смотрит. Префикс id делает имя уникальным:
    move без autorename при конфликте имён счёл бы, что файл уже на месте.
    """
    root = settings.dropbox_inbox_path.rstrip("/")
    month = naming.month_folder(doc.received_at or datetime.now(timezone.utc))
    return f"{root}/_ignored/{month}/{doc.id}_{doc.original_name}"


async def set_aside(doc: Document) -> str | None:
    """Убирает файл отброшенного документа с глаз, ничего не удаляя.

    Файлы из Dropbox система не удаляет никогда (решение владельца
    19.10.2026): удаление «только из инбокса» вызывалось по любому пути
    и снесло из папок компаний два файла фин директора. Поэтому:

    * файл, положенный человеком, не трогается вообще;
    * файл, который привезла система (из входящих или уже разложенный ею
      в папку компании — проигравший экземпляр дубля), уезжает в
      `_inbox/_ignored`, откуда его можно вернуть руками. Запись остаётся
      журналом решений и защищает от повторного скачивания того же письма.

    Возвращает путь, по которому файл лежит после вызова.
    """
    path = doc.dropbox_path
    if not path:
        return None
    if doc.source == Source.manual:
        logger.info("Документ %s отброшен, файл фин директора остаётся на месте: %s",
                    doc.id, path)
        return path
    target = ignored_path(doc)
    if path == target:
        return path
    dbx = get_dropbox()
    await dbx.ensure_folder(target.rsplit("/", 1)[0])
    try:
        final = await dbx.move(path, target)
    except SourceMissing:
        # Файла по записанному пути нет — откладывать нечего. Если его
        # переложили руками, сверка найдёт его по содержимому.
        logger.warning("Документ %s отброшен, файла по пути %s уже нет", doc.id, path)
        return None
    logger.info("Документ %s отброшен, файл отложен: %s", doc.id, final)
    return final
