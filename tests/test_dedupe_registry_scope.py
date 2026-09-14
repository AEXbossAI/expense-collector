from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.db.models import Source
from app.services import dedupe, registry
from app.tasks import reconciler
from app.services.validate import amount_present as _amount_present


# ── дедупликация ──────────────────────────────────────────────────────────


def test_вариации_написания_вендора_схлопываются():
    assert dedupe.normalize_vendor("Tonystam LLC") == dedupe.normalize_vendor("TONYSTAM, Inc.")
    assert dedupe.normalize_vendor("Orlanda Engineering OÜ") == "orlanda engineering"


def test_бизнес_ключ_без_даты():
    """Дата сравнивается окном, поэтому в ключ не входит."""
    a = dedupe.business_key("Tonystam LLC", 348.30, "EUR")
    b = dedupe.business_key("TONYSTAM inc", 348.30, "eur")
    assert a == b == "tonystam|348.30|EUR"


def test_ключ_не_строится_без_суммы_или_валюты():
    assert dedupe.business_key("Tonystam", None, "EUR") is None
    assert dedupe.business_key("Tonystam", 10.0, None) is None
    assert dedupe.business_key(None, 10.0, "EUR") is None


@dataclass
class _Doc:
    mime_type: str | None
    size_bytes: int | None


def test_pdf_побеждает_фотографию():
    """Иначе first-write-wins оставит худшую копию: фото с бликами
    вместо PDF с текстовым слоем."""
    pdf = _Doc("application/pdf", 20_000)
    photo = _Doc("image/jpeg", 900_000)
    assert dedupe.prefer(photo, pdf) is pdf
    assert dedupe.prefer(pdf, photo) is pdf


# ── проверка суммы ────────────────────────────────────────────────────────


def test_сумма_обязана_быть_в_тексте():
    """Самый дорогой класс ошибок — правдоподобное, но выдуманное число."""
    assert _amount_present(348.30, "Total 348.30 EUR") is True
    assert _amount_present(348.30, "Total 999.99 EUR") is False


def test_сумма_с_запятой_как_разделителем():
    assert _amount_present(348.30, "Итого 348,30 EUR") is True


def test_сумма_с_разделителем_разрядов():
    assert _amount_present(5025.32, "Total 5,025.32 ILS") is True


def test_целая_сумма_без_копеек():
    assert _amount_present(500.0, "Total 500 USD") is True


def test_пустой_текст_не_подтверждает():
    assert _amount_present(10.0, None) is False
    assert _amount_present(None, "10.00") is False


# ── область сверки реконсилера ────────────────────────────────────────────


def test_месяц_в_области():
    assert reconciler._month_in_scope("September_2026") is True
    assert reconciler._month_in_scope("October_2026") is True


def test_август_вне_области():
    """Решение владельца: август не трогаем."""
    assert reconciler._month_in_scope("August_2026") is False
    assert reconciler._month_in_scope("December_2025") is False


def test_не_месячная_папка_вне_области():
    assert reconciler._month_in_scope("Feb 2025 facebook") is False
    assert reconciler._month_in_scope("KMD") is False


def test_путь_вне_области_не_считается_удалённым():
    """Документ отсутствует в выборке не потому, что его удалили,
    а потому что мы туда не смотрели. Без этой проверки сухой прогон
    пометил удалёнными 4 корректно разложенных документа."""
    assert reconciler._path_in_scope(
        "/Finance/AMS Group_accouting/2026/September_2026/x.pdf") is True
    assert reconciler._path_in_scope(
        "/Finance/AMS Group_accouting/2026/August_2026/x.pdf") is False


# ── реестр ────────────────────────────────────────────────────────────────


@dataclass
class _Row:
    id: int = 1
    dropbox_path: str | None = "/x/2026-09-01_Slack_283.97-EUR_ab.pdf"
    original_name: str = "x.pdf"
    doc_date: date | None = date(2026, 9, 1)
    vendor: str | None = "Slack Technologies"
    purpose: str | None = "Подписка; тариф Pro"
    amount: float | None = 283.97
    currency: str | None = "EUR"
    source: Source = Source.mail
    month_from_receipt: bool = False
    belongs_to_closed_month: str | None = None
    status_reason: str | None = None
    service_period: str | None = None


def test_csv_начинается_с_bom():
    """Без BOM Excel в русской локали читает файл как ANSI."""
    assert registry.build_csv([_Row()]).startswith(b"\xef\xbb\xbf")


def test_разделитель_точка_с_запятой():
    """С запятой Excel в русской локали покажет всё одной колонкой."""
    text = registry.build_csv([_Row()]).decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Файл;Дата документа;")


def test_точка_с_запятой_внутри_значения_экранируется():
    text = registry.build_csv([_Row()]).decode("utf-8-sig")
    assert '"Подписка; тариф Pro"' in text


def test_сумма_с_запятой_для_русского_excel():
    text = registry.build_csv([_Row()]).decode("utf-8-sig")
    assert "283,97" in text


def test_есть_строка_итого():
    text = registry.build_csv([_Row(), _Row(id=2, amount=16.03)]).decode("utf-8-sig")
    assert "ИТОГО;;;;300,00;EUR" in text


def test_источник_по_русски():
    text = registry.build_csv([_Row(source=Source.manual)]).decode("utf-8-sig")
    assert "Вручную" in text


def test_пометка_проверить_попадает_в_примечания():
    row = _Row(status_reason="разложено вручную; проверить: сумма не найдена в тексте")
    text = registry.build_csv([row]).decode("utf-8-sig")
    assert "ПРОВЕРИТЬ: сумма не найдена в тексте" in text
