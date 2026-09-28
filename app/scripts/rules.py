"""Управление правилами маршрутизации.

Правило создаётся ТОЛЬКО этой командой, то есть явным решением человека.
Ни счётчик ответов, ни отсутствие разногласий его не включают.

    python -m app.scripts.rules list
    python -m app.scripts.rules add vendor "Bolt" orlanda_engineering
    python -m app.scripts.rules add sender vralumglass@gmail.com orlanda_engineering
    python -m app.scripts.rules project vendor "Podlyaby" 2776951902
    python -m app.scripts.rules off vendor bolt
    python -m app.scripts.rules off vendor podlyaby project

Правило про компанию и правило про проект живут раздельно: один и тот же
вендор может иметь оба сразу, и выключение одного не трогает второе.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.models import Document, MondayProject, RoutingRule, Status
from app.db.session import session_scope
from app.services import dedupe, rules


async def _list() -> None:
    async with session_scope() as session:
        rows = (await session.execute(select(RoutingRule).order_by(RoutingRule.id))).scalars().all()
    if not rows:
        print("Правил нет.")
        return
    for r in rows:
        state = "активно" if r.active else "выключено"
        goal = r.company or f"{r.project_name or '?'} ({r.project_monday_id})"
        print(f"  [{r.id}] {r.target:7s} {r.kind:7s} {r.pattern:28s} -> {goal:30s} "
              f"{state}  ({r.confirmed_by})")


async def _requeue(session, rule: RoutingRule) -> int:
    """Правило применяется задним числом к тем, кто уже висит в очереди.

    Только к очереди по компании: документ без проекта в очереди не висит,
    он давно разложен, и задним числом его чинит переразбор, а не это.
    """
    if rule.target != "company":
        return 0
    pending = (
        await session.execute(select(Document).where(Document.status == Status.needs_review))
    ).scalars().all()
    back = 0
    for doc in pending:
        key = (
            dedupe.normalize_vendor(doc.vendor) if rule.kind == "vendor"
            else (doc.sender or "").lower()
        )
        if key == rule.pattern:
            doc.status = Status.new
            doc.status_reason = None
            back += 1
    return back


async def _add_company(kind: str, pattern: str, company: str) -> None:
    async with session_scope() as session:
        rule = await rules.add(session, kind, pattern, who="владелец", company=company)
        back = await _requeue(session, rule)
    print(f"Правило {kind} {pattern!r} -> {company}")
    if back:
        print(f"Возвращено на раскладку из очереди: {back}")


async def _add_project(kind: str, pattern: str, project_id: str) -> None:
    if not project_id.isdigit():
        print("id проекта — число из Monday (колонка «ID элемента» на доске Projects).")
        return
    async with session_scope() as session:
        project = await session.get(MondayProject, int(project_id))
        if project is None:
            print(f"Проекта {project_id} нет в справочнике. Сначала прогрейте кэш: "
                  f"python -m app.scripts.match_projects --refresh")
            return
        await rules.add(
            session, kind, pattern, who="владелец",
            project_monday_id=project.monday_id, project_name=project.name,
        )
    print(f"Правило {kind} {pattern!r} -> проект {project.name}")


async def _off(kind: str, pattern: str, target: str) -> None:
    async with session_scope() as session:
        normalized = dedupe.normalize_vendor(pattern) if kind == "vendor" else pattern.lower()
        row = (
            await session.execute(
                select(RoutingRule).where(
                    RoutingRule.kind == kind,
                    RoutingRule.pattern == normalized,
                    RoutingRule.target == target,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            print("Такого правила нет.")
            return
        row.active = False
    print(f"Правило {target} {kind} {normalized!r} выключено.")


def main() -> None:
    argv = sys.argv
    if len(argv) < 2:
        print(__doc__)
        return
    cmd = argv[1]
    if cmd == "list":
        asyncio.run(_list())
    elif cmd == "add" and len(argv) == 5:
        asyncio.run(_add_company(argv[2], argv[3], argv[4]))
    elif cmd == "project" and len(argv) == 5:
        asyncio.run(_add_project(argv[2], argv[3], argv[4]))
    elif cmd == "off" and len(argv) in (4, 5):
        asyncio.run(_off(argv[2], argv[3], argv[4] if len(argv) == 5 else "company"))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
