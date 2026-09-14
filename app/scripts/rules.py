"""Управление правилами маршрутизации.

Правило создаётся ТОЛЬКО этой командой, то есть явным решением человека.
Ни счётчик ответов, ни отсутствие разногласий его не включают.

    python -m app.scripts.rules list
    python -m app.scripts.rules add vendor "Bolt" orlanda_engineering
    python -m app.scripts.rules add sender vralumglass@gmail.com orlanda_engineering
    python -m app.scripts.rules off vendor bolt
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.models import Document, RoutingRule, Status
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
        print(f"  [{r.id}] {r.kind:7s} {r.pattern:30s} -> {r.company:22s} {state}  ({r.confirmed_by})")


async def _add(kind: str, pattern: str, company: str) -> None:
    async with session_scope() as session:
        rule = await rules.add(session, kind, pattern, company, who="владелец")
        # Правило применяется задним числом к тем, кто уже висит в очереди
        pending = (
            await session.execute(
                select(Document).where(Document.status == Status.needs_review)
            )
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
    print(f"Правило {kind} {pattern!r} -> {company}")
    if back:
        print(f"Возвращено на раскладку из очереди: {back}")


async def _off(kind: str, pattern: str) -> None:
    async with session_scope() as session:
        normalized = dedupe.normalize_vendor(pattern) if kind == "vendor" else pattern.lower()
        row = (
            await session.execute(
                select(RoutingRule).where(
                    RoutingRule.kind == kind, RoutingRule.pattern == normalized
                )
            )
        ).scalar_one_or_none()
        if row is None:
            print("Такого правила нет.")
            return
        row.active = False
    print(f"Правило {kind} {normalized!r} выключено.")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        asyncio.run(_list())
    elif cmd == "add" and len(sys.argv) == 5:
        asyncio.run(_add(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif cmd == "off" and len(sys.argv) == 4:
        asyncio.run(_off(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
