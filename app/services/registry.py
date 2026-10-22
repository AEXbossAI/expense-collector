from __future__ import annotations

import csv
import io
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import naming
from app.core.config import get_settings
from app.db.models import Document, Source, Status
from app.services.dropbox_client import content_hash, get_dropbox

logger = logging.getLogger(__name__)
settings = get_settings()

HEADERS = [
    "Файл", "Дата документа", "Вендор", "Назначение",
    "Сумма", "Валюта", "Источник", "Примечания",
]

SOURCE_TITLES = {
    Source.mail: "Почта",
    Source.telegram: "Телеграм",
    Source.manual: "Вручную",
}


def _notes(doc: Document) -> str:
    notes = []
    if doc.month_from_receipt:
        notes.append("месяц по дате получения")
    if doc.belongs_to_closed_month:
        notes.append(f"относится к закрытому {doc.belongs_to_closed_month}")
    if doc.status_reason and "по умолчанию" in doc.status_reason:
        notes.append("компания по умолчанию")
    if doc.status_reason and "вручную" in doc.status_reason:
        notes.append("разложено вручную")
    if doc.status_reason and "проверить:" in doc.status_reason:
        notes.append("ПРОВЕРИТЬ: " + doc.status_reason.split("проверить:", 1)[1].strip())
    travel = naming.travel_date(doc)
    if travel:
        notes.append(f"поездка: {travel:%d.%m.%Y}")
    if doc.service_period:
        notes.append(f"период услуги: {doc.service_period}")
    # Строка поездки уходит в месяц поездки, а файл фин директора остаётся
    # в её подпапке банка — без пометки его в этом месяце не найти.
    root = settings.dropbox_company_roots.get(getattr(doc, "company", None) or "")
    path = getattr(doc, "dropbox_path", None) or ""
    folder = naming.folder_in_month(path, root) if root else None
    if folder and folder.split("/", 1)[0] != month_of(doc):
        notes.append(f"файл в папке {folder}")
    return "; ".join(notes)


def build_csv(docs: list[Document]) -> bytes:
    """CSV для Excel в русской локали.

    UTF-8 с BOM и разделитель ';' — без них Excel покажет всё одной
    колонкой и кириллицу кракозябрами. Это не придирка: реестр открывает
    фин директор на своей машине, а не мы на сервере.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(HEADERS)

    total = 0.0
    currency = None
    for doc in sorted(docs, key=lambda d: (d.doc_date or datetime.max.date(), d.id)):
        name = (doc.dropbox_path or doc.original_name).rsplit("/", 1)[-1]
        amount = float(doc.amount) if doc.amount is not None else None
        writer.writerow([
            name,
            doc.doc_date.strftime("%d.%m.%Y") if doc.doc_date else "",
            doc.vendor or "",
            doc.purpose or "",
            f"{amount:.2f}".replace(".", ",") if amount is not None else "",
            doc.currency or "",
            SOURCE_TITLES.get(doc.source, str(doc.source)),
            _notes(doc),
        ])
        if amount is not None:
            total += amount
            currency = currency or doc.currency

    writer.writerow([])
    writer.writerow(["ИТОГО", "", "", "", f"{total:.2f}".replace(".", ","), currency or "", "", ""])

    # BOM обязателен: без него Excel читает файл как ANSI
    return "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")


def month_of(doc: Document) -> str | None:
    """В реестр какого месяца входит документ — `naming.expense_month`."""
    return naming.expense_month(doc)


async def regenerate(session: AsyncSession, company: str, month: str) -> int:
    """Полная перегенерация реестра одной папки. Идемпотентно.

    Инкрементальных дописываний нет намеренно: одна прерванная запись
    оставила бы реестр битым навсегда.
    """
    root = settings.dropbox_company_roots.get(company)
    if not root:
        logger.warning("Нет корневой папки для компании %r", company)
        return 0

    docs = (
        await session.execute(
            select(Document).where(
                Document.company == company,
                Document.status == Status.filed,
            )
        )
    ).scalars().all()

    docs = [d for d in docs if month_of(d) == month]

    year = month.rsplit("_", 1)[1]
    path = f"{root.rstrip('/')}/{year}/{month}/{settings.registry_filename}"
    dbx = get_dropbox()
    data = build_csv(docs)
    existing = await dbx.hash_of(path)
    if not docs and existing is None:
        # Пустой реестр заводим только поверх старого: последний документ
        # месяца ушёл, а строки в файле остались бы. Папку месяца ради
        # пустого реестра не создаём.
        return 0
    if existing == content_hash(data):
        # Перезапись того же содержимого — лишняя версия у десктопного
        # клиента фин директора и лишний повод для conflicted copy.
        return len(docs)
    await dbx.upload_bytes(data, path)
    logger.info("Реестр обновлён: %s (%d строк)", path, len(docs))
    return len(docs)
