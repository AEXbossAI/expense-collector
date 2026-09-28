"""Пересчёт суммы документа в евро.

Два источника и ровно одно правило выбора между ними:

* **ECB** (через api.frankfurter.dev) — всё, что он покрывает. Отдаёт
  «единиц валюты за 1 EUR», поэтому пересчёт — ДЕЛЕНИЕ.
* **Нацбанк Грузии** — всё остальное. Публикует курсы В ЛАРИ примерно для
  сорока валют, то есть «лари за 1 единицу валюты». Направление
  ПРОТИВОПОЛОЖНОЕ, пересчёт — кросс через лари.

НБГ здесь не «источник для лари», а универсальный запасной источник. Это не
теоретическая красота: спек писался по данным на 24.09.2026, где валют было
четыре, а на 28.09 их стало пять — приехали восемь чеков Bolt из Баку в AZN,
которого в наборе ECB нет так же, как нет GEL. Следующая командировка
добавит шестую валюту, и она заработает без правки кода.

Перепутанное направление курса — самая дорогая ошибка в этом файле.
Типографский счёт на 1720 GEL при курсе 2.9927 это 575 EUR, а не 5147.
Ошибка тихая и правдоподобная, поэтому направление закреплено тестами
на каждую валюту.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import FxRate
from app.db.session import session_scope

logger = logging.getLogger(__name__)
settings = get_settings()

ECB = "ecb"
NBG = "nbg"
EUR = "eur"

# Валюта, в которой НБГ публикует всё остальное. Сам лари он не котирует:
# на `?currencies=GEL` отдаёт пустой массив, потому что лари стоит один лари.
NBG_BASE = "GEL"

_CENT = Decimal("0.01")

# Один пулленный клиент на процесс. AsyncHTTPTransport(retries=3)
# переподключается на сбоях уровня соединения (DNS, TCP, TLS) — ровно тех,
# что иначе роняют один документ на ровном месте. Всё, что дальше рукопожатия,
# разгребает очередь monday_pushes, а не транспорт.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=settings.fx_timeout_s,
            transport=httpx.AsyncHTTPTransport(retries=3),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class RateUnavailable(Exception):
    """Курс получить не удалось. Вызывающий обязан оставить сумму пустой,
    а не подставить «примерно на сегодня»."""


@dataclass(frozen=True)
class Converted:
    amount_eur: Decimal
    rate: Decimal
    rate_date: date
    source: str


@dataclass(frozen=True)
class Quote:
    """Котировка как её отдал провайдер, до всякого пересчёта."""

    currency: str
    rate: Decimal
    rate_date: date
    source: str


# ── чистые функции: здесь живёт вся арифметика и все тесты ────────────────


def normalize_currency(currency: str | None) -> str | None:
    if not currency:
        return None
    code = currency.strip().upper()
    return code if code.isalpha() and len(code) == 3 else None


def nbg_needs_request(currency: str) -> bool:
    """Нужно ли вообще спрашивать у НБГ курс этой валюты.

    Лари спрашивать нельзя: он база, и на `?currencies=GEL` НБГ отдаёт
    пустой массив. Без этой ветки грузинские расходы не пересчитывались
    вовсе — два чека уехали бы на доску без суммы. Поймано сухим прогоном
    на боевых данных 28.09.2026; юнит-тест не поймал, потому что подавал
    единицу прямо в формулу и путь получения курса не трогал.
    """
    return currency != NBG_BASE


def eur_from_ecb(amount: Decimal, rate: Decimal) -> Decimal:
    """ECB: rate — сколько единиц валюты дают за 1 EUR. 3.4812 ILS за евро."""
    if rate <= 0:
        raise ValueError(f"Курс ECB должен быть положительным, получен {rate}")
    return (amount / rate).quantize(_CENT, rounding=ROUND_HALF_UP)


def eur_from_nbg(amount: Decimal, rate_currency_gel: Decimal, rate_eur_gel: Decimal) -> Decimal:
    """НБГ: оба курса выражены в лари за единицу валюты, поэтому кросс.

    1 AZN = 1.5343 GEL, 1 EUR = 2.9927 GEL -> 7.90 AZN = 7.90 * 1.5343 / 2.9927.
    Для самого лари rate_currency_gel = 1 и формула вырождается в деление на
    курс евро. Отдельной ветки для GEL нет намеренно: одна формула — один
    класс ошибок вместо двух.
    """
    if rate_currency_gel <= 0 or rate_eur_gel <= 0:
        raise ValueError(
            f"Курсы НБГ должны быть положительными, получены {rate_currency_gel} и {rate_eur_gel}"
        )
    return (amount * rate_currency_gel / rate_eur_gel).quantize(_CENT, rounding=ROUND_HALF_UP)


def effective_rate(rate_eur_gel: Decimal, rate_currency_gel: Decimal) -> Decimal:
    """Сколько единиц валюты дают за 1 EUR — то же, что отдаёт ECB.

    Кросс НБГ считается через лари, но наружу такой курс отдавать нельзя:
    строка «4.90 AZN @ 1.5343» предлагает разделить и получить 3.19, тогда
    как в сумме стоит 2.53. Курс, который не объясняет цифру рядом с собой,
    хуже отсутствующего. Поэтому храним и показываем эффективный:
    делением на него сумма в евро получается всегда.
    """
    if rate_currency_gel <= 0 or rate_eur_gel <= 0:
        raise ValueError("курсы НБГ должны быть положительными")
    return (rate_eur_gel / rate_currency_gel).quantize(Decimal("0.00000001"))


def parse_ecb(payload: dict, currency: str) -> Quote | None:
    """Ответ frankfurter: {"date": "2026-09-18", "rates": {"ILS": 3.4812}}.

    Дата берётся из ответа, а не из запроса: провайдер сам откатывается на
    последний опубликованный день, и документ от субботы честно получает
    пятничный курс. Проверено: запрос на 2026-09-19 вернул 2026-09-18.
    """
    rates = (payload or {}).get("rates") or {}
    raw = rates.get(currency)
    if raw is None:
        return None
    try:
        quoted = datetime.strptime(str(payload["date"])[:10], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return None
    return Quote(currency=currency, rate=Decimal(str(raw)), rate_date=quoted, source=ECB)


def parse_nbg(payload: list, currency: str) -> Quote | None:
    """Ответ НБГ: [{"date": "...", "currencies": [{"code": "AZN",
    "quantity": 1, "rate": 1.5343, ...}]}].

    quantity — за сколько единиц валюты указан курс. У большинства валют это
    1, но у слабых (йена, донг) бывает 100 или 1000, и без деления курс
    завышается в сто раз. Проверяется тестом.
    """
    if not payload:
        return None
    block = payload[0]
    try:
        quoted = datetime.strptime(str(block["date"])[:10], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return None
    for row in block.get("currencies") or []:
        if str(row.get("code", "")).upper() != currency:
            continue
        rate = Decimal(str(row.get("rate")))
        quantity = Decimal(str(row.get("quantity") or 1))
        if quantity <= 0:
            return None
        return Quote(
            currency=currency, rate=rate / quantity, rate_date=quoted, source=NBG
        )
    return None


# ── обращение к провайдерам и кэш ─────────────────────────────────────────


async def _fetch_ecb(currency: str, on: date) -> Quote | None:
    url = f"{settings.fx_ecb_url.rstrip('/')}/{on.isoformat()}"
    resp = await _get_client().get(url, params={"base": "EUR", "symbols": currency})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return parse_ecb(resp.json(), currency)


async def _fetch_nbg(currency: str, on: date) -> Quote | None:
    resp = await _get_client().get(
        settings.fx_nbg_url, params={"currencies": currency, "date": on.isoformat()}
    )
    resp.raise_for_status()
    return parse_nbg(resp.json(), currency)


async def _cached(currency: str, on: date, source: str) -> Quote | None:
    """Кэш по ПРОСИМОЙ дате был бы неверен: провайдер откатывается назад, и
    две разные просимые даты дают одну фактическую. Поэтому кэш хранит
    фактическую дату, а промах по ней — обычное дело в выходные."""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(FxRate).where(
                    FxRate.rate_date == on,
                    FxRate.currency == currency,
                    FxRate.source == source,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return Quote(
        currency=currency, rate=Decimal(str(row.rate)), rate_date=row.rate_date, source=source
    )


async def _remember(quote: Quote) -> None:
    async with session_scope() as session:
        exists = (
            await session.execute(
                select(FxRate.id).where(
                    FxRate.rate_date == quote.rate_date,
                    FxRate.currency == quote.currency,
                    FxRate.source == quote.source,
                )
            )
        ).first()
        if exists:
            return
        session.add(
            FxRate(
                rate_date=quote.rate_date,
                currency=quote.currency,
                rate=quote.rate,
                source=quote.source,
            )
        )


async def _nbg_quote(currency: str, on: date) -> Quote:
    """Курс валюты в лари. Для самого лари — единица, без запроса."""
    if not nbg_needs_request(currency):
        return Quote(currency=NBG_BASE, rate=Decimal(1), rate_date=on, source=NBG)

    cached = await _cached(currency, on, NBG)
    if cached is not None:
        return cached

    try:
        found = await _fetch_nbg(currency, on)
    except Exception as exc:
        logger.warning("НБГ не ответил по %s на %s: %s", currency, on, exc)
        found = None

    if found is None:
        raise RateUnavailable(f"НБГ не знает {currency} на {on.isoformat()}")

    await _remember(found)
    return found


async def quote(currency: str, on: date) -> Quote:
    """Курс валюты на дату. Сначала ECB, потом НБГ, иначе RateUnavailable."""
    cached = await _cached(currency, on, ECB)
    if cached is not None:
        return cached
    cached = await _cached(currency, on, NBG)
    if cached is not None:
        return cached

    try:
        found = await _fetch_ecb(currency, on)
    except Exception as exc:
        logger.warning("ECB не ответил по %s на %s: %s", currency, on, exc)
        found = None

    if found is not None:
        await _remember(found)
        return found

    try:
        return await _nbg_quote(currency, on)
    except RateUnavailable:
        raise RateUnavailable(
            f"курс {currency} на {on.isoformat()} не найден ни в ECB, ни в НБГ"
        )


async def to_eur(amount: Decimal | float | None, currency: str | None, on: date) -> Converted:
    """Сумма в евро. Бросает RateUnavailable — сумма НЕ выдумывается."""
    code = normalize_currency(currency)
    if amount is None or code is None:
        raise RateUnavailable(f"нечего пересчитывать: {amount!r} {currency!r}")

    value = Decimal(str(amount))
    if code == "EUR":
        return Converted(
            amount_eur=value.quantize(_CENT, rounding=ROUND_HALF_UP),
            rate=Decimal(1),
            rate_date=on,
            source=EUR,
        )

    # Лари: курса «лари в лари» не существует, спрашивать нечего. Пересчёт
    # сводится к делению на курс евро, то есть выглядит и хранится ровно
    # как у ECB — «сколько единиц валюты за 1 EUR».
    if code == NBG_BASE:
        eur_in_gel = await _nbg_quote("EUR", on)
        return Converted(
            amount_eur=eur_from_nbg(value, Decimal(1), eur_in_gel.rate),
            rate=eur_in_gel.rate,
            rate_date=eur_in_gel.rate_date,
            source=NBG,
        )

    found = await quote(code, on)
    if found.source == ECB:
        return Converted(
            amount_eur=eur_from_ecb(value, found.rate),
            rate=found.rate,
            rate_date=found.rate_date,
            source=ECB,
        )

    # НБГ: нужен ещё курс евро в лари на ту же дату, иначе кросс не собрать.
    eur_in_gel = await _nbg_quote("EUR", found.rate_date)
    return Converted(
        amount_eur=eur_from_nbg(value, found.rate, eur_in_gel.rate),
        # Наружу отдаём ЭФФЕКТИВНЫЙ курс: сколько единиц валюты за 1 EUR.
        # Сырой курс НБГ («лари за манат») строку «4.90 AZN @ 1.5343» делает
        # ложью: разделив, получишь 3.19, а в сумме стоит 2.53. Курс,
        # который не объясняет цифру рядом с собой, хуже отсутствующего.
        rate=effective_rate(eur_in_gel.rate, found.rate),
        rate_date=found.rate_date,
        source=NBG,
    )


async def warmup(currencies: list[str], on: date) -> None:
    """Прогрев кэша перед пачкой документов: один запрос на валюту вместо
    запроса на документ. Ошибки глотаются — это оптимизация, а не путь."""
    for code in {c for c in (normalize_currency(c) for c in currencies) if c and c != "EUR"}:
        try:
            await quote(code, on)
        except Exception:
            logger.debug("Прогрев курса %s на %s не удался", code, on)
        await asyncio.sleep(0)
