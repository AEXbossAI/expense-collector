"""Разовое применение ответов директора от 14.09.2026.

  1-4 (Gamma, Wizz Air, Hertz Cyprus, Bolt) -> эстонская
  5   (Union Mobility)                      -> расход не берём

Gamma обрабатывается отдельно: оба файла фин директор уже положила в
папку компании руками. Повторная раскладка создала бы второй экземпляр —
реконсилера, который такое ловит, ещё нет (спек №3).

    docker exec expense-collector python -m app.scripts.apply_answers
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, Status
from app.db.session import session_scope
from app.services.dropbox_client import get_dropbox

settings = get_settings()

NOT_TAKEN = {"Union Mobility"}          # расход не берём вовсе
ALREADY_FILED = {"Gamma"}               # уже разложено вручную


async def main() -> None:
    dbx = get_dropbox()
    loop = asyncio.get_running_loop()

    # что реально лежит в папках компаний
    existing: dict[str, str] = {}
    for root in settings.dropbox_company_roots.values():
        for month in ("September_2026", "August_2026"):
            try:
                res = await loop.run_in_executor(
                    None, dbx._dbx.files_list_folder, f"{root}/2026/{month}"
                )
            except Exception:
                continue
            for e in res.entries:
                h = getattr(e, "content_hash", None)
                if h:
                    existing[h] = e.path_display

    async with session_scope() as session:
        docs = (
            await session.execute(
                select(Document).where(Document.status == Status.needs_review)
            )
        ).scalars().all()

        for doc in docs:
            vendor = doc.vendor or ""

            if any(v in vendor for v in NOT_TAKEN):
                doc.status = Status.ignored
                doc.status_reason = "не распределяем: расход не берём (решение директора 14.09.2026)"
                print(f"  {doc.id}  {vendor[:28]:30s} -> не распределяем")
                continue

            if any(v in vendor for v in ALREADY_FILED):
                path = existing.get(doc.content_hash or "")
                if path:
                    doc.company = settings.default_company
                    doc.dropbox_path = path
                    doc.status = Status.filed
                    doc.status_reason = "разложено фин директором вручную"
                    doc.resolved_at = datetime.now(timezone.utc)
                    print(f"  {doc.id}  {vendor[:28]:30s} -> привязан к {path.split('/')[-1][:34]}")
                    continue

            # остальные уходят на переразбор: умолчание разложит их само
            doc.status = Status.new
            doc.status_reason = None
            print(f"  {doc.id}  {vendor[:28]:30s} -> на переразбор (умолчание)")


if __name__ == "__main__":
    asyncio.run(main())
