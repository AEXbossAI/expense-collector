"""Сверка: что из разобранного уже лежит в папках компаний.

Фин директор раскладывает документы руками. Прежде чем выключать сухой
прогон, надо понять, сколько из них разборщик положит повторно, создав
дубль рядом с уже разложенным человеком файлом.

Сравнение по content_hash: имена файлов человек меняет, содержимое нет.

    docker exec expense-collector python -m app.scripts.compare_inbox
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document
from app.db.session import session_scope
from app.services.dropbox_client import get_dropbox

settings = get_settings()
MONTHS = ("September_2026", "August_2026")


async def main() -> None:
    dbx = get_dropbox()
    loop = asyncio.get_running_loop()

    existing: dict[str, tuple[str, str]] = {}
    for key, root in settings.dropbox_company_roots.items():
        for month in MONTHS:
            path = f"{root}/2026/{month}"
            try:
                res = await loop.run_in_executor(None, dbx._dbx.files_list_folder, path)
            except Exception as exc:
                print(f"  не открыть {path}: {str(exc)[:60]}")
                continue
            count = 0
            for entry in res.entries:
                h = getattr(entry, "content_hash", None)
                if h:
                    existing[h] = (key, entry.name)
                    count += 1
            print(f"  {key}/{month}: {count} файлов")

    print()
    async with session_scope() as session:
        docs = (
            await session.execute(
                select(Document)
                .where(Document.content_hash.isnot(None))
                .order_by(Document.id)
            )
        ).scalars().all()

    already = 0
    for doc in docs:
        hit = existing.get(doc.content_hash or "")
        vendor = (doc.vendor or "-")[:22]
        mark = {
            "filed": "разложить", "probable_duplicate": "дубль",
            "ignored": "не расход", "needs_review": "?",
        }.get(doc.status.value, doc.status.value)
        if doc.status_reason and doc.status_reason.startswith("[сухой"):
            mark = "разложить"
        if hit:
            already += 1
            print(f"  УЖЕ В ПАПКЕ  {doc.id:3d} [{mark:9s}] {vendor:22s} -> {hit[1][:38]}")
        else:
            print(f"  нет в папке  {doc.id:3d} [{mark:9s}] {vendor:22s}  {doc.amount} {doc.currency}")

    print(f"\n  всего к раскладке: {len(docs)}")
    print(f"  уже разложено человеком: {already}")
    print(f"  реально добавится: {len(docs) - already}")


if __name__ == "__main__":
    asyncio.run(main())
