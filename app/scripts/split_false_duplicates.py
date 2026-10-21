"""Разовая расклейка ложных дублей на доске (21.10.2026).

До правки `dedupe.same_expense` синк считал одним расходом всё, у чего
совпали вендор, сумма и валюта. Из 28 склеек 26 оказались разными
расходами: сентябрь и октябрь одной подписки, два билета одного рейса,
счета EasyStaff, которые фин директор прямо назвала разными. Строка
одного расхода прятала другой, на доске его не было вовсе.

Скрипт снимает `merged_into_id` там, где новое правило пару не склеило бы.
Дальше синк сам: документ без айтема получает свою строку, документ с
айтемом снова обновляет свою.

    docker exec expense-collector python -m app.scripts.split_false_duplicates
    docker exec expense-collector python -m app.scripts.split_false_duplicates --apply
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.models import Document
from app.db.session import session_scope
from app.services import dedupe, monday


async def main(apply: bool) -> None:
    async with session_scope() as session:
        merged = (
            await session.execute(
                select(Document).where(Document.merged_into_id.is_not(None)).order_by(Document.id)
            )
        ).scalars().all()

        split = kept = 0
        for doc in merged:
            winner = await session.get(Document, doc.merged_into_id)
            blocked = await dedupe.said_different(session, [doc, winner])
            same = dedupe.same_expense(doc, winner, blocked)
            mark = "  остаётся " if same else "  РАСКЛЕИТЬ"
            item = ""
            if not same and doc.monday_item_id:
                alive = bool(await monday.item_values(doc.monday_item_id))
                item = f"  айтем {doc.monday_item_id} {'есть' if alive else 'НЕТ на доске'}"
            print(f"{mark} {doc.id:4d} -> {winner.id:4d}  {doc.status.value:8s} "
                  f"{doc.doc_date} / {winner.doc_date}  "
                  f"№ {doc.doc_number or '—'} / {winner.doc_number or '—'}  "
                  f"{(doc.vendor or '')[:24]}{item}")
            if same:
                kept += 1
                continue
            split += 1
            if apply:
                doc.merged_into_id = None

        print(f"\nСклеек: {len(merged)}, остаются: {kept}, расклеить: {split}")
        if not apply:
            print("Это только показ. Чтобы применить: --apply")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
