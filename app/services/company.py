from __future__ import annotations

import logging
import re

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

UNKNOWN = "unknown"

# Регистрационный номер и VAT — чистые цифры и латиница, поэтому их не
# трогает ни развал RTL при извлечении иврита, ни вариативность написания
# названия ("OÜ" то есть, то нет). Это самый надёжный признак.
_ID_LIKE = re.compile(r"^[A-Z]{0,2}\d{6,}$")


def _normalize(text: str) -> str:
    """Схлопываем пробелы: в извлечённом PDF они рвут строки в случайных
    местах, и 'Orlanda  Engineering' не совпадёт с 'Orlanda Engineering'."""
    return re.sub(r"\s+", " ", text)


def detect(text: str) -> tuple[str, str]:
    """Определение компании по тексту документа.

    Возвращает (ключ компании, причина). Ключ UNKNOWN означает, что
    документ должен уйти человеку, а не быть разложенным наугад.
    """
    if not text:
        return UNKNOWN, "пустой текст"

    haystack = _normalize(text)
    haystack_lower = haystack.lower()

    hits: dict[str, list[str]] = {}

    for key, tokens in settings.company_legal_names.items():
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            found = (
                token in haystack
                if _ID_LIKE.match(token) or not token.isascii()
                else token.lower() in haystack_lower
            )
            if found:
                hits.setdefault(key, []).append(token)

    # Решает ТОЛЬКО регистрационный номер или VAT.
    #
    # Совпадение по названию решать не имеет права: существуют межфирменные
    # документы, где одна компания группы выставляет счёт другой. В таком
    # документе название эстонской компании присутствует, но она поставщик,
    # а не покупатель. На размеченном корпусе (28 документов) правило
    # "название решает" дало 2 ошибки из 4 срабатываний, то есть монетку,
    # а правило "решает только идентификатор" — 18 верных и ни одной ошибки.
    strong = {k: [t for t in v if _ID_LIKE.match(t)] for k, v in hits.items()}
    strong = {k: v for k, v in strong.items() if v}

    if len(strong) == 1:
        key, tokens = next(iter(strong.items()))
        return key, f"найден идентификатор {tokens[0]}"

    if len(strong) > 1:
        detail = "; ".join(f"{k}: {', '.join(v)}" for k, v in strong.items())
        return UNKNOWN, f"идентификаторы обеих компаний ({detail})"

    if hits:
        detail = "; ".join(f"{k}: {', '.join(v)}" for k, v in hits.items())
        return UNKNOWN, f"только название, без идентификатора ({detail}) — решает модель"

    return UNKNOWN, "ни одного известного идентификатора в тексте"


def name_hints(text: str) -> dict[str, list[str]]:
    """Совпадения по названию — подсказка для модели, не основание.

    Модель получает их вместе с документом и решает сама, чьё это
    название: покупателя или поставщика.
    """
    if not text:
        return {}
    haystack = _normalize(text)
    haystack_lower = haystack.lower()
    out: dict[str, list[str]] = {}
    for key, tokens in settings.company_legal_names.items():
        for token in tokens:
            token = token.strip()
            if not token or _ID_LIKE.match(token):
                continue
            found = token in haystack if not token.isascii() else token.lower() in haystack_lower
            if found:
                out.setdefault(key, []).append(token)
    return out


def vat_matches(company: str, vat_rate: float | None) -> bool | None:
    """Перекрёстная проверка: 24% у эстонской, 18% у израильской.

    Возвращает None, если проверить нечем. Не блокирует раскладку —
    встречаются документы других юрисдикций (видели грузинский инвойс),
    но расхождение стоит записать в причину и показать человеку.
    """
    if vat_rate is None:
        return None
    # Ноль — законная ставка, а не расхождение. Трансграничные B2B-счета
    # идут по reverse charge с НДС 0%: Slack, Anthropic, Time Doctor,
    # болгарский телеком — всё это в живом ящике пришло с нулём.
    # Проверено на боевых данных: без этого исключения проверка
    # заблокировала бы 9 документов из 20.
    if vat_rate == 0:
        return None
    expected = {"orlanda_engineering": 24.0, "orlanda_israel": 18.0}.get(company)
    if expected is None:
        return None
    return abs(vat_rate - expected) < 0.5
