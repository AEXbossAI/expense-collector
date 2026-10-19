"""Фото чеков и чеки по ссылке.

19.10.2026: каждое фото чека из чата уходило фин директору вопросом
«какая компания» — у картинки нет текста, и проверка «сумма есть в
тексте документа» не проходила никогда. Ссылка на электронный чек weezmo
от 14.10 была списана в обсуждение, заправка в реестр не попала.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.models import Document, Kind, PendingUpload, Source, Status
from app.services import links, llm, validate
from app.tasks import parser

WEEZMO = "https://receipts.weezmo.com/api/receipts/signed/1e1ca71a-492b-4713-96fc-46027cccb338/download"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, results=()):
        self.results = list(results)
        self.added = []
        self._next_id = 500

    async def execute(self, _stmt):
        return _Result(self.results.pop(0) if self.results else [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, Document) and obj.id is None:
                obj.id = self._next_id
                self._next_id += 1


def _text_doc(caption: str) -> Document:
    return Document(
        id=200, source=Source.telegram, source_key="301", kind=Kind.text_only,
        original_name="", raw_caption=caption, sender="326940213",
        company="orlanda_engineering", status=Status.new,
        received_at=datetime(2026, 10, 14, 3, 31, tzinfo=timezone.utc),
    )


# ── ссылки ────────────────────────────────────────────────────────────────


def test_ссылка_находится_в_тексте():
    assert links.find_urls(f"чек: {WEEZMO}.") == [WEEZMO]
    assert links.find_urls("http://insecure.example/a.pdf и просто текст") == []


def test_имя_файла_по_ссылке():
    assert links._filename(
        "inline; filename=1e1ca71a-492b-4713-96fc-46027cccb338.pdf", WEEZMO, "application/pdf"
    ) == "weezmo_1e1ca71a-492b-4713-96fc-46027cccb338.pdf"
    # без заголовка: последний кусок пути, но не «download»
    assert links._filename(None, WEEZMO, "application/pdf") == (
        "weezmo_1e1ca71a-492b-4713-96fc-46027cccb338.pdf"
    )


@pytest.fixture
def buffered(monkeypatch, tmp_path):
    def _write(source, name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return path

    monkeypatch.setattr(parser.buffer, "write", _write)


async def test_чек_по_ссылке_становится_документом(monkeypatch, buffered):
    async def _fetch(url):
        return links.Fetched(url=url, data=b"%PDF-1.4", mime="application/pdf",
                             name="weezmo_1e1ca71a.pdf")

    monkeypatch.setattr(links, "fetch_receipt", _fetch)
    doc = _text_doc(WEEZMO)
    session = _Session()

    assert await parser._spawn_from_links(session, doc)

    new = next(o for o in session.added if isinstance(o, Document))
    upload = next(o for o in session.added if isinstance(o, PendingUpload))
    assert new.kind == Kind.file and new.status == Status.new
    assert new.mime_type == "application/pdf"
    assert new.company == "orlanda_engineering" and new.sender == "326940213"
    assert Path(new.local_path).read_bytes() == b"%PDF-1.4"
    assert upload.document_id == new.id
    assert upload.target_path.endswith(f"/{new.id}_weezmo_1e1ca71a.pdf")
    assert doc.status == Status.ignored
    assert f"документ {new.id}" in doc.status_reason


async def test_та_же_ссылка_второй_раз_не_качается_заново(monkeypatch, buffered):
    async def _fetch(url):
        return links.Fetched(url=url, data=b"%PDF", mime="application/pdf", name="a.pdf")

    monkeypatch.setattr(links, "fetch_receipt", _fetch)
    doc = _text_doc(WEEZMO)
    session = _Session([[321]])          # документ по этой ссылке уже есть

    assert await parser._spawn_from_links(session, doc)
    assert session.added == []
    assert doc.status_reason == "ссылка на чек, файл — документ 321"


async def test_страница_вместо_документа_разбирается_как_текст(monkeypatch):
    async def _fetch(url):
        return None

    async def _classify(text):
        return llm.TextOnlyVerdict(is_expense=False)

    monkeypatch.setattr(links, "fetch_receipt", _fetch)
    monkeypatch.setattr(llm, "classify_text", _classify)
    doc = _text_doc("смотри https://example.com/page")

    await parser._handle_text_only(_Session(), doc)
    assert doc.status == Status.ignored
    assert doc.status_reason == "обсуждение в чате, не расход"


async def test_касса_не_отвечает_повтор_а_не_обсуждение(monkeypatch):
    async def _fetch(url):
        raise links.LinkUnavailable("timeout")

    async def _classify(text):
        raise AssertionError("сообщение со ссылкой нельзя списывать до последней попытки")

    monkeypatch.setattr(links, "fetch_receipt", _fetch)
    monkeypatch.setattr(llm, "classify_text", _classify)
    doc = _text_doc(WEEZMO)

    with pytest.raises(links.LinkUnavailable):
        await parser._handle_text_only(_Session([[]]), doc)   # попыток ещё не было
    assert doc.status == Status.new


# ── сумма на фото ─────────────────────────────────────────────────────────


def test_европейская_запись_суммы():
    assert validate.amount_present(1234.56, "Total 1.234,56 €")
    assert validate.amount_present(1234.56, "Итого 1 234,56")
    assert not validate.amount_present(1234.56, "Total 1.234,50")


async def test_сумма_с_фото_подтверждается_вторым_чтением(monkeypatch):
    calls = []

    async def _read(data, mime):
        calls.append(mime)
        return "58,71"

    monkeypatch.setattr(llm, "read_total", _read)
    doc = Document(id=176)
    assert await parser._amount_confirmed(doc, 58.71, None, b"jpg", "image/jpeg")
    assert calls == ["image/jpeg"]


async def test_второе_чтение_не_совпало(monkeypatch):
    async def _read(data, mime):
        return "50.00"

    monkeypatch.setattr(llm, "read_total", _read)
    assert not await parser._amount_confirmed(Document(id=225), 58.71, None, b"jpg", "image/jpeg")


async def test_текстовый_слой_обходится_без_второго_чтения(monkeypatch):
    async def _read(data, mime):
        raise AssertionError("лишний вызов модели")

    monkeypatch.setattr(llm, "read_total", _read)
    assert await parser._amount_confirmed(Document(id=1), 58.71, "Total 58.71", b"", "application/pdf")
    # неподдержанный тип — второго чтения нет, вопрос человеку
    assert not await parser._amount_confirmed(Document(id=1), 58.71, None, b"", "application/zip")
