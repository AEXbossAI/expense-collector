"""Сколько символов даёт текстовый слой у реальных PDF.

Порог MIN_TEXT_CHARS решает, уйдёт документ в vision или разберётся
бесплатно. Поставлен на глаз, здесь измеряется на боевых данных.

    docker exec expense-collector python -m app.scripts.text_layer_stats
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db.models import Document, Status
from app.db.session import session_scope
from app.services import extract
from app.services.dropbox_client import get_dropbox


async def main() -> None:
    dbx = get_dropbox()
    async with session_scope() as session:
        docs = (
            await session.execute(
                select(Document).where(
                    Document.status == Status.filed,
                    Document.dropbox_path.isnot(None),
                )
            )
        ).scalars().all()

    rows: list[tuple[int, str, str]] = []
    for doc in docs:
        name = (doc.original_name or "").lower()
        if (doc.mime_type or "") != "application/pdf" and not name.endswith(".pdf"):
            continue
        try:
            data = await dbx.download(doc.dropbox_path)
        except Exception as exc:
            print(f"  не скачать {doc.id}: {str(exc)[:50]}")
            continue
        text = await extract.pdf_text(data)
        rows.append((len(text), doc.vendor or "-", doc.original_name[:40]))

    rows.sort()
    print(f"PDF проверено: {len(rows)}   текущий порог: {extract.MIN_TEXT_CHARS}\n")

    print("самые бедные текстовым слоем:")
    for n, vendor, name in rows[:8]:
        mark = "-> VISION" if n < extract.MIN_TEXT_CHARS else ""
        print(f"  {n:6d} симв.  {vendor[:22]:24s} {name}  {mark}")

    print("\nраспределение:")
    buckets = ((0, 50), (50, 100), (100, 300), (300, 1000), (1000, 10**9))
    for lo, hi in buckets:
        count = sum(1 for n, _, _ in rows if lo <= n < hi)
        label = f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"
        bar = "#" * count
        print(f"  {label:>10}: {count:3d} {bar}")

    below = sum(1 for n, _, _ in rows if n < extract.MIN_TEXT_CHARS)
    print(f"\nуходит в vision при текущем пороге: {below} из {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
