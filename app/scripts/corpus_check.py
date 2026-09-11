"""Замер точности определения компании по размеченному корпусу.

В папках компаний лежат документы, разложенные человеком за месяцы работы.
Каждый файл в папке компании по определению принадлежит ей — это готовый
ground truth, полученный без ручной разметки.

Прогон даёт честную долю автоматического разбора ДО выката, а не обещание.

    docker exec expense-collector python -m app.scripts.corpus_check [N_на_компанию]
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import get_settings
from app.services import company, extract
from app.services.dropbox_client import get_dropbox

settings = get_settings()

YEARS = ("2026",)


async def _sample(dbx, root: str, limit: int) -> list[str]:
    """Пути к PDF в папках месяцев за указанные годы."""
    loop = asyncio.get_running_loop()
    paths: list[str] = []
    for year in YEARS:
        try:
            res = await loop.run_in_executor(None, dbx._dbx.files_list_folder, f"{root}/{year}")
        except Exception as exc:
            print(f"  не открыть {root}/{year}: {exc}")
            continue
        months = [e.path_display for e in res.entries if hasattr(e, "path_display") and not hasattr(e, "size")]
        for month in months:
            if len(paths) >= limit:
                return paths
            try:
                inner = await loop.run_in_executor(None, dbx._dbx.files_list_folder, month)
            except Exception:
                continue
            for entry in inner.entries:
                if getattr(entry, "size", None) and entry.name.lower().endswith(".pdf"):
                    paths.append(entry.path_display)
                    if len(paths) >= limit:
                        return paths
    return paths


async def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    dbx = get_dropbox()
    loop = asyncio.get_running_loop()

    totals = {"верно": 0, "unknown": 0, "ошибка": 0, "без текста": 0}

    for expected, root in settings.dropbox_company_roots.items():
        print(f"\n=== {expected}  ({root}) ===")
        paths = await _sample(dbx, root, limit)
        print(f"файлов в выборке: {len(paths)}\n")

        for path in paths:
            try:
                _, resp = await loop.run_in_executor(None, dbx._dbx.files_download, path)
                data = resp.content
            except Exception as exc:
                print(f"  СКАЧАТЬ НЕ СМОГ  {path.split('/')[-1][:50]}: {exc}")
                continue

            text = await extract.pdf_text(data)
            if len(text) < extract.MIN_TEXT_CHARS:
                totals["без текста"] += 1
                print(f"  [без текста] {path.split('/')[-1][:55]}  ({len(text)} симв.)")
                continue

            got, reason = company.detect(text)
            if got == expected:
                totals["верно"] += 1
                mark = "OK   "
            elif got == company.UNKNOWN:
                totals["unknown"] += 1
                mark = "?    "
            else:
                totals["ошибка"] += 1
                mark = "ОШИБКА"
            print(f"  [{mark}] {path.split('/')[-1][:55]}  <- {reason}")

    total = sum(totals.values())
    print("\n" + "=" * 60)
    for k, v in totals.items():
        share = f"{v / total * 100:.0f}%" if total else "-"
        print(f"  {k:12s} {v:3d}  {share}")
    decided = totals["верно"] + totals["ошибка"]
    if decided:
        print(f"\n  точность среди решённых: {totals['верно'] / decided * 100:.1f}%")
    if total:
        print(f"  доля без участия модели: {totals['верно'] / total * 100:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
