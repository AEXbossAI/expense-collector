from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Document, Status
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
        items.append(
            QueueItem(
                kind="duplicate",
                key=str(doc.id),
                title=doc.vendor or doc.original_name,
                document_ids=[doc.id],
                total=float(doc.amount or 0),
                currency=doc.currency,
                lines=[doc.status_reason or ""],
            )
        )

    return items


def render(items: list[QueueItem]) -> str:
    """Текст дайджеста. Одно сообщение в день, а не поток уведомлений."""
    n_company = sum(1 for i in items if i.kind == "company")
    n_dup = sum(1 for i in items if i.kind == "duplicate")

    head = ["<b>Расходы, требующие решения</b>", ""]
    if n_company:
        head.append(f"Не удалось определить компанию: {n_company}")
    if n_dup:
        head.append(f"Возможные дубли: {n_dup}")
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
            body.append("   Похоже, тот же расход пришёл дважды")
        body.append("")

    return "\n".join(head + body).strip()
