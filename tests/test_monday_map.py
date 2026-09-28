"""Сборка строки расхода для доски Monday.

Ни сети, ни базы: вся логика «что именно поедет на доску» — чистые функции,
и проверяется она здесь, а не глазами по факту на боевой доске.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from app.db.models import DocKind, Document, ProjectSource, Source
from app.services import monday_map


# Отображение наших ключей в id колонок — как в MONDAY_COLUMNS на боевой доске.
COLUMNS = {
    monday_map.AMOUNT_EUR: "numeric__1",
    monday_map.CURRENCY: "color_mm69asdm",
    monday_map.DOC_DATE: "date__1",
    monday_map.PAYMENT_DATE: "date_mm69a728",
    monday_map.DOC_TYPE: "color_mm69mgpx",
    monday_map.INTERNAL_NO: "text_mm69n4aj",
    monday_map.PROJECT: "board_relation_mm6chnqv",
    monday_map.TASKS: "board_relation_mm6cqt3m",
    monday_map.SOURCE_FILE: "link_mm69vhd6",
    monday_map.BILLING_MONTH: "text_mm6cvz81",
    monday_map.DOC_ID: "text_docid",
    monday_map.VENDOR: "text_vendor",
}


def make_doc(**kw) -> Document:
    base = dict(
        id=42,
        source=Source.mail,
        source_key="k",
        original_name="Invoice-2945.pdf",
        received_at=datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc),
        doc_date=date(2026, 9, 7),
        vendor="Anthropic, PBC",
        purpose="Подписка Claude Pro",
        amount=Decimal("22.32"),
        currency="EUR",
        amount_eur=Decimal("22.32"),
        company="orlanda_engineering",
        dropbox_path="/Finance/AMS Group_accouting/2026/September_2026/file.pdf",
    )
    base.update(kw)
    return Document(**base)


# ── имя айтема ────────────────────────────────────────────────────────────


def test_имя_айтема_начинается_с_даты():
    """Группа месяца читается столбиком, и дата первой делает её
    сортируемой глазами."""
    assert monday_map.item_name(make_doc()) == "2026-09-07 · Anthropic, PBC · Подписка Claude Pro"


def test_неизвестные_части_имени_заменяются_заглушкой():
    """Выкидывать их нельзя: позиции разъедутся и строки перестанут
    читаться столбиком."""
    name = monday_map.item_name(make_doc(doc_date=None, vendor=None, purpose=None))
    assert name == "no date · no vendor"


def test_длинное_имя_обрезается():
    name = monday_map.item_name(make_doc(purpose="о" * 300))
    assert len(name) <= monday_map.NAME_LIMIT
    assert name.endswith("…")


def test_иврит_в_вендоре_не_ломает_имя():
    doc = make_doc(vendor='פן ושות\' עורכי-דין ונוטריון', purpose="Юридические услуги")
    assert "עורכי" in monday_map.item_name(doc)


# ── значения колонок ──────────────────────────────────────────────────────


def test_валюта_едет_лейблом_а_сумма_в_евро():
    """В колонке Amount зашит символ евро, поэтому туда только EUR.
    Оригинал живёт отдельной строкой."""
    values = monday_map.fields(make_doc(currency="ILS", amount=Decimal("1770"),
                                        amount_eur=Decimal("508.45")))
    assert values[monday_map.AMOUNT_EUR] == "508.45"
    assert values[monday_map.CURRENCY] == {"label": "ILS"}


def test_оригинал_суммы_объясняет_цифру_в_евро():
    """Через полгода пересчёт должно быть чем объяснить, не поднимая базу."""
    doc = make_doc(currency="GEL", amount=Decimal("1720"), amount_eur=Decimal("574.73"),
                   fx_rate=Decimal("2.9927"), fx_source="nbg", fx_rate_date=date(2026, 9, 18))
    assert monday_map.original_amount(doc) == "1720.00 GEL @ 2.9927 (nbg, 2026-09-18)"


def test_у_евро_курс_в_строку_не_пишется():
    assert monday_map.original_amount(make_doc()) == "22.32 EUR"


def test_дата_оплаты_только_у_квитанции():
    """У счёта даты оплаты попросту нет, и догадываться о ней по дате
    счёта — врать."""
    receipt = monday_map.fields(make_doc(doc_kind=DocKind.receipt))
    invoice = monday_map.fields(make_doc(doc_kind=DocKind.invoice))
    assert receipt[monday_map.PAYMENT_DATE] == {"date": "2026-09-07"}
    assert monday_map.PAYMENT_DATE not in invoice
    assert invoice[monday_map.DOC_TYPE] == {"label": "Invoice"}


def test_проект_едет_одним_элементом():
    """Колонка Projects настроена на один элемент: allowMultipleItems у неё
    не выставлен, в отличие от Source tasks."""
    values = monday_map.fields(make_doc(project_monday_id=2776951902,
                                        project_name="Orhot Tora",
                                        project_source=ProjectSource.task_code))
    assert values[monday_map.PROJECT] == {"item_ids": ["2776951902"]}


def test_источник_привязки_на_доску_не_едет():
    """Решение владельца: колонки «как определили проект» на доске нет.
    Поле остаётся в базе — оно нужно в сухом прогоне и чтобы отличить
    ответ человека от догадки матчера, но фин директору на доске не нужно."""
    values = monday_map.fields(make_doc(project_monday_id=2776951902,
                                        project_source=ProjectSource.task_code))
    assert not any("source" in k and "project" in k for k in values)


def test_без_проекта_колонка_не_заполняется():
    """Пустой проект — норма для накладного расхода. Затирать чужое
    значение пустотой мы права не имеем, поэтому ключа просто нет."""
    assert monday_map.PROJECT not in monday_map.fields(make_doc())


def test_пустые_поля_не_попадают_в_значения():
    """Затирать чужое значение пустотой мы права не имеем, поэтому None
    не доезжает до доски вовсе."""
    values = monday_map.fields(make_doc(amount=None, amount_eur=None, currency=None, vendor=None))
    assert monday_map.AMOUNT_EUR not in values
    assert monday_map.CURRENCY not in values
    assert monday_map.VENDOR not in values


def test_месяц_совпадает_с_именем_папки_dropbox():
    values = monday_map.fields(make_doc())
    assert values[monday_map.BILLING_MONTH] == "September_2026"
    assert monday_map.group_title(make_doc(), "Expenses ") == "Expenses September_2026"


def test_ссылка_на_dropbox_экранируется():
    url = monday_map.dropbox_url("/Finance/AMS Group_accouting/2026/September_2026/a b.pdf")
    assert url == (
        "https://www.dropbox.com/home/Finance/AMS%20Group_accouting/2026/September_2026/a%20b.pdf"
    )


def test_ссылки_нет_если_файла_нет():
    """Запись из чата без документа: расход есть, файла нет."""
    assert monday_map.dropbox_url(None) is None
    assert monday_map.SOURCE_FILE not in monday_map.fields(make_doc(dropbox_path=None))


# ── сборка payload ────────────────────────────────────────────────────────


def test_ключи_без_колонки_пропускаются_молча():
    """Колонку могли ещё не завести руками. Это не повод не записать
    остальное."""
    payload, written = monday_map.column_values(
        {monday_map.AMOUNT_EUR: "10.00", "выдуманное_поле": "x"}, COLUMNS
    )
    assert json.loads(payload) == {"numeric__1": "10.00"}
    assert "выдуманное_поле" not in written


def test_payload_валиден_как_json_с_кириллицей():
    payload, _ = monday_map.column_values(monday_map.fields(make_doc()), COLUMNS)
    restored = json.loads(payload)
    assert restored["text_vendor"] == "Anthropic, PBC"
    assert restored["text_docid"] == "42"


def test_у_каждого_поля_объявлен_тип_колонки():
    """Синк сверяет типы колонок на старте. Поле, у которого тип не
    объявлен, проскочит проверку молча и запишется не туда — например
    дропдаун в status-колонку."""
    doc = make_doc(
        doc_kind=DocKind.receipt,
        doc_number="40378",
        category="Taxi & local transport",
        project_monday_id=1,
        task_monday_id=2,
        service_monday_id=3,
    )
    emitted = set(monday_map.fields(doc))
    assert emitted <= set(monday_map.EXPECTED_TYPES), (
        emitted - set(monday_map.EXPECTED_TYPES)
    )


def test_категория_едет_дропдауном_одним_значением():
    """На доске у колонки limit_select=1: ровно одна категория на строку."""
    values = monday_map.fields(make_doc(category="Taxi & local transport"))
    assert values[monday_map.CATEGORY] == {"labels": ["Taxi & local transport"]}
    assert monday_map.EXPECTED_TYPES[monday_map.CATEGORY] == "dropdown"


# ── защита правок человека ────────────────────────────────────────────────


def test_пустое_поле_на_доске_можно_заполнить():
    allowed = monday_map.writable(
        {monday_map.VENDOR: "Anthropic, PBC"}, {}, {"text_vendor": ""}, COLUMNS
    )
    assert monday_map.VENDOR in allowed


def test_поле_поправленное_человеком_не_перезаписывается():
    """Фин директор, поправившая проект руками, не должна обнаружить его
    затёртым через две минуты."""
    allowed = monday_map.writable(
        {monday_map.VENDOR: "Anthropic, PBC"},
        {monday_map.VENDOR: "Anthropic"},
        {"text_vendor": "Anthropic PBC исправлено"},
        COLUMNS,
    )
    assert allowed == {}


def test_наше_же_значение_можно_перезаписать():
    """На доске ровно то, что мы писали в прошлый раз — значит человек
    не трогал, и обновить можно."""
    allowed = monday_map.writable(
        {monday_map.VENDOR: "Anthropic, PBC"},
        {monday_map.VENDOR: "Anthropic"},
        {"text_vendor": "Anthropic"},
        COLUMNS,
    )
    assert allowed[monday_map.VENDOR] == "Anthropic, PBC"


def test_лейбл_сравнивается_по_тексту():
    """Monday отдаёт status-колонку текстом, а мы пишем словарём."""
    allowed = monday_map.writable(
        {monday_map.CURRENCY: {"label": "EUR"}},
        {monday_map.CURRENCY: {"label": "EUR"}},
        {"color_mm69asdm": "EUR"},
        COLUMNS,
    )
    assert monday_map.CURRENCY in allowed


def test_новый_айтем_пишется_целиком():
    """Айтема на доске ещё нет, сравнивать не с чем."""
    values = {monday_map.VENDOR: "x", monday_map.CURRENCY: {"label": "EUR"}}
    assert monday_map.writable(values, None, None, COLUMNS) == values


# ── лента айтема ──────────────────────────────────────────────────────────


def test_несколько_проектов_уходят_в_ленту():
    """Колонка принимает один проект, но остальные не должны потеряться."""
    doc = make_doc(task_codes="ALO-ART-AMN-SC-2026-05-1673, MAA-SAN-ORH-SC-2026-07-1676")
    body = monday_map.update_body(doc, ["Orhot Tora"], [])
    assert "amount is not split" in body
    assert "Orhot Tora" in body
    assert "MAA-SAN-ORH-SC-2026-07-1676" in body


def test_схлопнутые_дубли_видны_человеку():
    body = monday_map.update_body(make_doc(), [], ["Invoice-2945.pdf (telegram)"])
    assert "Also received" in body
    assert "Invoice-2945.pdf" in body


def test_ленты_нет_когда_нечего_сказать():
    assert monday_map.update_body(make_doc(), [], []) is None
