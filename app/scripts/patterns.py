"""Отчёт закономерностей в ответах человека.

Показывает и согласные случаи, и противоречивые. Отчёт, который прячет
разногласия, подталкивает вывести правило там, где его нет.

Правила отсюда НЕ создаются: это решение человека, а не счётчика.

    docker exec expense-collector python -m app.scripts.patterns
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

from sqlalchemy import select

from app.db.models import RoutingAnswer, RoutingRule
from app.db.session import session_scope

TITLES = {
    "orlanda_engineering": "эстонская",
    "orlanda_israel": "израильская",
    "skip": "не распределять",
    "same": "один расход",
    "different": "разные",
}


def _report(rows: list[RoutingAnswer], field: str, label: str) -> None:
    groups: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        key = getattr(row, field)
        if key:
            groups[key][row.answer] += 1

    if not groups:
        print(f"  по {label} ответов пока нет")
        return

    for key, counts in sorted(groups.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counts.values())
        if len(counts) == 1:
            answer, n = next(iter(counts.items()))
            mark = "разногласий нет" if n > 1 else "один ответ"
            print(f"  {key[:30]:32s} -> {TITLES.get(answer, answer):16s} {n} ответ(ов), {mark}")
        else:
            detail = ", ".join(f"{n} {TITLES.get(a, a)}" for a, n in counts.most_common())
            print(f"  {key[:30]:32s} -> {detail}   <- правило не выводится")


async def main() -> None:
    async with session_scope() as session:
        answers = (await session.execute(select(RoutingAnswer))).scalars().all()
        rules = (await session.execute(select(RoutingRule))).scalars().all()

    company_answers = [a for a in answers if a.question == "company"]
    dup_answers = [a for a in answers if a.question == "duplicate"]

    print(f"Ответов всего: {len(answers)}  "
          f"(о компании: {len(company_answers)}, о дублях: {len(dup_answers)})\n")

    print("По вендорам (вопрос о компании):")
    _report(company_answers, "vendor_normalized", "вендорам")

    print("\nПо отправителям (вопрос о компании):")
    _report(company_answers, "sender", "отправителям")

    print("\nПо вендорам (вопрос о дублях):")
    _report(dup_answers, "vendor_normalized", "вендорам")

    print(f"\nАктивных правил: {sum(1 for r in rules if r.active)}")
    for rule in rules:
        state = "активно" if rule.active else "выключено"
        print(f"  {rule.kind}: {rule.pattern} -> {rule.company}  ({state})")

    print("\nПравила создаются только явным решением человека,")
    print("ни счётчик ответов, ни отсутствие разногласий их не включают.")


if __name__ == "__main__":
    asyncio.run(main())
