from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import naming
from app.core.config import get_settings
from app.db.models import Document, MonthState, MoveIntent
from app.services import buffer
from app.services.dropbox_client import get_dropbox

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
) -> tuple[str, str | None]:
    """Перекладывает документ в папку компании и месяца.

    Возвращает (новый путь, пометка про закрытый месяц).
    """
    dbx = get_dropbox()
    effective = doc_date or doc.received_at.date()
    closed_note: str | None = None

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
    source = doc.dropbox_path
    if not source:
        raise RuntimeError(f"У документа {doc.id} нет пути в Dropbox")

    # Намерение пишется ДО операции: падение между шагами оставит след,
    # по которому sweeper доведёт дело до конца, а не потеряет файл.
    intent = MoveIntent(document_id=doc.id, from_path=source, to_path=destination)
    session.add(intent)
    await session.flush()

    final = await dbx.move(source, destination)

    intent.completed_at = datetime.now(timezone.utc)
    return final, closed_note


async def discard_ignored(doc: Document) -> None:
    """Файл удаляется, запись остаётся.

    Запись защищает от повторного скачивания того же письма и служит
    журналом решений. Файл удаляется, потому что рассылка, отрендеренная
    из HTML-тела, весит мегабайты (Tele2 дала 3.4 МБ) и инбоксу не нужна.
    """
    if not doc.dropbox_path:
        return
    await get_dropbox().delete(doc.dropbox_path)
    logger.info("Документ %s помечен ignored, файл удалён из _inbox", doc.id)
