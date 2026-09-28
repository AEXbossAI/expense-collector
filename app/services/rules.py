from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RoutingRule
from app.services import dedupe

logger = logging.getLogger(__name__)


async def _active(session: AsyncSession, target: str) -> list[RoutingRule]:
    return (
        await session.execute(
            select(RoutingRule).where(
                RoutingRule.active.is_(True), RoutingRule.target == target
            )
        )
    ).scalars().all()


async def match_project(
    session: AsyncSession, vendor: str | None, sender: str | None
) -> tuple[int, str | None] | None:
    """Проект по правилу. Возвращает (monday_id проекта, название).

    Правило слабее кода задачи в документе и сильнее пустоты: код говорит
    об ЭТОМ документе, правило — лишь о привычке подрядчика работать на
    одном объекте. Заводится только явным действием человека.
    """
    vendor_key, sender_key = _keys(vendor, sender)
    for rule in await _active(session, "project"):
        if not rule.project_monday_id:
            continue
        if rule.kind == "vendor" and vendor_key and rule.pattern == vendor_key:
            return rule.project_monday_id, rule.project_name
        if rule.kind == "sender" and sender_key and rule.pattern == sender_key:
            return rule.project_monday_id, rule.project_name
    return None


def _keys(vendor: str | None, sender: str | None) -> tuple[str, str]:
    return dedupe.normalize_vendor(vendor), (sender or "").strip().lower()


async def match(
    session: AsyncSession, vendor: str | None, sender: str | None
) -> tuple[str, str] | None:
    """Активное правило под документ. Возвращает (компания, объяснение).

    Правило слабее идентификатора в документе и сильнее ответа модели:
    идентификатор говорит об этом конкретном документе, а правило —
    лишь о привычке вендора.
    """
    vendor_key, sender_key = _keys(vendor, sender)

    for rule in await _active(session, "company"):
        if not rule.company:
            continue
        if rule.kind == "vendor" and vendor_key and rule.pattern == vendor_key:
            return rule.company, f"правило по вендору {rule.pattern!r}"
        if rule.kind == "sender" and sender_key and rule.pattern == sender_key:
            return rule.company, f"правило по отправителю {rule.pattern!r}"
    return None


async def add(
    session: AsyncSession,
    kind: str,
    pattern: str,
    who: str,
    *,
    company: str | None = None,
    project_monday_id: int | None = None,
    project_name: str | None = None,
) -> RoutingRule:
    """Создание правила. Вызывается ТОЛЬКО из явного действия человека:
    ни счётчик ответов, ни отсутствие разногласий его не включают.

    Правило про компанию и правило про проект живут в одной таблице, но не
    конкурируют: у них разный `target`, и один и тот же вендор может иметь
    оба — «Bolt всегда эстонская» и «этот подрядчик всегда на Orhot Tora».
    """
    if kind not in {"vendor", "sender"}:
        raise ValueError(f"Неизвестный тип правила: {kind}")
    if (company is None) == (project_monday_id is None):
        raise ValueError("Правило задаёт ЛИБО компанию, ЛИБО проект")

    target = "company" if company is not None else "project"
    normalized = dedupe.normalize_vendor(pattern) if kind == "vendor" else pattern.strip().lower()
    existing = (
        await session.execute(
            select(RoutingRule).where(
                RoutingRule.kind == kind,
                RoutingRule.pattern == normalized,
                RoutingRule.target == target,
            )
        )
    ).scalar_one_or_none()

    goal = company or project_name or str(project_monday_id)

    if existing is not None:
        existing.company = company
        existing.project_monday_id = project_monday_id
        existing.project_name = project_name
        existing.active = True
        existing.confirmed_by = who
        logger.info("Правило обновлено: %s %s %s -> %s", target, kind, normalized, goal)
        return existing

    rule = RoutingRule(
        kind=kind,
        pattern=normalized,
        target=target,
        company=company,
        project_monday_id=project_monday_id,
        project_name=project_name,
        confirmed_by=who,
    )
    session.add(rule)
    logger.info("Правило создано: %s %s %s -> %s", target, kind, normalized, goal)
    return rule
