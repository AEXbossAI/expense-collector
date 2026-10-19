from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import DocKind, Document, Source, Status

logger = logging.getLogger(__name__)
settings = get_settings()

# Документы в статусе new кандидатами в дубли НЕ считаются.
#
# Иначе при разборе первого из пары второй (ещё не разобранный) находится
# как дубль, а при разборе второго — первый. Оба уходят в очередь, ни один
# не раскладывается. Поймано на боевом прогоне: пары Tonystam, Slack и
# Anthropic пометили друг друга и застряли (коммит 665fe8a).
#
# Правильный порядок: первый разбирается и раскладывается, второй видит
# уже разложенный и честно помечается дублём.
_CANDIDATE_STATUSES = [Status.filed, Status.needs_review, Status.probable_duplicate]


def is_dedupe_candidate(status: Status | None, merged_into_id: int | None) -> bool:
    """Может ли документ вообще оказаться «тем самым, уже учтённым».

    Вынесено из SQL отдельной чистой функцией не ради красоты: правило выше
    жило только в комментарии, и его уже один раз нарушили. Теперь у него
    есть тест, и следующая правка запроса сломает тест, а не прод.

    Не-представитель группы дублей (merged_into_id заполнен) кандидатом не
    является: он сам указывает на другой документ, и найти его как «основной»
    значит построить цепочку, в которой дубль ссылается на дубль.
    """
    if merged_into_id is not None:
        return False
    return status in _CANDIDATE_STATUSES


def _candidate_clause():
    """То же правило в терминах SQL. Читает те же константы, что и
    `is_dedupe_candidate`, чтобы они не разъехались."""
    return and_(
        Document.status.in_(_CANDIDATE_STATUSES),
        Document.merged_into_id.is_(None),
    )



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
        _candidate_clause(),
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
        _candidate_clause(),
        or_(
            Document.doc_date.is_(None),
            and_(
                Document.doc_date >= doc_date - window,
                Document.doc_date <= doc_date + window,
            ),
        ),
    )
    return (await session.execute(stmt)).scalars().first()


def rank(doc: Document) -> tuple[int, int, int]:
    """Чем выше кортеж, тем лучше артефакт.

    Ярусы, сверху вниз:

    1. **квитанция сильнее счёта** — группа `Out` на доске фиксирует оплату,
       и документ, подтверждающий её, ближе к факту расхода, чем счёт;
    2. **PDF сильнее фотографии** — текстовый слой, нет бликов и обрезанных
       краёв;
    3. больший размер файла.

    Без первого яруса пара «счёт + квитанция» на один расход отдавала бы
    на доску случайный из двух. `getattr` здесь намеренно: у документов,
    разобранных до появления `doc_kind`, поля просто нет, и это не повод
    падать.
    """
    doc_kind = getattr(doc, "doc_kind", None)
    is_receipt = 1 if doc_kind == DocKind.receipt else 0
    is_pdf = 1 if (doc.mime_type or "").endswith("pdf") else 0
    return (is_receipt, is_pdf, doc.size_bytes or 0)


def prefer(a: Document, b: Document) -> Document:
    """Какой артефакт оставить основным при подтверждённом дубле.

    Файл, положенный фин директором, по умолчанию лучше привезённого
    системой (решение владельца 19.10.2026): она выбрала именно его и
    назвала по-своему. Качество артефакта решает только между равными.

    В `rank` это намеренно не внесено: по нему выбирается представитель
    группы на доске, и смена ранга у старых групп завела бы вторую строку
    на тот же расход.
    """
    a_manual = getattr(a, "source", None) == Source.manual
    b_manual = getattr(b, "source", None) == Source.manual
    if a_manual != b_manual:
        return a if a_manual else b
    return a if rank(a) >= rank(b) else b


def choose_representative(docs: list[Document]) -> Document:
    """Кто из группы дублей поедет на доску. Ровно один документ на расход.

    Детерминированно: при равных рангах побеждает меньший id. Иначе
    перезапуск синка мог бы выбрать другого представителя и завести
    вторую строку на тот же расход.
    """
    if not docs:
        raise ValueError("группа дублей пуста")
    return sorted(docs, key=lambda d: (tuple(-x for x in rank(d)), d.id))[0]


async def group_of(session: AsyncSession, doc: Document) -> list[Document]:
    """Прямые соседи документа по любому из трёх признаков.

    Замыкание НЕ транзитивное, на один шаг. Оба реальных случая — счёт и
    квитанция одним письмом, одно вложение двумя письмами — это прямые
    соседи. Полное замыкание на годовом объёме означало бы N запросов на
    документ ради случая, которого в данных нет.
    """
    conditions = []
    if doc.content_hash:
        conditions.append(Document.content_hash == doc.content_hash)
    if doc.business_key:
        conditions.append(Document.business_key == doc.business_key)
    if doc.doc_number and doc.vendor:
        # Номер документа сам по себе не уникален между вендорами: «40378»
        # найдётся у каждого второго. Пара «вендор + номер» — уже ключ.
        conditions.append(
            and_(Document.doc_number == doc.doc_number, Document.vendor == doc.vendor)
        )
    if not conditions:
        return [doc]

    rows = (
        await session.execute(
            select(Document).where(
                Document.status == Status.filed,
                Document.merged_into_id.is_(None),
                or_(*conditions),
            )
        )
    ).scalars().all()

    by_id = {d.id: d for d in rows}
    by_id[doc.id] = doc
    return sorted(by_id.values(), key=lambda d: d.id)
