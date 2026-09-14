"""Живая проверка ветки «PDF без текстового слоя → в модель».

На боевых данных эта ветка не выполнялась ни разу: у всех 29 реальных
PDF текстовый слой богатый (минимум 440 символов). Значит код мёртвый
и непроверенный, а выполнится он в самый неудобный момент — когда
придёт первый скан.

Собираем синтетический скан и прогоняем через настоящий путь разбора.

    docker exec expense-collector python -m app.scripts.check_vision
"""

from __future__ import annotations

import asyncio
import io

from app.services import extract, llm


def _scan_like_pdf() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    lines = [
        "TAX INVOICE",
        "",
        "Invoice to:",
        "Orlanda Engineering OU",
        "Reg. No. 16503223",
        "",
        "From: Kohvik Tallinn OU",
        "Date: 12.09.2026",
        "",
        "Business lunch, 2 persons      48.00",
        "VAT 24%                        11.52",
        "TOTAL EUR                      59.52",
    ]
    y = 120
    for line in lines:
        d.text((90, y), line, fill="black")
        y += 46

    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


async def main() -> None:
    data = _scan_like_pdf()
    print(f"синтетический скан: {len(data)} байт")

    text, file_data, mime_type = await extract.prepare(data, "application/pdf")
    print(f"текстовый слой: {len(text or '')} символов (порог {extract.MIN_TEXT_CHARS})")
    if file_data is None:
        print("ОШИБКА: документ не ушёл в модель, ветка не проверена")
        return
    print(f"уходит в модель как {mime_type}\n")

    parsed = await llm.extract_fields(file_data=file_data, mime_type=mime_type, text=text)
    print("что извлекла модель:")
    print(f"  документ расхода: {parsed.is_expense_document}")
    print(f"  компания:         {parsed.company}")
    print(f"  вендор:           {parsed.vendor}")
    print(f"  сумма:            {parsed.amount} {parsed.currency}")
    print(f"  дата:             {parsed.doc_date}")
    print(f"  ставка НДС:       {parsed.vat_rate}")
    print(f"  назначение:       {parsed.purpose}")

    ok = (
        parsed.is_expense_document
        and parsed.company == "orlanda_engineering"
        and parsed.amount == 59.52
        and parsed.currency == "EUR"
    )
    print("\nИТОГ:", "ветка vision работает" if ok else "ветка отработала, но поля разошлись")


if __name__ == "__main__":
    asyncio.run(main())
