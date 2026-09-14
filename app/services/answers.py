from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Document, RoutingAnswer, Status
from app.services import dedupe, filer

logger = logging.getLogger(__name__)
settings = get_settings()


async def _group(session: AsyncSession, doc: Document) -> list[Document]:
    """Все документы того же вопроса. Ответ применяется ко всей группе:
    три чека одного вендора — один вопрос и один ответ."""
    key = dedupe.normalize_vendor(doc.vendor)
    if not key:
        return [doc]
    rows = (
        await session.execute(
            select(Document).where(Document.status == Status.needs_review)
        )
    ).scalars().all()
    return [d for d in rows if dedupe.normalize_vendor(d.vendor) == key] or [doc]


async def _primary_of(session: AsyncSession, doc: Document) -> Document | None:
    """Документ, с которым этот был сочтён дублем."""
    match = re.search(r"документ (\d+)", doc.status_reason or "")
    if not match:
        return None
    return await session.get(Document, int(match.group(1)))


async def _record(session: AsyncSession, doc: Document, question: str,
                  answer: str, who: str) -> None:
    """Ответ сохраняется вместе с признаками, а не только с id документа:
    иначе через месяц из журнала не вывести ни одной закономерности."""
    session.add(
        RoutingAnswer(
            document_id=doc.id,
            question=question,
            answer=answer,
            answered_by=who,
            vendor_normalized=dedupe.normalize_vendor(doc.vendor) or None,
            sender=doc.sender,
            currency=doc.currency,
            amount=doc.amount,
            source=doc.source.value if doc.source else None,
        )
    )


async def answer_company(session: AsyncSession, doc_id: int, company: str,
                         who: str) -> tuple[int, str]:
    """Ответ на вопрос «чья компания». Возвращает (сколько документов, текст)."""
    doc = await session.get(Document, doc_id)
    if doc is None or doc.status != Status.needs_review:
        return 0, "Этот вопрос уже закрыт."

    docs = await _group(session, doc)
    handled = 0

    for d in docs:
        await _record(session, d, "company", company, who)

        if company == "skip":
            d.status = Status.ignored
            d.status_reason = "не распределяем (решение фин директора)"
            d.resolved_by = who
            d.resolved_at = datetime.now(timezone.utc)
            try:
                await filer.discard_ignored(d)
                d.dropbox_path = None
            except Exception:
                logger.exception("Не удалось удалить файл документа %s из _inbox", d.id)
            handled += 1
            continue

        try:
            path, closed = await filer.file_document(
                session, d,
                company=company,
                vendor=d.vendor,
                amount=float(d.amount) if d.amount is not None else None,
                currency=d.currency,
                doc_date=d.doc_date,
            )
        except Exception as exc:
            logger.exception("Не разложить документ %s", d.id)
            d.status_reason = f"ответ получен, но раскладка не удалась: {exc}"[:2000]
            continue

        d.company = company
        d.dropbox_path = path
        d.belongs_to_closed_month = closed
        d.status = Status.filed
        d.status_reason = "распределено фин директором"
        d.resolved_by = who
        d.resolved_at = datetime.now(timezone.utc)
        handled += 1

    if company == "skip":
        return handled, f"Не распределяем. Документов: {handled}."
    title = {"orlanda_engineering": "эстонскую", "orlanda_israel": "израильскую"}.get(company, company)
    return handled, f"Отнесено на {title} компанию. Документов: {handled}."


async def answer_duplicate(session: AsyncSession, doc_id: int, same: bool,
                           who: str) -> tuple[int, str]:
    """Подтверждение дубля. «Разные» возвращает документ в разбор,
    «один расход» закрывает его ссылкой на основной."""
    doc = await session.get(Document, doc_id)
    if doc is None or doc.status != Status.probable_duplicate:
        return 0, "Этот вопрос уже закрыт."

    await _record(session, doc, "duplicate", "same" if same else "different", who)

    if same:
        # Побеждает не тот, кто разобрался первым, а лучший артефакт:
        # PDF с текстовым слоем против фотографии с бликами и обрезанными
        # краями. Без этого first-write-wins оставляет худшую копию.
        #
        # Подмена — это не обмен статусами: файл победителя лежит в _inbox,
        # файл проигравшего в папке компании. Их надо реально переложить,
        # иначе в базе будет одно, а в Dropbox другое.
        primary = await _primary_of(session, doc)
        loser, winner = doc, primary

        if primary is not None and dedupe.prefer(doc, primary) is doc:
            logger.info("Дубль %s лучше основного %s, меняю местами", doc.id, primary.id)
            try:
                path, closed = await filer.file_document(
                    session, doc,
                    company=primary.company,
                    vendor=doc.vendor,
                    amount=float(doc.amount) if doc.amount is not None else None,
                    currency=doc.currency,
                    doc_date=doc.doc_date,
                )
            except Exception:
                logger.exception("Не удалось заменить основной экземпляр %s на %s",
                                 primary.id, doc.id)
                return 0, "Не получилось заменить экземпляр, попробуйте позже."
            doc.company = primary.company
            doc.dropbox_path = path
            doc.belongs_to_closed_month = closed
            doc.status = Status.filed
            doc.status_reason = "подтверждён человеком как основной экземпляр (лучше качеством)"
            doc.resolved_by = who
            doc.resolved_at = datetime.now(timezone.utc)
            loser, winner = primary, doc

        if loser is None:
            return 0, "Не нашёл, с чем сравнивать."

        loser.status = Status.ignored
        loser.status_reason = f"подтверждённый дубль: {loser.status_reason or ''}".strip()
        loser.resolved_by = who
        loser.resolved_at = datetime.now(timezone.utc)
        try:
            await filer.discard_ignored(loser)
            loser.dropbox_path = None
        except Exception:
            logger.exception("Не удалось удалить лишний экземпляр %s", loser.id)
        return 1, "Понято, один расход. Лишний экземпляр убран."

    # Разные расходы — документ идёт на обычную раскладку
    doc.status = Status.new
    doc.status_reason = "подтверждено человеком: не дубль"
    doc.business_key = None      # чтобы не поймался тем же ключом повторно
    return 1, "Понято, разные расходы. Второй будет разложен отдельно."
