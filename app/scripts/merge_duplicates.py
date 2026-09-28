"""Разовая чистка дублей перед первым синком с доской.

На 28.09.2026 в базе лежат пары, разобранные до появления правила «один
документ на расход»: `Invoice-2945.pdf` дважды (348.30 EUR, id 3 и 4) и
авиабилет flydubai дважды (844.81 USD, id 42 и 44 — от вендора и пересылкой
с invoices@orlanda.info). Если залить их как есть, фин директор увидит
двойной расход и выключит систему в тот же день.

Печатает группы и предлагает слить. Не автоматом — глазами, по образцу
`patterns.py`: решение о том, что два документа это один расход, остаётся
за человеком.

    docker exec expense-collector python -m app.scripts.merge_duplicates
    docker exec expense-collector python -m app.scripts.merge_duplicates --apply
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.models import Document, Status
from app.db.session import session_scope
from app.services import dedupe
from app.tasks import monday_sync


async def main(apply: bool) -> None:
    since = monday_sync.reconcile_from()

    async with session_scope() as session:
        docs = (
            await session.execute(
                select(Document)
                .where(
                    Document.status == Status.filed,
                    Document.merged_into_id.is_(None),
                    Document.doc_date >= since,
                )
                .order_by(Document.id)
            )
        ).scalars().all()

        seen: set[int] = set()
        groups: list[list[Document]] = []
        for doc in docs:
            if doc.id in seen:
                continue
            group = await dedupe.group_of(session, doc)
            for d in group:
                seen.add(d.id)
            if len(group) > 1:
                groups.append(group)

        if not groups:
            print("Групп дублей не найдено.")
            return

        merged = 0
        for group in groups:
            winner = dedupe.choose_representative(group)
            print(f"\nГруппа из {len(group)}:")
            for d in group:
                mark = "  ОСТАЁТСЯ" if d.id == winner.id else "  схлопнуть"
                kind = d.doc_kind.value if d.doc_kind else "?"
                print(f"{mark}  id={d.id:4d}  {d.doc_date}  {kind:11s} "
                      f"{str(d.amount or '?'):>10s} {d.currency or '':4s}  "
                      f"{(d.vendor or '')[:26]:28s} {d.original_name[:40]}")
            why = []
            if len({d.content_hash for d in group if d.content_hash}) == 1:
                why.append("один и тот же файл")
            if len({d.business_key for d in group if d.business_key}) == 1:
                why.append("вендор+сумма+валюта совпали")
            if len({d.doc_number for d in group if d.doc_number}) == 1:
                why.append("один номер документа")
            print(f"  причина: {', '.join(why) or 'прямые соседи'}")

            if apply:
                for d in group:
                    if d.id != winner.id:
                        d.merged_into_id = winner.id
                        merged += 1

        print(f"\nГрупп: {len(groups)}")
        if apply:
            print(f"Схлопнуто документов: {merged}")
        else:
            print("Это только показ. Чтобы применить: --apply")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
