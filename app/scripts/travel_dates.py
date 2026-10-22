"""Дата поездки задним числом (22.10.2026).

Фин директор: перелёт и проживание относятся к месяцу поездки, а не
оплаты. У документов, разобранных до extract_v3, даты поездки нет, а
`service_period` — свободный текст («14OCT2026», «рейс LY0513 24/09/2026»),
опираться на него нельзя. Скрипт спрашивает модель заново, с темой и телом
письма, которых она раньше не видела.

Что меняется после --apply:
* `travel_date` у документа;
* строка реестра и Billing month на доске уходят в месяц поездки;
* файл, привезённый системой, переезжает в папку месяца поездки (папка
  создаётся). Файл фин директора остаётся на месте, в реестре у строки
  пометка, в какой папке он лежит;
* поездка в закрытый месяц сданный реестр не трогает: строка остаётся,
  где была, с пометкой «относится к закрытому».

Сумма, дата документа, вендор, компания и имена файлов не меняются.

    docker exec expense-collector python -m app.scripts.travel_dates
    docker exec expense-collector python -m app.scripts.travel_dates --preview
    docker exec expense-collector python -m app.scripts.travel_dates --apply

--preview зовёт модель и сохраняет ответы в travel_dates.json; --apply
применяет ровно показанное, модель второй раз не зовёт.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.core import naming
from app.core.config import get_settings
from app.db.models import Document, Kind, MoveIntent, ParseAttempt, Source, Status
from app.db.session import session_scope
from app.services import extract, filer, llm, registry
from app.services.dropbox_client import get_dropbox
from app.tasks import parser

logging.basicConfig(level=logging.WARNING)
settings = get_settings()

SAVED = Path("travel_dates.json")
PAUSE_S = 1.0
_FIELDS = ("category", "travel_date", "belongs_to_closed_month", "company",
           "dropbox_path", "doc_date")


async def _select() -> list[int]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Document.id)
                .where(
                    Document.category == naming.TRAVEL_CATEGORY,
                    Document.kind == Kind.file,
                    Document.travel_date.is_(None),
                    Document.status.in_(
                        [Status.filed, Status.needs_review, Status.probable_duplicate]
                    ),
                )
                .order_by(Document.id)
            )
        ).scalars().all()
    return list(rows)


def _month_with(doc: Document, travel: date | None) -> str | None:
    view = SimpleNamespace(**{f: getattr(doc, f) for f in _FIELDS})
    view.travel_date = travel
    return naming.expense_month(view)


async def _ask(doc_id: int) -> dict:
    async with session_scope() as session:
        doc = await session.get(Document, doc_id)
        data = await parser._read_bytes(doc)
        if data is None:
            return {"id": doc_id, "error": "файл недоступен"}
        mime = doc.mime_type or mimetypes.guess_type(doc.original_name)[0]
        text, file_data, mime_type = await extract.prepare(data, mime)
        if text is None and file_data is None:
            return {"id": doc_id, "error": f"тип {doc.mime_type} не разбирается"}
        parsed = await llm.extract_fields(
            text=text, file_data=file_data, mime_type=mime_type, hint=parser.hint_for(doc)
        )
        return {"id": doc_id, "travel_date": parsed.travel_date,
                "result": parsed.model_dump_json(), "text": (text or "")[:200000]}


def _describe(doc: Document, travel: date | None) -> str:
    old, new = registry.month_of(doc), _month_with(doc, travel)
    if travel is None:
        action = "даты поездки нет, месяц оплаты"
    elif old == new:
        action = "месяц тот же"
    elif doc.source == Source.manual:
        action = f"строка {old} -> {new}, файл на месте"
    elif doc.status == Status.filed:
        action = f"файл и строка {old} -> {new}"
    else:
        action = "ещё не разложен, разложится в месяц поездки"
    return (f"{doc.id:4d} {doc.source.value:8s} {str(travel or '—'):10s} "
            f"{(doc.vendor or '')[:24]:24s} {action}")


async def _move_to_month(session, doc: Document, month_date: date) -> None:
    """Файл системы — в папку месяца поездки, имя то же."""
    folder = naming.company_path(doc.company, datetime(month_date.year, month_date.month, 1))
    dbx = get_dropbox()
    await dbx.ensure_folder(folder)
    target = f"{folder}/{doc.dropbox_path.rsplit('/', 1)[1]}"
    intent = MoveIntent(document_id=doc.id, from_path=doc.dropbox_path, to_path=target)
    session.add(intent)
    await session.flush()
    doc.dropbox_path = await dbx.move(doc.dropbox_path, target)
    intent.completed_at = datetime.now(timezone.utc)


async def _apply_one(answer: dict, touched: set[tuple[str, str]]) -> str:
    async with session_scope() as session:
        doc = await session.get(Document, answer["id"])
        if doc is None or doc.travel_date is not None:
            return f"{answer['id']:4d} пропуск: уже заполнено"
        travel = parser._parse_date(answer.get("travel_date"))
        line = _describe(doc, travel)
        old = registry.month_of(doc)

        doc.travel_date = travel
        session.add(ParseAttempt(
            document_id=doc.id, extracted_text=answer.get("text") or None,
            prompt_version=llm.PROMPT_VERSION, model=settings.parser_model,
            result=answer["result"], error=None,
        ))
        if travel and doc.company:
            await parser.note_closed_travel_month(session, doc)
            if (
                doc.source != Source.manual
                and doc.status == Status.filed
                and not doc.belongs_to_closed_month
                and doc.dropbox_path
                and filer.in_company_folder(doc.dropbox_path, doc.company)
                and naming.month_of_path(
                    doc.dropbox_path, settings.dropbox_company_roots[doc.company]
                ) != registry.month_of(doc)
            ):
                await _move_to_month(session, doc, travel)

        new = registry.month_of(doc)
        if doc.status == Status.filed and doc.company:
            # Новый месяц пересоберёт цикл реестра сам, а старый о переезде
            # не узнает: строка осталась бы в обоих.
            touched.update({(doc.company, m) for m in (old, new) if m})
        return line


async def main(mode: str) -> None:
    ids = await _select()
    print(f"Поездок без даты: {len(ids)}")

    if mode == "list":
        print("Это только список, модель не вызывалась. Дальше: --preview")
        return

    if mode == "preview":
        answers = []
        for doc_id in ids:
            answer = await _ask(doc_id)
            answers.append(answer)
            async with session_scope() as session:
                doc = await session.get(Document, doc_id)
                if "error" in answer:
                    print(f"{doc_id:4d} {answer['error']}")
                else:
                    print(_describe(doc, parser._parse_date(answer["travel_date"])))
            await asyncio.sleep(PAUSE_S)
        SAVED.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
        print(f"\nОтветы сохранены в {SAVED.resolve()}. Применить: --apply")
        return

    if not SAVED.exists():
        print("Сначала --preview: применяется только показанное.")
        return
    touched: set[tuple[str, str]] = set()
    for answer in json.loads(SAVED.read_text(encoding="utf-8")):
        if "error" in answer:
            continue
        print(await _apply_one(answer, touched))
    async with session_scope() as session:
        for company, month in sorted(touched):
            rows = await registry.regenerate(session, company, month)
            print(f"Реестр {company} {month}: {rows} строк")


if __name__ == "__main__":
    mode = "apply" if "--apply" in sys.argv else "preview" if "--preview" in sys.argv else "list"
    asyncio.run(main(mode))
