"""Сухой прогон синка: что именно завелось бы на доске.

Смотреть глазами ДО того, как это случится. Тот же приём, что с
PARSER_DRY_RUN в спеке №2: раскладку на боевых документах сначала
показывают, потом она происходит.

    docker exec expense-collector python -m app.scripts.monday_dry_run
    docker exec expense-collector python -m app.scripts.monday_dry_run --push

`--push` выполняет реальный прогон очереди — это и есть ручной бэкфилл
сентября. Автоматически при старте контейнера он не запускается намеренно:
перезапуск не должен переливать историю заново.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, Status
from app.db.session import session_scope
from app.services import dedupe, fx, monday_map
from app.tasks import monday_sync

settings = get_settings()


async def _show() -> None:
    since = monday_sync.reconcile_from()
    print(f"Доска {settings.monday_board_id}, документы с {since.isoformat()}\n")

    async with session_scope() as session:
        docs = (
            await session.execute(
                select(Document)
                .where(
                    Document.status == Status.filed,
                    Document.merged_into_id.is_(None),
                    Document.doc_date >= since,
                )
                .order_by(Document.doc_date, Document.id)
            )
        ).scalars().all()

        if not docs:
            print("Нечего показывать: подходящих документов нет.")
            return

        seen: set[int] = set()
        rows = 0
        skipped: list[str] = []

        for doc in docs:
            if doc.id in seen:
                continue
            group = await dedupe.group_of(session, doc)
            winner = dedupe.choose_representative(group)
            for other in group:
                seen.add(other.id)
                if other.id != winner.id:
                    skipped.append(
                        f"  {other.id:4d} {other.original_name[:44]:46s} "
                        f"-> схлопнут в {winner.id}"
                    )

            when = doc.doc_date or doc.received_at.date()
            try:
                converted = await fx.to_eur(winner.amount, winner.currency, when)
                money = f"{converted.amount_eur} EUR"
                if converted.source != "eur":
                    money += f"  ({winner.amount} {winner.currency} @ {converted.rate} " \
                             f"{converted.source} {converted.rate_date})"
            except fx.RateUnavailable as exc:
                money = f"БЕЗ СУММЫ ({exc})"

            print(f"[{winner.company}] {monday_map.item_name(winner)}")
            print(f"    сумма   : {money}")
            print(f"    проект  : {winner.project_name or '—'} "
                  f"({winner.project_source.value if winner.project_source else 'нет'})")
            print(f"    категория: {winner.category or '—'}   "
                  f"тип: {winner.doc_kind.value if winner.doc_kind else '—'}   "
                  f"№ {winner.doc_number or '—'}")
            print(f"    файл    : {monday_map.dropbox_url(winner.dropbox_path)}")

            values = monday_map.fields(winner)
            unmapped = [k for k in values if k not in settings.monday_columns]
            if unmapped:
                print(f"    НЕКУДА ПИСАТЬ: {', '.join(sorted(unmapped))}")
            print()
            rows += 1

        print(f"Итого строк на доске: {rows}")
        if skipped:
            print(f"\nНе поедут (не представители групп дублей): {len(skipped)}")
            for line in skipped:
                print(line)


async def _push() -> None:
    if settings.monday_dry_run:
        print("MONDAY_DRY_RUN=true — реальный прогон запрещён конфигом.")
        return
    total = 0
    while True:
        done = await monday_sync.tick()
        total += done
        if not done:
            break
        print(f"  ... записано {total}")
    print(f"Готово, документов записано: {total}")


def main() -> None:
    if "--push" in sys.argv:
        asyncio.run(_push())
    else:
        asyncio.run(_show())


if __name__ == "__main__":
    main()
