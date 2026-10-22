"""Поездки относятся к месяцу поездки, а не оплаты (фин директор, 22.10.2026).

Примеры взяты с прода: билеты на октябрь, оплаченные в сентябре, лежали
в `September_2026/+ invoices REVOLUT` и считались сентябрём.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.core import naming
from app.db.models import Document, Source, Status
from app.services import filer, monday_map, registry
from app.tasks import parser

ROOT = naming.settings.dropbox_company_roots["orlanda_engineering"].rstrip("/")
INBOX = naming.settings.dropbox_inbox_path.rstrip("/")
SEPT = f"{ROOT}/2026/September_2026"
REVOLUT = f"{SEPT}/+ invoices REVOLUT"
TRAVEL = naming.TRAVEL_CATEGORY


def _doc(**kw) -> Document:
    base = dict(
        id=122, source=Source.manual, source_key="k", original_name="Tus Air 192.09 EUR.pdf",
        received_at=datetime(2026, 9, 29, tzinfo=timezone.utc), doc_date=date(2026, 9, 29),
        company="orlanda_engineering", category=TRAVEL, travel_date=date(2026, 10, 14),
        dropbox_path=f"{REVOLUT}/Tus Air 192.09 EUR.pdf", status=Status.filed,
    )
    base.update(kw)
    return Document(**base)


# ── месяц расхода ─────────────────────────────────────────────────────────


def test_билет_на_октябрь_в_папке_сентября_это_октябрь():
    assert naming.expense_month(_doc()) == "October_2026"


def test_поездка_без_даты_по_месяцу_оплаты():
    assert naming.expense_month(_doc(travel_date=None)) == "September_2026"
    inbox = _doc(travel_date=None, dropbox_path=f"{INBOX}/October_2026/a.pdf",
                 doc_date=date(2026, 8, 30))
    assert naming.expense_month(inbox) == "August_2026"


def test_дата_поездки_у_не_поездки_ничего_не_решает():
    """Выставка BAU 2027 — Training & events, остаётся в месяце папки."""
    assert naming.expense_month(_doc(category="Training & events")) == "September_2026"


def test_поездка_в_закрытый_месяц_остаётся_в_месяце_папки():
    doc = _doc(travel_date=date(2026, 8, 20), belongs_to_closed_month="August_2026")
    assert naming.expense_month(doc) == "September_2026"


def test_поездка_в_будущий_месяц():
    assert naming.expense_month(_doc(travel_date=date(2027, 1, 11))) == "January_2027"


def test_не_поездка_как_раньше_по_папке():
    doc = _doc(category="Software & subscriptions", travel_date=None, doc_date=date(2026, 8, 31))
    assert naming.expense_month(doc) == "September_2026"


def test_папка_от_месяца():
    assert naming.folder_in_month(f"{REVOLUT}/a.pdf", ROOT) == "September_2026/+ invoices REVOLUT"
    assert naming.folder_in_month(f"{SEPT}/a.pdf", ROOT) == "September_2026"
    assert naming.folder_in_month(f"{INBOX}/a.pdf", ROOT) is None


# ── реестр и доска ────────────────────────────────────────────────────────


def test_реестр_показывает_поездку_и_где_лежит_файл():
    notes = registry._notes(_doc())
    assert "поездка: 14.10.2026" in notes
    assert "файл в папке September_2026/+ invoices REVOLUT" in notes


def test_реестр_не_пишет_про_папку_когда_месяц_совпал():
    notes = registry._notes(_doc(travel_date=date(2026, 9, 16)))
    assert "файл в папке" not in notes


def test_billing_month_на_доске_месяц_поездки():
    assert monday_map.fields(_doc())[monday_map.BILLING_MONTH] == "October_2026"


# ── разбор ────────────────────────────────────────────────────────────────


def test_модель_видит_и_тему_и_тело_письма():
    """Раньше `raw_caption or raw_email_body`: у письма caption — тема,
    и тело с датами перелёта до модели не доходило никогда."""
    doc = _doc(source=Source.mail, raw_caption="Your booking", raw_email_body="Flight on 14 Oct")
    hint = parser.hint_for(doc)
    assert "Your booking" in hint and "Flight on 14 Oct" in hint


async def test_ручной_билет_в_закрытый_месяц_помечается(monkeypatch):
    async def _closed(_s, company, month):
        return month == "August_2026"

    monkeypatch.setattr(filer, "is_month_closed", _closed)
    doc = _doc(travel_date=date(2026, 8, 20))
    await parser.note_closed_travel_month(None, doc)
    assert doc.belongs_to_closed_month == "August_2026"
    doc = _doc()
    await parser.note_closed_travel_month(None, doc)
    assert doc.belongs_to_closed_month is None


class _Dbx:
    def __init__(self):
        self.folders, self.moves = [], []

    async def ensure_folder(self, path):
        self.folders.append(path)

    async def exists(self, path):
        return True

    async def move(self, src, dst):
        self.moves.append((src, dst))
        return dst


class _Session:
    def add(self, _obj):
        pass

    async def flush(self):
        pass


async def test_файл_системы_уезжает_в_месяц_поездки_и_папка_создаётся(monkeypatch):
    async def _open(*_a):
        return False

    dbx = _Dbx()
    monkeypatch.setattr(filer, "get_dropbox", lambda: dbx)
    monkeypatch.setattr(filer, "is_month_closed", _open)
    doc = _doc(source=Source.mail, travel_date=date(2026, 12, 3),
               dropbox_path=f"{INBOX}/September_2026/ticket.pdf", original_name="ticket.pdf")
    path, note = await filer.file_document(
        _Session(), doc, company="orlanda_engineering", vendor="Wizz Air",
        amount=40.0, currency="EUR", doc_date=date(2026, 9, 29),
    )
    assert path.startswith(f"{ROOT}/2026/December_2026/2026-09-29_")
    assert f"{ROOT}/2026/December_2026" in dbx.folders
    assert note is None
