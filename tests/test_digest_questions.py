"""Вопросы дайджеста.

19.10.2026: на вопрос «к какому проекту» фин директор ответила 87 раз,
все 87 — «общие расходы», и вопрос из дайджеста убран. Сообщение из чата
без файла («Fuel, paid by company card») приходило вопросом «без названия
— 0.00», ответ на него падал на «нет пути в Dropbox», и вопрос
возвращался каждый день.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.db.models import Document, Kind, RoutingAnswer, Status
from app.services import answers, filer, review


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    """Каждый execute отдаёт следующий заготовленный результат."""

    def __init__(self, results, by_id=None):
        self.results = list(results)
        self.by_id = by_id or {}
        self.added = []

    async def execute(self, _stmt):
        return _Result(self.results.pop(0) if self.results else [])

    async def get(self, _model, id):
        return self.by_id.get(id)

    def add(self, obj):
        self.added.append(obj)


def _text_doc(id: int, caption: str, amount=None) -> Document:
    return Document(
        id=id, kind=Kind.text_only, original_name="", raw_caption=caption,
        status=Status.needs_review, amount=amount, currency="EUR" if amount else None,
        status_reason="расход без документа, сумма не названа",
        received_at=datetime(2026, 10, 14, 3, 31, tzinfo=timezone.utc),
    )


@pytest.fixture
def no_filing(monkeypatch):
    async def _boom(*a, **kw):
        raise AssertionError("сообщение без файла не раскладывается")

    monkeypatch.setattr(filer, "file_document", _boom)


async def test_ответ_на_сообщение_без_суммы_закрывает_вопрос(no_filing):
    doc = _text_doc(201, "Fuel , paid by company card")
    session = _Session([], {201: doc})

    count, text = await answers.answer_company(session, 201, "orlanda_engineering", "Elena")

    assert count == 1
    assert doc.status == Status.ignored        # в реестр без суммы не идёт
    assert doc.company == "orlanda_engineering"
    assert doc.resolved_by == "Elena"
    assert "Суммы в сообщении нет" in text
    assert [a.answer for a in session.added if isinstance(a, RoutingAnswer)] == ["orlanda_engineering"]


async def test_сообщение_с_суммой_ложится_в_реестр(no_filing):
    doc = _text_doc(202, "такси 20 евро", amount=20)
    session = _Session([], {202: doc})

    count, text = await answers.answer_company(session, 202, "orlanda_engineering", "Elena")

    assert count == 1
    assert doc.status == Status.filed
    assert "эстонскую" in text


async def test_повторное_нажатие_не_спрашивает_снова(no_filing):
    doc = _text_doc(201, "Fuel , paid by company card")
    session = _Session([], {201: doc})
    await answers.answer_company(session, 201, "orlanda_engineering", "Elena")

    count, text = await answers.answer_company(session, 201, "orlanda_engineering", "Elena")
    assert count == 0
    assert text == "Этот вопрос уже закрыт."


async def test_в_дайджесте_нет_вопросов_про_проект_и_у_сообщения_есть_текст():
    doc = _text_doc(226, "Карта компания бензин")
    doc.doc_date = date(2026, 10, 17)
    # needs_review, затем probable_duplicate; третьего запроса быть не должно
    session = _Session([[doc], []])

    items = await review.build(session)

    assert [i.kind for i in items] == ["company"]
    assert items[0].title == "Карта компания бензин"
    assert session.results == []
    assert "проект" not in review.render(items)
