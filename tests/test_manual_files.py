"""Файлы, которые фин директор кладёт в папки расходов руками.

19.10.2026: разбор признал «не расходом» сводку сделки Partner (документ 71)
и подтверждение заказа Messe München (187) — и удалил оба файла из папок
фин директора: удаление «только из инбокса» вызывалось по любому пути.
Решение владельца: файлы система не удаляет никогда, файл фин директора
по умолчанию лучше привезённого системой.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import naming
from app.db.models import Document, Kind, Source, Status
from app.services import answers, dedupe, filer, llm, registry
from app.services.dropbox_client import DropboxClient, SourceMissing, content_hash
from app.tasks import parser, reconciler

ROOT = "/Finance/AMS Group_accouting"
SEPT = f"{ROOT}/2026/September_2026"
OCT = f"{ROOT}/2026/October_2026"
WISE = f"{SEPT}/+INVOICES WISE"
RECEIVED = datetime(2026, 10, 13, tzinfo=timezone.utc)
INBOX = filer.settings.dropbox_inbox_path.rstrip("/")


def _doc(id: int, source: Source | None, path: str | None, *, h: str | None = None,
         status: Status = Status.filed, reason: str | None = None,
         company: str = "orlanda_engineering", **kw) -> Document:
    return Document(id=id, source=source, dropbox_path=path, content_hash=h, status=status,
                    status_reason=reason, company=company, original_name="a.pdf",
                    received_at=RECEIVED, **kw)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows=(), by_id=None):
        self.rows = list(rows)
        self.by_id = by_id or {}
        self.added = []

    async def execute(self, _stmt):
        return _Result(self.rows)

    async def get(self, _model, id):
        return self.by_id.get(id)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _FakeDropbox:
    def __init__(self, missing=(), hashes=None):
        self.missing = set(missing)
        self.hashes = dict(hashes or {})
        self.moves: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, bytes]] = []

    async def ensure_folder(self, path):
        pass

    async def exists(self, path):
        return path not in self.missing

    async def move(self, src, dst):
        if src in self.missing:
            raise SourceMissing(src)
        self.moves.append((src, dst))
        return dst

    async def hash_of(self, path):
        return self.hashes.get(path)

    async def upload_bytes(self, data, path):
        self.uploads.append((path, data))
        return content_hash(data)


@pytest.fixture
def alerts_sent(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def _alert(key, text):
        sent.append((key, text))

    monkeypatch.setattr("app.services.alerts.alert", _alert)
    return sent


# ── удалений нет вообще ───────────────────────────────────────────────────


def test_у_клиента_dropbox_нет_удаления():
    assert not hasattr(DropboxClient, "delete")
    assert not hasattr(filer, "discard_ignored")


async def test_файл_фин_директора_не_откладывается(monkeypatch):
    dbx = _FakeDropbox()
    monkeypatch.setattr(filer, "get_dropbox", lambda: dbx)
    for path in (f"{WISE}/sim card.pdf", "/_inbox/October_2026/sim card.pdf"):
        doc = _doc(71, Source.manual, path)
        assert await filer.set_aside(doc) == path
    assert dbx.moves == []


async def test_файл_из_входящих_откладывается_а_не_удаляется(monkeypatch):
    dbx = _FakeDropbox()
    monkeypatch.setattr(filer, "get_dropbox", lambda: dbx)
    doc = _doc(5, Source.mail, f"{INBOX}/October_2026/5_newsletter.pdf")
    doc.original_name = "newsletter.pdf"
    path = await filer.set_aside(doc)
    assert path == f"{INBOX}/_ignored/October_2026/5_newsletter.pdf"
    assert dbx.moves == [(f"{INBOX}/October_2026/5_newsletter.pdf", path)]


async def test_экземпляр_системы_из_папки_компании_откладывается(monkeypatch):
    dbx = _FakeDropbox()
    monkeypatch.setattr(filer, "get_dropbox", lambda: dbx)
    doc = _doc(14, Source.mail, f"{SEPT}/2026-09-01_TimeDoctor_270.00-USD_ab.pdf")
    path = await filer.set_aside(doc)
    assert path.startswith(f"{INBOX}/_ignored/")
    assert dbx.moves[0][0] == doc.dropbox_path


async def test_пропавший_файл_откладывать_нечего(monkeypatch):
    dbx = _FakeDropbox(missing={"/_inbox/x.pdf"})
    monkeypatch.setattr(filer, "get_dropbox", lambda: dbx)
    assert await filer.set_aside(_doc(5, Source.mail, "/_inbox/x.pdf")) is None
    assert await filer.set_aside(_doc(6, Source.mail, None)) is None


# ── разбор ────────────────────────────────────────────────────────────────


async def _parse_not_expense(monkeypatch, doc):
    aside: list[int] = []

    async def _bytes(_doc):
        return b"%PDF"

    async def _prepare(data, mime):
        return "סיכום עסקה Partner", None, None

    async def _extract(**kw):
        return llm.ExtractedDoc(is_expense_document=False, company="unknown",
                                notes="сводка условий сделки")

    async def _set_aside(d):
        aside.append(d.id)
        return d.dropbox_path

    async def _no(*_a, **_k):
        return None

    async def _unconfirmed(*_a, **_k):
        return False

    monkeypatch.setattr(parser, "_read_bytes", _bytes)
    monkeypatch.setattr(parser.extract, "prepare", _prepare)
    monkeypatch.setattr(llm, "extract_fields", _extract)
    monkeypatch.setattr(parser.filer, "set_aside", _set_aside)
    monkeypatch.setattr(parser, "apply_project", _no)
    monkeypatch.setattr(parser, "_amount_confirmed", _unconfirmed)
    monkeypatch.setattr(parser.settings, "parser_dry_run", False)
    await parser._handle_file(_Session(), doc)
    return aside


async def test_ручной_файл_не_расход_попадает_в_реестр_с_пометкой(monkeypatch):
    doc = _doc(71, Source.manual, f"{WISE}/sim card.pdf", status=Status.new, kind=Kind.file,
               mime_type="application/pdf")
    aside = await _parse_not_expense(monkeypatch, doc)
    assert aside == []
    assert doc.status == Status.filed
    assert doc.dropbox_path == f"{WISE}/sim card.pdf"
    assert "проверить:" in doc.status_reason
    assert "не документ расхода" in doc.status_reason
    assert "ПРОВЕРИТЬ" in registry._notes(doc)


async def test_письмо_не_расход_откладывается(monkeypatch):
    doc = _doc(300, Source.mail, "/_inbox/October_2026/300_promo.pdf", status=Status.new,
               kind=Kind.file, mime_type="application/pdf")
    aside = await _parse_not_expense(monkeypatch, doc)
    assert aside == [300]
    assert doc.status == Status.ignored


async def test_неразобранный_ручной_файл_даёт_алерт(monkeypatch, alerts_sent):
    doc = _doc(190, Source.manual, f"{OCT}/scan.pdf", status=Status.new, kind=Kind.file)
    session = _Session(rows=[object(), object()], by_id={190: doc})

    @asynccontextmanager
    async def _scope():
        yield session

    async def _boom(_s, _d):
        raise RuntimeError("битый PDF")

    monkeypatch.setattr(parser, "session_scope", _scope)
    monkeypatch.setattr(parser, "_handle_file", _boom)
    monkeypatch.setattr(parser.settings, "parser_max_attempts", 3)
    await parser._process_one(190)
    assert doc.status == Status.failed
    assert [k for k, _ in alerts_sent] == ["manual-failed:doc190"]


# ── реестр ────────────────────────────────────────────────────────────────


def test_месяц_из_пути_с_подпапкой_банка():
    assert naming.month_of_path(f"{WISE}/a.pdf", ROOT) == "September_2026"
    assert naming.month_of_path(f"{ROOT}/2026/a.pdf", ROOT) is None
    assert naming.month_of_path("/_inbox/September_2026/a.pdf", ROOT) is None


def test_реестр_по_папке_а_не_по_дате():
    no_date = _doc(1, Source.manual, f"{WISE}/a.pdf")
    august_in_sept = _doc(2, Source.manual, f"{SEPT}/b.pdf", doc_date=date(2026, 8, 30))
    in_inbox = _doc(3, Source.mail, f"{INBOX}/October_2026/c.pdf", doc_date=date(2026, 9, 2))
    text_only = _doc(4, Source.telegram, None, doc_date=date(2026, 10, 1))
    nothing = _doc(5, Source.telegram, None)
    assert registry.month_of(no_date) == "September_2026"
    assert registry.month_of(august_in_sept) == "September_2026"
    assert registry.month_of(in_inbox) == "September_2026"
    assert registry.month_of(text_only) == "October_2026"
    assert registry.month_of(nothing) is None


async def _regenerate(monkeypatch, docs, dbx, month="September_2026"):
    monkeypatch.setattr(registry, "get_dropbox", lambda: dbx)
    return await registry.regenerate(_Session(docs), "orlanda_engineering", month)


async def test_реестр_включает_ручной_файл_без_даты(monkeypatch):
    dbx = _FakeDropbox()
    n = await _regenerate(monkeypatch, [_doc(1, Source.manual, f"{WISE}/a.pdf")], dbx)
    assert n == 1
    assert dbx.uploads[0][0] == f"{SEPT}/_реестр.csv"


async def test_реестр_с_тем_же_содержимым_не_перезаписывается(monkeypatch):
    docs = [_doc(1, Source.manual, f"{WISE}/a.pdf")]
    same = content_hash(registry.build_csv(docs))
    dbx = _FakeDropbox(hashes={f"{SEPT}/_реестр.csv": same})
    await _regenerate(monkeypatch, docs, dbx)
    assert dbx.uploads == []


async def test_опустевший_месяц_получает_пустой_реестр(monkeypatch):
    dbx = _FakeDropbox(hashes={f"{SEPT}/_реестр.csv": "old"})
    assert await _regenerate(monkeypatch, [], dbx) == 0
    assert [p for p, _ in dbx.uploads] == [f"{SEPT}/_реестр.csv"]


async def test_пустой_реестр_без_старого_не_заводится(monkeypatch):
    dbx = _FakeDropbox()
    await _regenerate(monkeypatch, [], dbx)
    assert dbx.uploads == []


# ── сверка ────────────────────────────────────────────────────────────────


def _entry(path: str, h: str) -> SimpleNamespace:
    return SimpleNamespace(path_display=path, name=path.rsplit("/", 1)[1], content_hash=h)


async def _reconcile(monkeypatch, docs, files, dry=False):
    session = _Session(docs)
    regenerated: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _scope():
        yield session

    async def _list(company, root):
        return reconciler._collect(files) if company == "orlanda_engineering" else {}

    async def _regen(_s, company, month):
        regenerated.append((company, month))
        return 0

    monkeypatch.setattr(reconciler, "session_scope", _scope)
    monkeypatch.setattr(reconciler, "_list_company", _list)
    monkeypatch.setattr(reconciler.registry, "regenerate", _regen)
    stats = await reconciler.reconcile_once(dry=dry)
    return stats, session, regenerated


async def test_отброшенный_моделью_ручной_файл_возвращается(monkeypatch, alerts_sent):
    """Документ 187: восстановленный из корзины файл должен снова попасть
    в разбор, а не молчать из-за старой пометки модели."""
    doc = _doc(187, Source.manual, None, h="h187", status=Status.ignored,
               reason="Это подтверждение заказа билета, а не счёт")
    stats, session, _ = await _reconcile(
        monkeypatch, [doc], [_entry(f"{OCT}/Messe Munchen 71 EUR.png", "h187")])
    assert stats["возвращено"] == 1
    assert doc.status == Status.new
    assert doc.dropbox_path == f"{OCT}/Messe Munchen 71 EUR.png"
    assert session.added == []


async def test_сухой_прогон_не_возвращает(monkeypatch, alerts_sent):
    doc = _doc(187, Source.manual, None, h="h187", status=Status.ignored, reason="не счёт")
    stats, _, _ = await _reconcile(monkeypatch, [doc], [_entry(f"{OCT}/m.png", "h187")], dry=True)
    assert stats["возвращено"] == 1
    assert doc.status == Status.ignored


async def test_файл_отброшенного_письма_заводится_ручным(monkeypatch, alerts_sent):
    rejected = _doc(300, Source.mail, None, h="h300", status=Status.ignored,
                    reason="не является документом расхода")
    stats, session, _ = await _reconcile(monkeypatch, [rejected], [_entry(f"{OCT}/x.pdf", "h300")])
    assert stats["новых"] == 1
    [new] = session.added
    assert new.source == Source.manual and new.dropbox_path == f"{OCT}/x.pdf"
    assert rejected.status == Status.ignored


async def test_решение_человека_не_переигрывается(monkeypatch, alerts_sent):
    skipped = _doc(301, Source.mail, None, h="h301", status=Status.ignored,
                   reason="не распределяем (решение фин директора)", resolved_by="elena")
    stats, session, _ = await _reconcile(monkeypatch, [skipped], [_entry(f"{OCT}/x.pdf", "h301")])
    assert stats["новых"] == 0 and stats["возвращено"] == 0
    assert session.added == []


async def test_переезд_в_другой_месяц_пересобирает_старый_реестр(monkeypatch, alerts_sent):
    doc = _doc(97, Source.manual, f"{SEPT}/Cyprus Airways.pdf", h="h97")
    _, _, regenerated = await _reconcile(monkeypatch, [doc], [_entry(f"{OCT}/Cyprus Airways.pdf", "h97")])
    assert doc.dropbox_path == f"{OCT}/Cyprus Airways.pdf"
    assert regenerated == [("orlanda_engineering", "September_2026")]


# ── дубли: файл фин директора лучше ───────────────────────────────────────


def test_ручной_файл_побеждает_лучший_артефакт():
    manual = _doc(1, Source.manual, f"{SEPT}/a.jpg", mime_type="image/jpeg", size_bytes=10)
    mail = _doc(2, Source.mail, "/_inbox/b.pdf", mime_type="application/pdf", size_bytes=10**6)
    assert dedupe.prefer(manual, mail) is manual
    assert dedupe.prefer(mail, manual) is manual


def test_между_ручными_решает_качество():
    jpg = _doc(1, Source.manual, f"{SEPT}/a.jpg", mime_type="image/jpeg", size_bytes=10)
    pdf = _doc(2, Source.manual, f"{SEPT}/b.pdf", mime_type="application/pdf", size_bytes=10)
    assert dedupe.prefer(jpg, pdf) is pdf


async def _answer_same(monkeypatch, dupe, primary):
    session = _Session(by_id={dupe.id: dupe, primary.id: primary})
    aside: list[int] = []
    filed: list[int] = []

    async def _locate(d):
        return d.dropbox_path

    async def _set_aside(d):
        aside.append(d.id)
        return "/_inbox/_ignored/x.pdf"

    async def _file_document(_s, d, **_kw):
        filed.append(d.id)
        return f"{SEPT}/renamed.pdf", None

    monkeypatch.setattr(filer, "locate", _locate)
    monkeypatch.setattr(filer, "set_aside", _set_aside)
    monkeypatch.setattr(filer, "file_document", _file_document)
    n, text = await answers.answer_duplicate(session, dupe.id, True, "elena")
    return aside, filed, text


async def test_ручной_дубль_остаётся_на_месте_под_своим_именем(monkeypatch):
    """Mellow, Easy staff, MJG2ZWNE: экземпляр из почты вставал на место
    файла фин директора под системным именем, её файл удалялся."""
    primary = _doc(14, Source.mail, f"{SEPT}/2026-09-01_Mellow_ab.pdf", h="hm",
                   mime_type="application/pdf", size_bytes=10**6)
    dupe = _doc(145, Source.manual, f"{WISE}/Mellow.pdf", h="hd", mime_type="image/jpeg",
                size_bytes=10, status=Status.probable_duplicate,
                reason="похож на документ 14 (Mellow.pdf)")
    aside, filed, _ = await _answer_same(monkeypatch, dupe, primary)
    assert filed == []
    assert dupe.status == Status.filed and dupe.dropbox_path == f"{WISE}/Mellow.pdf"
    assert primary.status == Status.ignored
    assert aside == [14]


async def test_дубль_из_почты_уступает_ручному_основному(monkeypatch):
    primary = _doc(145, Source.manual, f"{WISE}/Mellow.pdf", h="hd", mime_type="image/jpeg",
                   size_bytes=10)
    dupe = _doc(200, Source.mail, "/_inbox/October_2026/200_Mellow.pdf", h="hm",
                mime_type="application/pdf", size_bytes=10**6,
                status=Status.probable_duplicate, reason="похож на документ 145 (Mellow.pdf)")
    aside, filed, _ = await _answer_same(monkeypatch, dupe, primary)
    assert filed == []
    assert primary.status == Status.filed and primary.dropbox_path == f"{WISE}/Mellow.pdf"
    assert dupe.status == Status.ignored
    assert aside == [200]


async def test_второй_ручной_экземпляр_не_трогается(monkeypatch):
    primary = _doc(145, Source.manual, f"{WISE}/Mellow.pdf", h="h1", mime_type="application/pdf",
                   size_bytes=10**6)
    dupe = _doc(146, Source.manual, f"{SEPT}/Mellow copy.jpg", h="h2", mime_type="image/jpeg",
                size_bytes=10, status=Status.probable_duplicate,
                reason="похож на документ 145 (Mellow.pdf)")
    aside, filed, text = await _answer_same(monkeypatch, dupe, primary)
    assert aside == [] and filed == []
    assert dupe.status == Status.ignored and dupe.dropbox_path == f"{SEPT}/Mellow copy.jpg"
    assert "остался в папке" in text


async def test_не_распределяем_откладывает_а_не_удаляет(monkeypatch):
    doc = _doc(310, Source.mail, "/_inbox/October_2026/310_x.pdf", status=Status.needs_review)
    session = _Session(by_id={310: doc})

    async def _set_aside(d):
        return "/_inbox/_ignored/October_2026/310_x.pdf"

    monkeypatch.setattr(filer, "set_aside", _set_aside)
    await answers.answer_company(session, 310, "skip", "elena")
    assert doc.status == Status.ignored
    assert doc.dropbox_path == "/_inbox/_ignored/October_2026/310_x.pdf"
