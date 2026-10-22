"""Привязка расхода к проекту.

Каждый тест закрепляет вывод, полученный на боевых документах: четыре
инвойса подрядчика-статика от 08.08.2026 и справочник из 380 проектов.

Главное, что здесь проверяется, — не «нашли проект», а «не выбрали наугад».
Одна ошибочная привязка в P&L проекта дороже ста пустых ячеек.
"""

from __future__ import annotations

from app.services import projects


# Подмножество боевого справочника, включая обе настоящие пары-ловушки:
# два проекта со словом «Aqua sence» и два со словом «Eilat».
CATALOG = [
    (2776951902, "Orhot Tora"),
    (2729486131, "Amnir Project Hedera"),
    (1606975561, "Aqua sence Netanya"),
    (2974218550, "Aqua sence structure for mistor"),
    (2593498139, "Eilat project Plot 103-104-105-106"),
    (2911535808, "Hotel Eilat"),
    (2794889410, "Daniel Tower"),
    (3084545967, "Island"),
]

RATIO = 0.85


# ── код задачи: детерминированный уровень ─────────────────────────────────


def test_четыре_боевых_кода_распознаются():
    """Коды из инвойсов подрядчика-статика. Это колонка Drawings Num на
    доске TO DO LIST, по ней задача находится одним запросом."""
    text = (
        "AMR-SAN-AQU-SC-2026-07-1677: Aqua sence Netanya. Cladding: Static Calculation\n"
        "SHL-SAN-EIL-SC-2026-05-1674: Eilat project Plot 103-104-105-106\n"
        "ALO-ART-AMN-SC-2026-05-1673: Amnir Project Hedera\n"
        "MAA-SAN-ORH-SC-2026-07-1676: Orhot Tora\n"
    )
    codes = projects.find_task_codes(text)
    assert codes == [
        "AMR-SAN-AQU-SC-2026-07-1677",
        "SHL-SAN-EIL-SC-2026-05-1674",
        "ALO-ART-AMN-SC-2026-05-1673",
        "MAA-SAN-ORH-SC-2026-07-1676",
    ]


def test_мусор_не_принимается_за_код_задачи():
    """Всё это живьём встречается в тех же документах. Ложный код увёл бы
    расход на чужой проект, а это хуже отсутствия привязки."""
    junk = (
        "IL670143440000000287203 номер счёта в банке\n"
        "2026-09-30 срок\n"
        "PO26000463 номер заказа\n"
        "512354424 рег. номер\n"
        "AMR-SAN-AQU-SC-2026-07 без номера\n"
    )
    assert projects.find_task_codes(junk) == []


def test_код_ищется_и_в_имени_файла():
    assert projects.find_task_codes("SOM-ART-MER-SD-2026-05-6119 Merkaza Yarka.pdf") == [
        "SOM-ART-MER-SD-2026-05-6119"
    ]


def test_повторы_кода_схлопываются():
    text = "AMR-SAN-AQU-SC-2026-07-1677 ... AMR-SAN-AQU-SC-2026-07-1677"
    assert projects.find_task_codes(text, "amr-san-aqu-sc-2026-07-1677") == [
        "AMR-SAN-AQU-SC-2026-07-1677"
    ]


# ── нормализация ──────────────────────────────────────────────────────────


def test_подчёркивания_и_регистр_схлопываются():
    """Имя файла приходит как aqua_sence_40378_static.pdf, а проект на доске
    называется Aqua sence Netanya. Без нормализации не совпадёт ничто.

    В Python \\w включает подчёркивание, поэтому класс должен быть [\\W_].
    Этот тест поймал ровно эту ошибку до выката.
    """
    assert projects.normalize("aqua_sence_40378_static.pdf") == "aqua sence 40378 static pdf"


def test_различающие_слова_отбрасывают_общие():
    """«Project» есть у десятков проектов и потому не различает ничего."""
    assert projects.core_words("Amnir Project Hedera") == {"amnir", "hedera"}
    assert projects.core_words("Hotel Eilat") == {"eilat"}
    assert projects.core_words("Daniel Tower") == {"daniel"}


# ── матч по названию ──────────────────────────────────────────────────────


def test_полное_название_в_тексте_решает():
    """Текст инвойса содержит «Aqua sence Netanya» целиком. Второй проект
    со словом «Aqua sence» при этом не мешает: полное вхождение точнее."""
    hit = projects.match_project(
        "aqua_sence_40378_static.pdf",
        "AMR-SAN-AQU-SC-2026-07-1677: Aqua sence Netanya. Cladding: Static Calculation",
        CATALOG, RATIO,
    )
    assert hit.project_monday_id == 1606975561
    assert not hit.ambiguous


def test_одно_слово_из_названия_в_имени_файла_не_привязывает():
    """Регрессия 22.10.2026. Билеты `Flights - AZAL ... (Daniel Cheled).pdf`
    получили проект «Daniel Tower» по имени пассажира, и он попал на
    кнопки дайджеста ко всем вопросам. Одного слова из названия мало:
    в имени файла фин директор пишет имена людей."""
    for name in ("Flights - AZAL TLV-Baku 24-27.09 915.37 USD (Daniel Cheled) TKT.pdf",
                 "Flights - AZAL EMD 92.20 USD (Daniel Cheled).pdf",
                 "orlanda_40376_Amnir.pdf"):
        hit = projects.match_project(name, None, CATALOG, RATIO)
        assert hit.project_monday_id is None, name
        assert not hit.ambiguous, name


def test_опечатка_в_имени_файла_ловится_нечётко():
    """«Orhot Tota» вместо «Orhot Tora» — так файл и назвал человек."""
    hit = projects.match_project("orlanda_40379_Orhot Tota static.pdf", None, CATALOG, RATIO)
    assert hit.project_monday_id == 2776951902


def test_опечатка_в_нескольких_словах_тоже_ловится():
    """Ни одно различающее слово не совпадает дословно, работает только
    нечёткое сравнение окна той же длины."""
    assert projects.best_ratio("amnir project hedera", "amnr project hedra") >= RATIO
    hit = projects.match_project("Amnr Project Hedra.pdf", None, CATALOG, RATIO)
    assert hit.project_monday_id == 2729486131


def test_полное_название_на_два_проекта_даёт_неоднозначность():
    """Два кандидата означают пустое поле, а не догадку."""
    catalog = CATALOG + [(1, "Eilat")]
    hit = projects.match_project("orlanda_40375_Hotel Eilat.pdf", None, catalog, RATIO)
    assert hit.project_monday_id is None
    assert set(hit.ambiguous) == {"Hotel Eilat", "Eilat"}


def test_обычное_слово_из_текста_не_привязывает_к_проекту():
    """Регрессия 28.09.2026, самая дорогая за весь спек.

    Правило «различающее слово» применялось к тексту документа, а в
    названиях проектов сидят обычные английские слова. Прогон на боевых
    данных выдал чужой проект почти каждому накладному расходу:
    Anthropic -> «Port Market Tel Aviv», Tonystam -> «m.a. green city Ltd»,
    такси Bolt -> «Riding Tel-Aviv», конференция -> «Matiani Tbilisi».

    Слово «begin» в «Billing period begins» не делает подписку расходом
    по объекту «ALIZA BEGIN 10 Rishon LeZion».
    """
    catalog = CATALOG + [
        (1, "ALIZA BEGIN 10 Rishon LeZion"),
        (2, "Port Market Tel Aviv"),
        (3, "m.a. green city Ltd"),
    ]
    hit = projects.match_project(
        "Fwd-Invoice-Time-Doctor-Software_body.pdf",
        "Time Doctor LLC. Billing period begins on September 5. "
        "Green city office market rate applies.",
        catalog, RATIO,
    )
    assert hit.project_monday_id is None
    assert not hit.ambiguous


def test_однословное_название_в_тексте_не_ищется():
    """«Island» и «Factory» — реальные названия проектов. Искать их
    в тексте инвойса значит ловить любое употребление слова."""
    catalog = [(3084545967, "Island"), (2536843740, "Factory")]
    hit = projects.match_project(
        "Invoice-2945.pdf", "Delivered to the island factory warehouse", catalog, RATIO
    )
    assert hit.project_monday_id is None
    assert projects.is_distinctive("Island") is False
    assert projects.is_distinctive("Aqua sence Netanya") is True


def test_вхождение_считается_по_границам_слов():
    """Подстрочный поиск нашёл бы «island» внутри «Highlands»."""
    assert projects._contains_phrase("delivery to highlands", "island") is False
    assert projects._contains_phrase("delivery to the island", "island") is True


def test_однословное_название_в_имени_файла_работает():
    """В имени файла ограничения нет: его пишет человек и осознанно."""
    catalog = [(3084545967, "Island"), (2536843740, "Factory")]
    hit = projects.match_project("Island_invoice_2026-09.pdf", None, catalog, RATIO)
    assert hit.project_monday_id == 3084545967


def test_накладной_расход_не_матчится_ни_на_что():
    """Счёт Google Workspace не относится к объекту, и выдумывать привязку
    для него нельзя."""
    hit = projects.match_project(
        "Invoice-2945.pdf", "Google Workspace Business Starter for orlanda.info",
        CATALOG, RATIO,
    )
    assert hit.project_monday_id is None
    assert not hit.ambiguous


def test_пустой_справочник_не_роняет_матчер():
    hit = projects.match_project("Orhot Tora.pdf", None, [], RATIO)
    assert hit.project_monday_id is None


def test_ни_имени_ни_текста():
    hit = projects.match_project(None, None, CATALOG, RATIO)
    assert hit.project_monday_id is None
