from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Document, Status

logger = logging.getLogger(__name__)
settings = get_settings()

# Документы в статусе new кандидатами в дубли НЕ считаются.
#
# Иначе при разборе первого из пары второй (ещё не разобранный) находится
# как дубль, а при разборе второго — первый. Оба уходят в очередь, ни один
# не раскладывается. Поймано на боевом прогоне: пары Tonystam, Slack и
# Anthropic пометили друг друга и застряли.
#
# Правильный порядок: первый разбирается и раскладывается, второй видит
# уже разложенный и честно помечается дублём.
_CANDIDATE_STATUSES = [Status.filed, Status.needs_review, Status.probable_duplicate]



def normalize_vendor(vendor: str | None) -> str:
    """Схлопывает вариации написания поставщика.

    'Tonystam', 'TONYSTAM LLC', 'Tonystam, Inc.' должны дать один ключ,
    иначе бизнес-уровень дедупа не сработает никогда.
    """
    if not vendor:
        return ""
    text = unicodedata.normalize("NFKD", vendor).lower()
    text = re.sub(r"\b(ltd|llc|inc|gmbh|oü|ou|bv|sarl|ооо|оао|зао|бе[\"']?м)\b", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def business_key(vendor: str | None, amount: float | None, currency: str | None) -> str | None:
    """Ключ без даты: дата сравнивается окном, поэтому в ключ не входит."""
    v = normalize_vendor(vendor)
    if not v or amount is None or not currency:
        return None
    return f"{v}|{amount:.2f}|{currency.upper()}"


async def find_exact(
    session: AsyncSession, doc: Document, content_hash: str | None
) -> Document | None:
    """Точный уровень: тот же файл байт в байт.

    Ловит вендоров, шлющих одно вложение двумя письмами — Tonystam
    присылает 'Customer Invoice' и 'Credit Card Payment Confirmation'
    с идентичным PDF, воспроизведено на живых данных.
    """
    if not content_hash:
        return None
    stmt = select(Document).where(
        Document.id != doc.id,
        Document.content_hash == content_hash,
        Document.status.in_(_CANDIDATE_STATUSES),
    )
    return (await session.execute(stmt)).scalars().first()


async def find_business(
    session: AsyncSession,
    doc: Document,
    vendor: str | None,
    amount: float | None,
    currency: str | None,
    doc_date: date | None,
) -> Document | None:
    """Бизнес-уровень: тот же расход, пришедший другим каналом.

    PDF из почты и фотография того же инвойса из телеграма имеют разные
    байты, поэтому точный уровень их не увидит. Совпадение НЕ означает
    автослияния: два одинаковых платежа одному вендору в один день —
    законный сценарий, решение принимает человек.
    """
    key = business_key(vendor, amount, currency)
    if not key or doc_date is None:
        return None

    window = timedelta(days=settings.dedup_date_window_days)
    stmt = select(Document).where(
        Document.id != doc.id,
        Document.business_key == key,
        Document.status.in_(_CANDIDATE_STATUSES),
        or_(
            Document.doc_date.is_(None),
            and_(
                Document.doc_date >= doc_date - window,
                Document.doc_date <= doc_date + window,
            ),
        ),
    )
    return (await session.execute(stmt)).scalars().first()


def prefer(a: Document, b: Document) -> Document:
    """Какой артефакт оставить основным при подтверждённом дубле.

    PDF всегда лучше фотографии: текстовый слой, нет бликов и обрезанных
    краёв. Без этого правила first-write-wins оставит худшую копию.
    """
    def rank(doc: Document) -> tuple[int, int]:
        is_pdf = 1 if (doc.mime_type or "").endswith("pdf") else 0
        return (is_pdf, doc.size_bytes or 0)

    return a if rank(a) >= rank(b) else b
