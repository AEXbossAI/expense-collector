from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Document, RoutingAnswer, Status
from app.services import dedupe

logger = logging.getLogger(__name__)
settings = get_settings()

COMPANY_TITLES = {
    "orlanda_engineering": "Эстонская",
    "orlanda_israel": "Израильская",
}


@dataclass
class QueueItem:
    """Один вопрос в дайджесте. Может охватывать несколько документов:
    три чека Bolt за неделю — это один вопрос, ответ всё равно один."""

    kind: str                      # company | duplicate
    key: str                       # ключ группировки
    title: str
    document_ids: list[int] = field(default_factory=list)
    total: float = 0.0
    currency: str | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def callback_key(self) -> str:
        """Telegram ограничивает callback_data 64 байтами, поэтому передаём
        не список документов, а идентификатор первого — остальные
        восстанавливаются по той же группировке."""
        return f"{self.kind}:{self.document_ids[0]}"


async def build(session: AsyncSession) -> list[QueueItem]:
    """Очередь вопросов, сгруппированная так, чтобы человек отвечал
    один раз на однотипные документы."""
    items: list[QueueItem] = []

    # 1. Вопрос "чья компания", группировка по нормализованному вендору
    rows = (
        await session.execute(
            select(Document)
            .where(Document.status == Status.needs_review)
            .order_by(Document.doc_date, Document.id)
        )
    ).scalars().all()

    groups: dict[str, list[Document]] = {}
    for doc in rows:
        key = dedupe.normalize_vendor(doc.vendor) or f"doc{doc.id}"
        groups.setdefault(key, []).append(doc)

    for key, docs in groups.items():
        head = docs[0]
        total = sum(float(d.amount or 0) for d in docs)
        item = QueueItem(
            kind="company",
            key=key,
            title=head.vendor or head.original_name or "без названия",
            document_ids=[d.id for d in docs],
            total=total,
            currency=head.currency,
        )
        for d in docs:
            when = d.doc_date.strftime("%d.%m") if d.doc_date else "дата?"
            item.lines.append(f"{when} · {d.amount or '?'} {d.currency or ''} · {d.purpose or '—'}")
        items.append(item)

    # 2. Подтверждение дублей. Вопрос бинарный и того же рода,
    #    поэтому живёт в том же интерфейсе, а не в отдельном.
    dups = (
        await session.execute(
            select(Document)
            .where(Document.status == Status.probable_duplicate)
            .order_by(Document.id)
        )
    ).scalars().all()

    for doc in dups:
        primary = await _primary_of(session, doc)
        items.append(
            QueueItem(
                kind="duplicate",
                key=str(doc.id),
                title=doc.vendor or doc.original_name,
                document_ids=[doc.id],
                total=float(doc.amount or 0),
                currency=doc.currency,
                lines=[describe_duplicate(doc, primary)],
            )
        )

    items.extend(await _project_questions(session))
    return items


async def _project_questions(session: AsyncSession) -> list[QueueItem]:
    """Вопрос «к какому проекту», и только там, где он осмыслен.

    Документ при этом УЖЕ разложен и уже в реестре: незнание проекта не
    повод держать расход вне папки. Поэтому вопрос строится по `filed`,
    а не по `needs_review`.

    Накладные расходы не спрашиваются вовсе. Software & subscriptions,
    Accounting, Legal — общие по определению, и тридцать бессмысленных
    вопросов в месяц кончатся тем, что дайджест перестанут открывать.
    """
    asked = set(
        (
            await session.execute(
                select(RoutingAnswer.document_id).where(RoutingAnswer.question == "project")
            )
        ).scalars().all()
    )

    rows = (
        await session.execute(
            select(Document)
            .where(
                Document.status == Status.filed,
                Document.merged_into_id.is_(None),
                Document.project_monday_id.is_(None),
                Document.category.is_not(None),
                Document.category.not_in(settings.overhead_categories),
            )
            .order_by(Document.doc_date, Document.id)
        )
    ).scalars().all()

    groups: dict[str, list[Document]] = {}
    for doc in rows:
        if doc.id in asked:
            continue
        key = dedupe.normalize_vendor(doc.vendor) or f"doc{doc.id}"
        groups.setdefault(key, []).append(doc)

    items: list[QueueItem] = []
    for key, docs in groups.items():
        head = docs[0]
        item = QueueItem(
            kind="project",
            key=key,
            title=head.vendor or head.original_name or "без названия",
            document_ids=[d.id for d in docs],
            total=sum(float(d.amount or 0) for d in docs),
            currency=head.currency,
        )
        for d in docs[:4]:
            when = d.doc_date.strftime("%d.%m") if d.doc_date else "дата?"
            item.lines.append(f"{when} · {d.amount or '?'} {d.currency or ''} · {d.purpose or '—'}")
        items.append(item)

    return items


async def recent_projects(session: AsyncSession, limit: int = 5) -> list[tuple[int, str]]:
    """Проекты, по которым расходы уже проходили, свежие первыми.

    Кнопки строятся по ним, а не по всем 380 проектам: в дайджесте нужен
    короткий список того, что реально в работе.
    """
    rows = (
        await session.execute(
            select(Document.project_monday_id, Document.project_name)
            .where(Document.project_monday_id.is_not(None))
            .order_by(Document.doc_date.desc())
        )
    ).all()

    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for pid, name in rows:
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, name or str(pid)))
        if len(out) >= limit:
            break
    return out


async def _primary_of(session: AsyncSession, doc: Document) -> Document | None:
    """Документ, с которым этот был сочтён дублем."""
    import re

    match = re.search(r"документ (\d+)", doc.status_reason or "")
    if not match:
        return None
    return await session.get(Document, int(match.group(1)))


def describe_duplicate(doc: Document, primary: Document | None) -> str:
    """Вопрос формулируется по СУТИ совпадения, а не одинаково для всех.

    Три разных случая, и ответ на них может отличаться:
      - тот же файл двумя письмами (вендор шлёт счёт и подтверждение оплаты);
      - два РАЗНЫХ файла из одного письма (счёт и квитанция на один расход);
      - совпадение по сумме и вендору из разных источников.
    Формулировка "похоже, тот же расход пришёл дважды" верна только
    в первом случае и вводит в заблуждение в остальных.
    """
    if primary is None:
        return "Похоже на уже учтённый расход"

    if doc.content_hash and doc.content_hash == primary.content_hash:
        return (
            f"Один и тот же файл ({doc.original_name[:34]}) пришёл двумя письмами: "
            f"«{(primary.raw_caption or '')[:28]}» и «{(doc.raw_caption or '')[:28]}»"
        )

    if doc.email_id and doc.email_id == primary.email_id:
        return (
            f"Из одного письма два разных документа: {primary.original_name[:26]} "
            f"и {doc.original_name[:26]}. Один расход или два?"
        )

    if doc.original_name == primary.original_name:
        # Наблюдалось на Tonystam: "Customer Invoice" и "Credit Card Payment
        # Confirmation" приходят разными письмами с файлом одного имени, но
        # разным содержимым — вероятно, во втором счёт с отметкой об оплате.
        return (
            f"Двумя письмами пришёл файл с одним именем ({doc.original_name[:30]}), "
            f"но разным содержимым: «{(primary.raw_caption or '')[:26]}» "
            f"и «{(doc.raw_caption or '')[:26]}»"
        )

    return (
        f"Совпали вендор, сумма и дата с документом {primary.id} "
        f"({primary.original_name[:30]}), но файлы разные"
    )


def render(items: list[QueueItem]) -> str:
    """Текст дайджеста. Одно сообщение в день, а не поток уведомлений."""
    n_company = sum(1 for i in items if i.kind == "company")
    n_dup = sum(1 for i in items if i.kind == "duplicate")

    n_project = sum(1 for i in items if i.kind == "project")

    head = ["<b>Расходы, требующие решения</b>", ""]
    if n_company:
        head.append(f"Не удалось определить компанию: {n_company}")
    if n_dup:
        head.append(f"Возможные дубли: {n_dup}")
    if n_project:
        head.append(f"Неизвестен проект: {n_project}")
    head.append("")

    body: list[str] = []
    for n, item in enumerate(items, 1):
        if item.kind == "company":
            amount = f"{item.total:.2f} {item.currency or ''}".strip()
            count = f", {len(item.document_ids)} док." if len(item.document_ids) > 1 else ""
            body.append(f"<b>{n}. {item.title}</b> — {amount}{count}")
            body.extend(f"   {line}" for line in item.lines[:4])
        else:
            amount = f"{item.total:.2f} {item.currency or ''}".strip()
            body.append(f"<b>{n}. {item.title}</b> — {amount}")
            body.extend(f"   {line}" for line in item.lines if line)
        body.append("")

    return "\n".join(head + body).strip()
