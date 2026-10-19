"""Файлы, которые фин директор переложила руками.

13.10.2026: 9 октября месяц разложили по подпапкам банков
(`+INVOICES WISE`, `+invoices Wamo`). Сверка не заходила в подпапки и
10 октября пометила удалёнными 51 документ — реестр сентября просел
с 86 строк до 42. Ответ «один расход» по документу 115 пытался
переложить файл по старому пути, падал, оставлял подвисшее перемещение,
и sweeper слал про него алерт каждый час.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.models import Document, MoveIntent, Status
from app.services import answers, filer
from app.services.dropbox_client import SourceMissing
from app.tasks import reconciler, sweeper

ROOT = "/Finance/AMS Group_accouting"
SEPT = f"{ROOT}/2026/September_2026"
WISE = f"{SEPT}/+INVOICES WISE"


def _entry(path: str, h: str) -> SimpleNamespace:
    return SimpleNamespace(path_display=path, name=path.rsplit("/", 1)[1], content_hash=h)


def _doc(id: int, h: str | None, status: Status, path: str | None,
         reason: str | None = None, company: str = "orlanda_engineering") -> Document:
    return Document(id=id, content_hash=h, status=status, dropbox_path=path,
                    status_reason=reason, company=company)


# ── фейки базы и Dropbox ──────────────────────────────────────────────────


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows, by_id=None):
        self.rows = rows
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


@pytest.fixture
def alerts_sent(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def _alert(key, text):
        sent.append((key, text))

    monkeypatch.setattr("app.services.alerts.alert", _alert)
    return sent


async def _reconcile(monkeypatch, docs, files, *, israel_listed=True, dry=False):
    session = _Session(docs)

    @asynccontextmanager
    async def _scope():
        yield session

    async def _list(company, root):
        if company == "orlanda_israel":
            return {} if israel_listed else None
        return reconciler._collect(files)

    monkeypatch.setattr(reconciler, "session_scope", _scope)
    monkeypatch.setattr(reconciler, "_list_company", _list)
    return await reconciler.reconcile_once(dry=dry), session


# ── листинг ───────────────────────────────────────────────────────────────


def test_одинаковые_копии_не_теряются():
    found = reconciler._collect([
        _entry(f"{SEPT}/a (1).pdf", "h1"),
        _entry(f"{SEPT}/a (2).pdf", "h1"),
        _entry(f"{SEPT}/_реестр.csv", "hr"),
        SimpleNamespace(path_display=f"{SEPT}/x", name="x", content_hash=None),
    ])
    assert sorted(p for p, _ in found["h1"]) == [f"{SEPT}/a (1).pdf", f"{SEPT}/a (2).pdf"]
    assert "hr" not in found


def test_текущая_копия_остаётся_текущей():
    """Иначе документ каждый день «перемещается» между копиями:
    9.10.2026 таких перемещений было 91."""
    copies = [(f"{SEPT}/a (1).pdf", "a (1).pdf"), (f"{SEPT}/a (2).pdf", "a (2).pdf")]
    assert reconciler._pick(f"{SEPT}/a (2).pdf", copies)[0] == f"{SEPT}/a (2).pdf"
    assert reconciler._pick(f"{SEPT}/A (2).PDF", copies)[0] == f"{SEPT}/a (2).pdf"


def test_новая_копия_выбирается_детерминированно():
    copies = [(f"{SEPT}/b.pdf", "b.pdf"), (f"{SEPT}/a.pdf", "a.pdf")]
    assert reconciler._pick(None, copies)[0] == f"{SEPT}/a.pdf"
    assert reconciler._pick("/нет/такого.pdf", list(reversed(copies)))[0] == f"{SEPT}/a.pdf"


# ── сверка ────────────────────────────────────────────────────────────────


async def test_файл_в_подпапке_банка_не_удалён(monkeypatch, alerts_sent):
    doc = _doc(29, "h29", Status.filed, f"{SEPT}/PI NO.AC26090806S (2).pdf")
    stats, _ = await _reconcile(monkeypatch, [doc],
                                [_entry(f"{WISE}/FOSHAN ALUCROWN 231 USD.pdf", "h29")])
    assert doc.status == Status.filed
    assert doc.dropbox_path == f"{WISE}/FOSHAN ALUCROWN 231 USD.pdf"
    assert stats["удалено"] == 0


async def test_ошибочно_удалённые_восстанавливаются(monkeypatch, alerts_sent):
    """Ровно 10.10.2026: 51 документ помечен удалённым, файлы в подпапках."""
    docs = [_doc(i, f"h{i}", Status.ignored, f"{SEPT}/f{i} (2).pdf", reconciler.DELETED_REASON)
            for i in range(51)]
    files = [_entry(f"{WISE}/f{i}.pdf", f"h{i}") for i in range(51)]
    stats, _ = await _reconcile(monkeypatch, docs, files)
    assert stats["восстановлено"] == 51
    assert all(d.status == Status.filed for d in docs)
    assert all(d.dropbox_path.startswith(WISE) for d in docs)


async def test_сухой_прогон_ничего_не_восстанавливает(monkeypatch, alerts_sent):
    doc = _doc(1, "h1", Status.ignored, f"{SEPT}/x.pdf", reconciler.DELETED_REASON)
    stats, _ = await _reconcile(monkeypatch, [doc], [_entry(f"{WISE}/x.pdf", "h1")], dry=True)
    assert stats["восстановлено"] == 1
    assert doc.status == Status.ignored


async def test_не_восстанавливается_второй_документ_на_тот_же_файл(monkeypatch, alerts_sent):
    live = _doc(115, "h", Status.probable_duplicate, f"{SEPT}/a.pdf")
    dead = _doc(29, "h", Status.ignored, f"{SEPT}/b.pdf", reconciler.DELETED_REASON)
    await _reconcile(monkeypatch, [live, dead], [_entry(f"{WISE}/a.pdf", "h")])
    assert dead.status == Status.ignored


async def test_отброшенный_дубль_не_следует_за_файлом(monkeypatch, alerts_sent):
    """Иначе путь подтверждённого дубля укажет на живой файл основного,
    и откладывание «лишнего экземпляра» унесёт единственный."""
    dupe = _doc(7, "h", Status.ignored, None, "подтверждённый дубль: похож на документ 6")
    await _reconcile(monkeypatch, [dupe], [_entry(f"{SEPT}/a.pdf", "h")])
    assert dupe.dropbox_path is None
    assert dupe.status == Status.ignored


async def test_массовое_удаление_не_применяется(monkeypatch, alerts_sent):
    docs = [_doc(i, f"h{i}", Status.filed, f"{SEPT}/f{i}.pdf")
            for i in range(reconciler.MAX_DELETIONS + 1)]
    stats, _ = await _reconcile(monkeypatch, docs, [])
    assert stats["удалено"] == reconciler.MAX_DELETIONS + 1
    assert all(d.status == Status.filed for d in docs)
    assert [k for k, _ in alerts_sent] == ["reconcile-mass-delete"]


async def test_единичное_удаление_применяется(monkeypatch, alerts_sent):
    doc = _doc(1, "h1", Status.filed, f"{SEPT}/f.pdf")
    await _reconcile(monkeypatch, [doc], [])
    assert doc.status == Status.ignored
    assert doc.status_reason == reconciler.DELETED_REASON
    assert alerts_sent == []


async def test_неоткрытая_папка_компании_не_даёт_удалений(monkeypatch, alerts_sent):
    israel = "/Finance/Orlanda Israel/Invoices/2026/September_2026/x.pdf"
    doc = _doc(1, "h1", Status.filed, israel, company="orlanda_israel")
    await _reconcile(monkeypatch, [doc], [], israel_listed=False)
    assert doc.status == Status.filed


# ── раскладка ─────────────────────────────────────────────────────────────


def test_папка_компании_и_входящие():
    assert filer.in_company_folder(f"{WISE}/a.pdf", "orlanda_engineering") is True
    assert filer.in_company_folder("/_inbox/a.pdf", "orlanda_engineering") is False
    assert filer.in_company_folder(f"{WISE}/a.pdf", "orlanda_israel") is False
    assert filer.in_company_folder(f"{ROOT} old/a.pdf", "orlanda_engineering") is False


class _FakeDropbox:
    def __init__(self, missing: set[str]):
        self.missing = missing
        self.moves: list[tuple[str, str]] = []

    async def ensure_folder(self, path):
        pass

    async def exists(self, path):
        return path not in self.missing

    async def move(self, src, dst):
        if src in self.missing:
            raise SourceMissing(src)
        self.moves.append((src, dst))
        return dst


async def _file(monkeypatch, session, doc, dbx, located):
    async def _locate(_doc):
        return located

    async def _open(*_a):
        return False

    monkeypatch.setattr(filer, "get_dropbox", lambda: dbx)
    monkeypatch.setattr(filer, "locate", _locate)
    monkeypatch.setattr(filer, "is_month_closed", _open)
    doc.received_at = datetime(2026, 9, 8, tzinfo=timezone.utc)
    doc.original_name = "a.pdf"
    return await filer.file_document(session, doc, company="orlanda_engineering",
                                     vendor="FOSHAN", amount=231.0, currency="USD",
                                     doc_date=None)


async def test_переложенный_руками_файл_остаётся_на_месте(monkeypatch):
    doc = _doc(115, "h", Status.probable_duplicate, f"{SEPT}/old.pdf")
    dbx = _FakeDropbox(missing={f"{SEPT}/old.pdf"})
    session = _Session([])
    path, _ = await _file(monkeypatch, session, doc, dbx, f"{WISE}/FOSHAN.pdf")
    assert path == f"{WISE}/FOSHAN.pdf"
    assert dbx.moves == []
    intents = [o for o in session.added if isinstance(o, MoveIntent)]
    assert intents and all(i.completed_at is not None for i in intents)


async def test_файл_в_подпапке_банка_не_перекладывается(monkeypatch):
    """13.10.2026: подмена дубля 115 нашла файл в `+INVOICES WISE`, файл
    существовал — и был вынесен в корень месяца с переименованием."""
    doc = _doc(115, "h", Status.probable_duplicate, f"{WISE}/FOSHAN ALUCROWN 231 USD.pdf")
    dbx = _FakeDropbox(missing=set())
    session = _Session([])
    path, _ = await _file(monkeypatch, session, doc, dbx, None)
    assert path == f"{WISE}/FOSHAN ALUCROWN 231 USD.pdf"
    assert dbx.moves == []
    assert not [o for o in session.added if isinstance(o, MoveIntent)]


async def test_файл_из_входящих_раскладывается_как_раньше(monkeypatch):
    doc = _doc(5, "h", Status.new, "/_inbox/a.pdf")
    dbx = _FakeDropbox(missing=set())
    session = _Session([])
    path, _ = await _file(monkeypatch, session, doc, dbx, None)
    assert dbx.moves == [("/_inbox/a.pdf", path)]
    assert path == f"{SEPT}/nodate_FOSHAN_231.00-USD_h.pdf"


async def test_файл_найденный_во_входящих_перекладывается(monkeypatch):
    doc = _doc(115, "h", Status.probable_duplicate, "/_inbox/old.pdf")
    dbx = _FakeDropbox(missing={"/_inbox/old.pdf"})
    session = _Session([])
    path, _ = await _file(monkeypatch, session, doc, dbx, "/_inbox/renamed.pdf")
    assert dbx.moves and dbx.moves[0][0] == "/_inbox/renamed.pdf"
    assert path.startswith(SEPT + "/")


async def test_пропавший_файл_не_оставляет_открытого_намерения(monkeypatch):
    """Каждое из шести нажатий оставляло открытое намерение, и sweeper
    слал про каждое алерт раз в час."""
    doc = _doc(115, "h", Status.probable_duplicate, f"{SEPT}/old.pdf")
    dbx = _FakeDropbox(missing={f"{SEPT}/old.pdf"})
    session = _Session([])
    with pytest.raises(SourceMissing):
        await _file(monkeypatch, session, doc, dbx, None)
    intents = [o for o in session.added if isinstance(o, MoveIntent)]
    assert intents and all(i.completed_at is not None for i in intents)


# ── ответ «один расход» ───────────────────────────────────────────────────


async def test_один_расход_без_файла_лучшего_экземпляра(monkeypatch):
    """Подменять нечем — остаётся основной, вопрос закрывается, а не
    «попробуйте позже» шесть раз подряд."""
    primary = _doc(29, "h29", Status.filed, f"{WISE}/FOSHAN.pdf")
    dupe = _doc(115, "h115", Status.probable_duplicate, f"{SEPT}/old.pdf",
                "похож на документ 29 (PI NO.AC26090806S_delivery from China.pdf)")
    session = _Session([], by_id={29: primary, 115: dupe})
    discarded: list[int] = []

    async def _locate(d):
        return d.dropbox_path if d is primary else None

    async def _file_document(*_a, **_k):
        raise SourceMissing(dupe.dropbox_path)

    async def _discard(d):
        discarded.append(d.id)
        return None

    monkeypatch.setattr(answers.dedupe, "prefer", lambda a, b: a)
    monkeypatch.setattr(filer, "locate", _locate)
    monkeypatch.setattr(filer, "file_document", _file_document)
    monkeypatch.setattr(filer, "set_aside", _discard)

    n, text = await answers.answer_duplicate(session, 115, True, "elena")
    assert n == 1
    assert dupe.status == Status.ignored
    assert primary.status == Status.filed
    assert primary.dropbox_path == f"{WISE}/FOSHAN.pdf"
    assert discarded == [115]


async def test_лучший_экземпляр_встаёт_в_подпапку_основного(monkeypatch):
    primary = _doc(29, "h29", Status.filed, f"{WISE}/FOSHAN.pdf")
    dupe = _doc(115, "h115", Status.probable_duplicate, "/_inbox/a.pdf",
                "похож на документ 29 (x.pdf)")
    session = _Session([], by_id={29: primary, 115: dupe})
    seen = {}

    async def _locate(d):
        return d.dropbox_path

    async def _file_document(_s, d, **kw):
        seen["folder"] = kw["folder"]
        return f"{kw['folder']}/new.pdf", None

    async def _discard(d):
        return None

    monkeypatch.setattr(answers.dedupe, "prefer", lambda a, b: a)
    monkeypatch.setattr(filer, "locate", _locate)
    monkeypatch.setattr(filer, "file_document", _file_document)
    monkeypatch.setattr(filer, "set_aside", _discard)

    await answers.answer_duplicate(session, 115, True, "elena")
    assert seen["folder"] == WISE
    assert dupe.status == Status.filed and dupe.dropbox_path == f"{WISE}/new.pdf"
    assert primary.status == Status.ignored


async def test_один_файл_под_двумя_записями_не_откладывается(monkeypatch):
    primary = _doc(29, "h", Status.filed, f"{WISE}/a.pdf")
    dupe = _doc(115, "h", Status.probable_duplicate, f"{WISE}/a.pdf",
                "похож на документ 29 (a.pdf)")
    session = _Session([], by_id={29: primary, 115: dupe})
    discarded: list[int] = []

    async def _discard(d):
        discarded.append(d.id)
        return None

    monkeypatch.setattr(answers.dedupe, "prefer", lambda a, b: b)
    monkeypatch.setattr(filer, "set_aside", _discard)

    await answers.answer_duplicate(session, 115, True, "elena")
    assert discarded == []
    assert dupe.status == Status.ignored


# ── sweeper ───────────────────────────────────────────────────────────────


async def test_потерянное_перемещение_один_алерт_и_закрыто(monkeypatch, alerts_sent):
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    intents = [MoveIntent(id=i, document_id=115, from_path="a", to_path="b",
                          created_at=old, attempts=0) for i in (54, 55, 56)]
    intents.append(MoveIntent(id=62, document_id=154, from_path="c", to_path="d",
                              created_at=old, attempts=0))
    session = _Session(intents)

    @asynccontextmanager
    async def _scope():
        yield session

    async def _nowhere(_intent):
        return "нигде"

    monkeypatch.setattr(sweeper, "session_scope", _scope)
    monkeypatch.setattr(sweeper, "_resolve", _nowhere)

    stats = await sweeper.sweep_once()
    assert stats["потеряно"] == 4
    assert all(i.completed_at is not None for i in intents)
    assert sorted(k for k, _ in alerts_sent) == ["move-lost:doc115", "move-lost:doc154"]
