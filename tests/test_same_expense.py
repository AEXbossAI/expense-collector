"""Какие документы схлопываются на доске в один расход.

До 21.10.2026 хватало совпадения вендора, суммы и валюты, и синк склеил
26 пар разных расходов из 28. Каждый случай ниже взят с прода.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.db.models import DocKind
from app.services import dedupe

KEY = "anthropic pbc|22.32|EUR"


@dataclass
class _D:
    id: int
    doc_date: date | None
    doc_kind: DocKind | None = DocKind.invoice
    doc_number: str | None = None
    business_key: str | None = KEY
    vendor: str | None = "Anthropic, PBC"
    content_hash: str | None = None
    also_received_from: str | None = None


def _same(a, b, blocked=frozenset()):
    return dedupe.same_expense(a, b, set(blocked))


def test_подписка_за_соседние_месяцы_не_дубль():
    """Док 18 и 172: сентябрьский счёт прятался за октябрьской квитанцией."""
    sep = _D(18, date(2026, 9, 7), DocKind.invoice, "MJG2ZWNE0003")
    oct_ = _D(172, date(2026, 10, 7), DocKind.receipt, "MJG2ZWNE0004")
    assert not _same(sep, oct_)


def test_подписка_без_номеров_тоже_разводится_датой():
    assert not _same(_D(1, date(2026, 9, 1), None), _D(2, date(2026, 10, 1), None))


def test_счёт_и_квитанция_одной_оплаты_дубль():
    """Gamma, док 6 и 7: номера разные законно, тип разный, день один."""
    inv = _D(6, date(2026, 9, 1), DocKind.invoice, "FCAXANXU0004")
    rec = _D(7, date(2026, 9, 1), DocKind.receipt, "21060524")
    assert _same(inv, rec)


def test_два_счёта_одного_дня_с_разными_номерами_не_дубль():
    """EasyStaff 165–167: та же сумма, тот же день, три счёта."""
    a = _D(165, date(2026, 10, 6), DocKind.invoice, "ES-332393")
    b = _D(166, date(2026, 10, 6), DocKind.invoice, "ES-332394")
    assert not _same(a, b)


def test_два_билета_одного_рейса_не_дубль():
    a = _D(220, date(2026, 10, 17), DocKind.receipt, "7712440193142")
    b = _D(223, date(2026, 10, 17), DocKind.receipt, "7712440193141")
    assert not _same(a, b)


def test_номер_из_списка_номеров_совпадает():
    many = _D(222, date(2026, 10, 17), DocKind.receipt, "7714561714860, 7714561714862")
    one = _D(1, date(2026, 10, 17), DocKind.receipt, "7714561714862")
    other = _D(224, date(2026, 10, 17), DocKind.receipt, "7714561714861")
    assert _same(many, one)
    assert not _same(many, other)


def test_номер_сравнивается_без_дефисов():
    assert _same(_D(1, date(2026, 10, 7), doc_number="MJG2ZWNE-0004"),
                 _D(2, date(2026, 10, 7), doc_number="mjg2zwne0004"))


def test_тот_же_номер_у_вендора_дубль_и_вне_окна():
    """Счёт Tonystam 3008 пришёл двумя письмами."""
    a = _D(209, date(2026, 10, 15), DocKind.invoice, "3008")
    b = _D(210, date(2026, 10, 25), DocKind.receipt, "3008")
    assert _same(a, b)


def test_пара_названная_человеком_разной_не_склеивается():
    """Док 3 и 4: финдиректор ответила «разные», синк склеил всё равно."""
    a = _D(3, date(2026, 8, 31), None)
    b = _D(4, date(2026, 8, 31), None)
    assert _same(a, b)
    assert not _same(a, b, {frozenset((3, 4))})


def test_тот_же_файл_дубль_всегда():
    a = _D(42, date(2026, 9, 15), content_hash="h", doc_number="0PREY7")
    b = _D(44, date(2026, 9, 15), content_hash="h", doc_number="0PREY7")
    assert _same(a, b, {frozenset((42, 44))})


def test_без_даты_по_ключу_не_склеивается():
    assert not _same(_D(1, None), _D(2, date(2026, 9, 1)))


def test_окно_дат():
    w = dedupe.settings.dedup_date_window_days
    a = _D(1, date(2026, 9, 1))
    assert _same(a, _D(2, date(2026, 9, 1 + w)))
    assert not _same(a, _D(3, date(2026, 9, 2 + w)))


# ── ответы «разные» и поиск вопроса о дубле ──────────────────────────────


@dataclass
class _Answer:
    document_id: int
    answer: str


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _Result(self.rows)


async def test_последний_ответ_и_пара_из_also_received_from():
    docs = [_D(149, None, also_received_from="дубль 147"),
            _D(166, None, also_received_from="дубль 165"),
            _D(172, None, also_received_from="дубль 171")]
    answers = [_Answer(149, "different"),
               _Answer(166, "same"), _Answer(166, "different"),
               _Answer(172, "same")]
    pairs = await dedupe.said_different(_Session(answers), docs)
    assert pairs == {frozenset((149, 147)), frozenset((166, 165))}


async def test_вопрос_о_дубле_не_задаётся_для_разных_номеров():
    doc = _D(166, date(2026, 10, 6), DocKind.invoice, "ES-332394")
    other_invoice = _D(165, date(2026, 10, 6), DocKind.invoice, "ES-332393")
    receipt = _D(170, date(2026, 10, 6), DocKind.receipt, "R-1")
    session = _Session([other_invoice])
    assert await dedupe.find_business(session, doc, "EasyStaff", 1.0, "EUR", doc.doc_date) is None
    session = _Session([other_invoice, receipt])
    assert await dedupe.find_business(session, doc, "EasyStaff", 1.0, "EUR", doc.doc_date) is receipt
