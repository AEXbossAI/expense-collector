"""Сборка строки расхода для доски Monday.

Весь файл — чистые функции над документом: ни сети, ни базы. Так решение
«что именно поедет на доску» проверяется тестом, а не глазами по факту.

Доска `🟣 5. Invoices` целиком на английском, поэтому названия колонок и
все варианты заполнения тоже английские — новые колонки не должны выбиваться
из существующих. Все id колонок приходят из `MONDAY_COLUMNS`, а не зашиты
здесь: правка колонки на доске не должна требовать релиза.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date
from decimal import Decimal

from app.core import naming
from app.db.models import DocKind, Document, Source

# Ключи наших полей. Колонка адресуется ключом, id берётся из конфига.
AMOUNT_EUR = "amount_eur"
CURRENCY = "currency"
DOC_DATE = "doc_date"
PAYMENT_DATE = "payment_date"
DOC_TYPE = "doc_type"
INTERNAL_NO = "internal_no"
PROJECT = "project"
TASKS = "tasks"
SERVICE = "service"
SOURCE_FILE = "source_file"
BILLING_MONTH = "billing_month"
VENDOR = "vendor"
PAYER = "payer"
AMOUNT_ORIGINAL = "amount_original"
CATEGORY = "category"
DOC_ID = "doc_id"
SOURCE_COL = "source"

# Какого типа обязана быть колонка под каждое наше поле.
#
# Проверяется на старте синка. Тип важнее существования: запись значения
# дропдауна в status-колонку Monday либо отвергнет, либо запишет мусор, и
# обнаружится это не в логе, а глазами на доске через неделю. Перепутать
# легко — id колонок похожи, а `color_*` это status, не цвет.
EXPECTED_TYPES = {
    AMOUNT_EUR: "numbers",
    CURRENCY: "status",
    DOC_DATE: "date",
    PAYMENT_DATE: "date",
    DOC_TYPE: "status",
    INTERNAL_NO: "text",
    PROJECT: "board_relation",
    TASKS: "board_relation",
    SERVICE: "board_relation",
    SOURCE_FILE: "link",
    BILLING_MONTH: "text",
    VENDOR: "text",
    PAYER: "status",
    AMOUNT_ORIGINAL: "text",
    CATEGORY: "dropdown",
    DOC_ID: "text",
    SOURCE_COL: "status",
}

# Лейблы заводятся на доске руками один раз: синк никогда не создаёт их сам
# (create_labels_if_missing=false), потому что опечатка в разборе не должна
# плодить мусор на доске, которой пользуется вся команда.
PAYER_LABELS = {
    "orlanda_engineering": "Orlanda Engineering OÜ",
    "orlanda_israel": "Orlanda Israel",
}

DOC_TYPE_LABELS = {
    DocKind.invoice: "Invoice",
    DocKind.receipt: "Receipt",
    DocKind.credit_note: "Credit note",
    DocKind.proforma: "Proforma",
}

SOURCE_LABELS = {
    Source.mail: "Email",
    Source.telegram: "Telegram",
    Source.manual: "Manual",
}

# Monday режет имя айтема на 255; 100 берём ради того, чтобы строка
# читалась в свёрнутой группе без горизонтальной прокрутки.
NAME_LIMIT = 100


def dropbox_url(path: str | None) -> str | None:
    """Ссылка на файл в веб-клиенте Dropbox.

    Форма совпадает с уже работающими ссылками в группе `In` на этой доске.
    Проверено 28.09.2026: оба корня компаний видны из-под сервисного
    аккаунта в командном namespace, ссылка открывается.
    """
    if not path:
        return None
    return "https://www.dropbox.com/home" + urllib.parse.quote(path)


def item_name(doc: Document) -> str:
    """`2026-09-07 · Anthropic, PBC · Claude Pro subscription`

    Дата первой, потому что группа месяца сортируется по ней глазами.
    Неизвестные части заменяются заглушкой, а не выкидываются: иначе
    позиции разъезжаются и строки перестают читаться столбиком.
    """
    when = doc.doc_date.isoformat() if doc.doc_date else "no date"
    vendor = (doc.vendor or "no vendor").strip()
    purpose = (doc.purpose or "").strip()
    parts = [when, vendor] + ([purpose] if purpose else [])
    name = " · ".join(parts)
    return name[: NAME_LIMIT - 1] + "…" if len(name) > NAME_LIMIT else name


def group_title(doc: Document, prefix: str) -> str:
    """Ключ группы совпадает с именем папки Dropbox (`September_2026`),
    чтобы доска и папка сверялись глазами без пересчётов."""
    when = doc.doc_date or doc.received_at.date()
    return f"{prefix}{naming.month_folder(_as_datetime(when))}"


def _as_datetime(value: date):
    from datetime import datetime

    return datetime(value.year, value.month, 1)


def _money(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def original_amount(doc: Document) -> str | None:
    """`1770.00 ILS @ 3.4812 (ecb, 2026-09-18)` — одной строкой, без формул.

    Через полгода цифру в евро должно быть чем объяснить, не поднимая базу.
    """
    if doc.amount is None or not doc.currency:
        return None
    text = f"{_money(doc.amount)} {doc.currency.upper()}"
    if doc.fx_rate is not None and doc.fx_source and doc.fx_source != "eur":
        when = doc.fx_rate_date.isoformat() if doc.fx_rate_date else "?"
        text += f" @ {Decimal(str(doc.fx_rate)).normalize()} ({doc.fx_source}, {when})"
    return text


def fields(doc: Document) -> dict[str, object]:
    """Наши поля -> значения в формате Monday, ещё без привязки к id колонок.

    Поля со значением None сюда не попадают вовсе: пустое поле на доске и
    поле, которое мы не смогли заполнить, — это одно и то же, и затирать
    чужое значение пустотой мы права не имеем.
    """
    out: dict[str, object] = {}

    if doc.amount_eur is not None:
        out[AMOUNT_EUR] = _money(doc.amount_eur)
    if doc.currency:
        out[CURRENCY] = {"label": doc.currency.upper()}
    if doc.doc_date:
        out[DOC_DATE] = {"date": doc.doc_date.isoformat()}
    # Дата оплаты проставляется только у квитанции: у счёта её попросту нет,
    # а догадываться о дате оплаты по дате счёта — врать.
    if doc.doc_kind == DocKind.receipt and doc.doc_date:
        out[PAYMENT_DATE] = {"date": doc.doc_date.isoformat()}
    if doc.doc_kind in DOC_TYPE_LABELS:
        out[DOC_TYPE] = {"label": DOC_TYPE_LABELS[doc.doc_kind]}
    if doc.doc_number:
        out[INTERNAL_NO] = doc.doc_number
    if doc.project_monday_id:
        # Колонка Projects принимает ровно один элемент: allowMultipleItems
        # у неё не выставлен, в отличие от Source tasks. Остальные проекты
        # многопроектного счёта уходят в Update айтема.
        out[PROJECT] = {"item_ids": [str(doc.project_monday_id)]}
    if doc.task_monday_id:
        out[TASKS] = {"item_ids": [str(doc.task_monday_id)]}
    if doc.service_monday_id:
        out[SERVICE] = {"item_ids": [str(doc.service_monday_id)]}

    url = dropbox_url(doc.dropbox_path)
    if url:
        out[SOURCE_FILE] = {"url": url, "text": doc.original_name[:60]}

    when = doc.doc_date or doc.received_at.date()
    out[BILLING_MONTH] = naming.month_folder(_as_datetime(when))

    if doc.vendor:
        out[VENDOR] = doc.vendor[:255]
    if doc.company in PAYER_LABELS:
        out[PAYER] = {"label": PAYER_LABELS[doc.company]}
    original = original_amount(doc)
    if original:
        out[AMOUNT_ORIGINAL] = original
    if doc.category:
        # Dropdown, а не status: категорий четырнадцать, и status на такой
        # набор превращается в радугу из нечитаемых плашек. Прецедент на
        # этой же доске — «Customer (from invoice)» с 39 значениями.
        out[CATEGORY] = {"labels": [doc.category]}
    out[DOC_ID] = str(doc.id)
    if doc.source in SOURCE_LABELS:
        out[SOURCE_COL] = {"label": SOURCE_LABELS[doc.source]}

    return out


def writable(
    proposed: dict[str, object],
    last_written: dict | None,
    current_on_board: dict[str, str] | None,
    columns: dict[str, str],
) -> dict[str, object]:
    """Что из предложенного нам позволено записать.

    Правило одно: поле пишется, если на доске оно ПУСТО либо совпадает с тем,
    что мы писали в прошлый раз. Всё остальное — правка человека, и она
    сильнее нас. Фин директор, поправившая сумму руками, не должна
    обнаружить её затёртой через две минуты.

    `columns` отображает наши ключи в id колонок; ключ без id пропускается
    молча — колонку могли ещё не завести.
    """
    if current_on_board is None:
        return dict(proposed)

    last_written = last_written or {}
    allowed: dict[str, object] = {}

    for key, value in proposed.items():
        column_id = columns.get(key)
        if not column_id:
            continue
        on_board = (current_on_board.get(column_id) or "").strip()
        if not on_board:
            allowed[key] = value
            continue
        if _same(on_board, last_written.get(key)):
            allowed[key] = value

    return allowed


def _same(board_text: str, written: object) -> bool:
    """Сравнение текста с доски с тем, что мы записали.

    Monday отдаёт всё как текст, поэтому сравнивать надо по тексту, а не по
    структуре: `{"label": "EUR"}` на доске выглядит как `EUR`, а dropdown —
    как перечисление через запятую.
    """
    if written is None:
        return False
    if isinstance(written, dict):
        if "labels" in written:
            return board_text == ", ".join(str(x) for x in written["labels"])
        for field in ("label", "date", "text", "url"):
            if field in written:
                return board_text == str(written[field])
        if "item_ids" in written:
            # Monday отдаёт relation именами связанных элементов, а мы
            # пишем идентификаторами. Сопоставить нечем, поэтому считаем
            # непустое значение на доске правкой человека и не трогаем.
            return False
        return False
    return board_text == str(written)


def column_values(
    values: dict[str, object], columns: dict[str, str]
) -> tuple[str, dict[str, object]]:
    """JSON для Monday плюс слепок записанного, который ляжет в
    `monday_last_written`. Ключи без id колонки пропускаются молча."""
    payload: dict[str, object] = {}
    written: dict[str, object] = {}
    for key, value in values.items():
        column_id = columns.get(key)
        if not column_id:
            continue
        payload[column_id] = value
        written[key] = value
    return json.dumps(payload, ensure_ascii=False), written


def update_body(doc: Document, extra_projects: list[str], duplicates: list[str]) -> str | None:
    """Текст в ленту айтема: то, чему не нашлось колонки.

    Два случая, и оба иначе выглядят как потеря данных: остальные проекты
    многопроектного счёта и вторые экземпляры того же расхода.
    """
    lines: list[str] = []
    if extra_projects:
        lines.append("Expense covers several projects, amount is not split:")
        lines.extend(f"• {p}" for p in extra_projects)
    if doc.task_codes:
        lines.append(f"Task codes: {doc.task_codes}")
    if duplicates:
        lines.append("Also received:")
        lines.extend(f"• {d}" for d in duplicates)
    return "\n".join(lines) if lines else None
