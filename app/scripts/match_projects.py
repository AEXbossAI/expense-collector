"""Замер матчера проектов на боевых данных.

Порог PROJECT_MATCH_MIN_RATIO и правило «различающее слово» должны быть
подтверждены числами, а не на слово. Скрипт прогоняет матчер по всем
собранным документам против всех проектов доски и печатает четыре группы:
по коду, по названию, неоднозначно, промах.

Неоднозначность — не провал. «eilat» из `orlanda_40375_static eilat.pdf`
совпадает и с `Eilat project Plot 103-104-105-106`, и с `Hotel Eilat`, и
правильный исход здесь — вопрос человеку, а не выбор наугад.

    docker exec expense-collector python -m app.scripts.match_projects
    docker exec expense-collector python -m app.scripts.match_projects --refresh
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, ParseAttempt, Status
from app.db.session import session_scope
from app.services import projects

settings = get_settings()


async def main(refresh: bool) -> None:
    async with session_scope() as session:
        if refresh:
            count = await projects.refresh_projects(session, force=True)
            print(f"Справочник обновлён: {count} проектов\n")

        catalog = await projects.cached_projects(session)
        if not catalog:
            print("Справочник проектов пуст. Запустите с --refresh.")
            return

        docs = (
            await session.execute(
                select(Document)
                .where(Document.status.in_([Status.filed, Status.needs_review]))
                .order_by(Document.id)
            )
        ).scalars().all()

        texts: dict[int, str] = {}
        for row in (
            await session.execute(
                select(ParseAttempt.document_id, ParseAttempt.extracted_text)
                .where(ParseAttempt.extracted_text.is_not(None))
            )
        ).all():
            texts.setdefault(row[0], row[1] or "")

        hit, ambiguous, miss, by_code = [], [], [], []

        for doc in docs:
            codes = projects.find_task_codes(doc.original_name, texts.get(doc.id), doc.raw_caption)
            if codes:
                by_code.append((doc, codes))
                continue

            found = projects.match_project(
                doc.original_name, texts.get(doc.id), catalog,
                settings.project_match_min_ratio,
            )
            if found.project_monday_id:
                hit.append((doc, found))
            elif found.ambiguous:
                ambiguous.append((doc, found))
            else:
                miss.append((doc, found))

        print(f"Проектов в справочнике: {len(catalog)}")
        print(f"Документов проверено:   {len(docs)}")
        print(f"  по коду задачи:       {len(by_code)}")
        print(f"  по названию:          {len(hit)}")
        print(f"  неоднозначно:         {len(ambiguous)}")
        print(f"  без совпадения:       {len(miss)}")
        print(f"  порог:                {settings.project_match_min_ratio}")

        if by_code:
            print("\n── по коду задачи (детерминированно) ──")
            for doc, codes in by_code:
                print(f"  {doc.id:4d} {doc.original_name[:46]:48s} {', '.join(codes)}")

        if hit:
            print("\n── по названию ──")
            for doc, found in hit:
                print(f"  {doc.id:4d} {doc.original_name[:46]:48s} -> {found.project_name}")
                print(f"       {found.reason}")

        if ambiguous:
            print("\n── неоднозначно (вопрос человеку, это правильный исход) ──")
            for doc, found in ambiguous:
                print(f"  {doc.id:4d} {doc.original_name[:46]:48s} {found.reason}")
                print(f"       кандидаты: {', '.join(found.ambiguous[:6])}")

        if miss:
            print(f"\n── без совпадения: {len(miss)} документов ──")
            print("   (для накладных расходов это норма, а не промах)")


if __name__ == "__main__":
    asyncio.run(main("--refresh" in sys.argv))
