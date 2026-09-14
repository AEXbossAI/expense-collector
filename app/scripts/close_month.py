"""Закрытие месяца.

После закрытия поздние документы не дописываются в сданный отчёт молча:
они кладутся в текущий месяц с пометкой «относится к ГГГГ-ММ», и уходит
уведомление. Без этого доверие к реестру заканчивается на первом же
случае, когда система тихо изменила уже сданные цифры.

    python -m app.scripts.close_month list
    python -m app.scripts.close_month close orlanda_engineering August_2026
    python -m app.scripts.close_month open  orlanda_engineering August_2026
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import MonthState
from app.db.session import session_scope


async def _list() -> None:
    async with session_scope() as session:
        rows = (await session.execute(select(MonthState).order_by(MonthState.id))).scalars().all()
    if not rows:
        print("Закрытых месяцев нет.")
        return
    for r in rows:
        state = f"закрыт {r.closed_at:%d.%m.%Y}" if r.closed_at else "открыт"
        print(f"  {r.company:22s} {r.month:16s} {state}")


async def _set(company: str, month: str, closed: bool) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(MonthState).where(
                    MonthState.company == company, MonthState.month == month
                )
            )
        ).scalar_one_or_none()
        stamp = datetime.now(timezone.utc) if closed else None
        if row is None:
            session.add(MonthState(company=company, month=month, closed_at=stamp))
        else:
            row.closed_at = stamp
    print(f"{company} / {month}: {'закрыт' if closed else 'открыт'}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        asyncio.run(_list())
    elif cmd in {"close", "open"} and len(sys.argv) == 4:
        asyncio.run(_set(sys.argv[2], sys.argv[3], cmd == "close"))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
