"""Аудит имён папок месяцев в обеих компаниях.

Соглашение: <корень>/<ГГГГ>/<Месяц_ГГГГ>, месяц по-английски полностью.
Отклонения ломают автоматическую раскладку: система создаст рядом
правильную папку, и документы за один месяц окажутся в двух местах.

    python -m app.scripts.audit_folders          # показать
    python -m app.scripts.audit_folders --fix    # переименовать
"""
from __future__ import annotations

import asyncio
import sys

from app.core.config import get_settings
from app.core.naming import _MONTHS_EN
from app.services.dropbox_client import get_dropbox

settings = get_settings()


async def main() -> None:
    fix = "--fix" in sys.argv
    dbx = get_dropbox()
    loop = asyncio.get_running_loop()

    for key, root in settings.dropbox_company_roots.items():
        print(f"\n=== {key} ({root}) ===")
        try:
            years = await loop.run_in_executor(None, dbx._dbx.files_list_folder, root)
        except Exception as exc:
            print(f"  не открыть: {str(exc)[:70]}")
            continue

        for y in years.entries:
            name = y.name
            if not (name.isdigit() and len(name) == 4):
                continue
            try:
                res = await loop.run_in_executor(None, dbx._dbx.files_list_folder, y.path_display)
            except Exception:
                continue
            for entry in res.entries:
                if getattr(entry, "size", None) is not None:
                    continue
                expected = {f"{m}_{name}" for m in _MONTHS_EN}
                if entry.name in expected:
                    continue
                # пытаемся понять, какой это месяц
                guess = next((f"{m}_{name}" for m in _MONTHS_EN
                              if entry.name.lower().startswith(m.lower())), None)
                print(f"  ОТКЛОНЕНИЕ  {name}/{entry.name}" + (f"  -> {guess}" if guess else "  (не опознан)"))
                if fix and guess:
                    target = f"{y.path_display}/{guess}"
                    try:
                        await loop.run_in_executor(None, dbx._dbx.files_move_v2,
                                                   entry.path_display, target)
                        print(f"              переименовано в {guess}")
                    except Exception as exc:
                        print(f"              НЕ УДАЛОСЬ: {str(exc)[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
