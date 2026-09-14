from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RoutingRule
from app.services import dedupe

logger = logging.getLogger(__name__)


async def match(
    session: AsyncSession, vendor: str | None, sender: str | None
) -> tuple[str, str] | None:
    """Активное правило под документ. Возвращает (компания, объяснение).

    Правило слабее идентификатора в документе и сильнее ответа модели:
    идентификатор говорит об этом конкретном документе, а правило —
    лишь о привычке вендора.
    """
    rules = (
        await session.execute(select(RoutingRule).where(RoutingRule.active.is_(True)))
    ).scalars().all()
    if not rules:
        return None

    vendor_key = dedupe.normalize_vendor(vendor)
    sender_key = (sender or "").strip().lower()

    for rule in rules:
        if rule.kind == "vendor" and vendor_key and rule.pattern == vendor_key:
            return rule.company, f"правило по вендору {rule.pattern!r}"
        if rule.kind == "sender" and sender_key and rule.pattern == sender_key:
            return rule.company, f"правило по отправителю {rule.pattern!r}"
    return None


async def add(
    session: AsyncSession, kind: str, pattern: str, company: str, who: str,
) -> RoutingRule:
    """Создание правила. Вызывается ТОЛЬКО из явного действия человека:
    ни счётчик ответов, ни отсутствие разногласий его не включают."""
    if kind not in {"vendor", "sender"}:
        raise ValueError(f"Неизвестный тип правила: {kind}")

    normalized = dedupe.normalize_vendor(pattern) if kind == "vendor" else pattern.strip().lower()
    existing = (
        await session.execute(
            select(RoutingRule).where(
                RoutingRule.kind == kind, RoutingRule.pattern == normalized
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.company = company
        existing.active = True
        existing.confirmed_by = who
        logger.info("Правило обновлено: %s %s -> %s", kind, normalized, company)
        return existing

    rule = RoutingRule(kind=kind, pattern=normalized, company=company, confirmed_by=who)
    session.add(rule)
    logger.info("Правило создано: %s %s -> %s", kind, normalized, company)
    return rule
