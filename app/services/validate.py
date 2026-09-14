from __future__ import annotations

import re


def amount_present(amount: float | None, text: str | None) -> bool:
    """Сумма обязана буквально присутствовать в тексте документа.

    Проверка дешевле любой модели и ловит самый дорогой класс ошибок:
    правдоподобное, но выдуманное число. Проверяем обе записи
    десятичного разделителя и с разделителем разрядов, и без.

    Живёт отдельно от разборщика намеренно: чистая функция не должна
    тянуть за собой SDK модели, иначе её нельзя протестировать без
    установленного anthropic.
    """
    if amount is None or not text:
        return False
    candidates = {
        f"{amount:.2f}",
        f"{amount:.2f}".replace(".", ","),
        f"{amount:,.2f}",
        f"{amount:,.2f}".replace(",", " "),
        f"{amount:.0f}" if amount == int(amount) else None,
    }
    haystack = re.sub(r"\s+", " ", text)
    return any(c and c in haystack for c in candidates)
