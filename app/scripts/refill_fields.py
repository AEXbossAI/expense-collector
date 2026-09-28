"""Переразбор задним числом: заполняет поля, которых не было в extract_v1.

Документы, разобранные до 28.09.2026, не имеют рода документа, номера
поставщика, категории и привязки к проекту — этих полей в промпте просто
не существовало. Без них строка на доске бесполезна: категория и есть
ответ на вопрос «куда уходят деньги», ради которого доска заводится. А без
рода документа не работает правило «квитанция сильнее счёта» при выборе
одного документа на расход.

**Что скрипт НЕ трогает: сумму, валюту, дату, вендора, компанию, статус,
файлы.** Они проверены, лежат в реестре и в именах файлов; переписать их
задним числом значит рассинхронизировать базу с Dropbox. Заполняются
ТОЛЬКО пустые новые поля.

Ровно та возможность, ради которой в спеке №2 заведена таблица
parse_attempts: разбор можно перезапустить задним числом. Каждая попытка
туда и пишется, с версией промпта.

    docker exec expense-collector python -m app.scripts.refill_fields
    docker exec expense-collector python -m app.scripts.refill_fields --preview 3
    docker exec expense-collector python -m app.scripts.refill_fields --apply

Без ключей — список документов, модель не вызывается и денег не стоит.
`--preview N` прогоняет модель на N документах и показывает её ответ,
ничего не записывая; стоит столько же, сколько записать эти N. Бесплатного
полного сухого прогона тут быть не может: увидеть ответ модели можно
только спросив модель.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.text import clean
from app.db.models import DocKind, Document, Kind, ParseAttempt, Status
from app.db.session import session_scope
from app.services import extract, llm
from app.tasks import parser
from app.tasks.monday_sync import reconcile_from

logging.basicConfig(level=logging.WARNING)
settings = get_settings()

# Пауза между документами. Не про лимиты Anthropic, а про возможность
# прервать прогон по Ctrl+C и не оставить половину базы переразобранной.
PAUSE_S = 1.0


def _needs_refill(doc: Document) -> bool:
    """Документ, у которого новые поля пусты. Уже заполненные не трогаем:
    повторный вызов модели стоит денег и может дать другой ответ."""
    return doc.doc_kind is None or doc.category is None


async def _select(limit: int | None) -> list[int]:
    since = reconcile_from()
    async with session_scope() as session:
        docs = (
            await session.execute(
                select(Document)
                .where(
                    Document.status.in_([Status.filed, Status.needs_review]),
                    Document.kind == Kind.file,
                    Document.doc_date >= since,
                    Document.merged_into_id.is_(None),
                )
                .order_by(Document.id)
            )
        ).scalars().all()
    ids = [d.id for d in docs if _needs_refill(d)]
    return ids[:limit] if limit else ids


async def _refill_one(doc_id: int, apply: bool) -> str:
    async with session_scope() as session:
        doc = await session.get(Document, doc_id)
        if doc is None or not _needs_refill(doc):
            return "пропуск"

        data = await parser._read_bytes(doc)
        if data is None:
            return "файл недоступен"

        mime = doc.mime_type or mimetypes.guess_type(doc.original_name)[0]
        text, file_data, mime_type = await extract.prepare(data, mime)
        if text is None and file_data is None:
            return f"тип {doc.mime_type} не разбирается"

        hint = f"Имя файла: {doc.original_name}"
        body = doc.raw_caption or doc.raw_email_body or ""
        if body:
            hint += f"\n\n{body}"

        parsed = await llm.extract_fields(
            text=text, file_data=file_data, mime_type=mime_type, hint=hint
        )

        summary = (
            f"{parsed.doc_kind:12s} {parsed.category:26s} "
            f"№ {(parsed.doc_number or '—')[:14]:16s}"
        )

        if not apply:
            return f"[предпросмотр] {summary}"

        # Только пустые новые поля. Сумма, дата, вендор и компания
        # остаются как есть — они в реестре и в имени файла.
        try:
            doc.doc_kind = DocKind(parsed.doc_kind)
        except ValueError:
            doc.doc_kind = DocKind.other
        doc.doc_number = (clean(parsed.doc_number) or "")[:64] or None
        doc.category = clean(parsed.category)

        await parser.apply_project(session, doc, parsed, text)

        session.add(
            ParseAttempt(
                document_id=doc.id,
                extracted_text=(clean(text) or "")[:200000] or None,
                prompt_version=llm.PROMPT_VERSION,
                model=settings.parser_model,
                result=parsed.model_dump_json(),
                error=None,
            )
        )
        project = f" -> {doc.project_name}" if doc.project_name else ""
        return f"{summary}{project}"


async def _list_only(ids: list[int]) -> None:
    """Список без единого обращения к модели. Дефолт именно такой: полный
    предпросмотр стоит ровно столько же, сколько запись, и молчаливо
    удваивать счёт нельзя."""
    async with session_scope() as session:
        docs = (
            await session.execute(select(Document).where(Document.id.in_(ids)))
        ).scalars().all()
    for d in sorted(docs, key=lambda x: x.id):
        print(f"  id={d.id:4d}  {str(d.doc_date):12s} {(d.vendor or '—')[:34]:36s} "
              f"{str(d.amount or '?'):>10s} {d.currency or ''}")


async def main(mode: str, limit: int | None) -> None:
    ids = await _select(limit)
    if not ids:
        print("Нечего переразбирать: у всех документов новые поля заполнены.")
        return

    print(f"Документов к переразбору: {len(ids)}")
    print(f"Модель: {settings.parser_model}, промпт: {llm.PROMPT_VERSION}\n")

    if mode == "list":
        await _list_only(ids)
        print("\nМодель не вызывалась. Посмотреть её ответ на нескольких: --preview 3")
        print("Записать все: --apply")
        return

    apply = mode == "apply"
    if apply:
        print("ЗАПИСЬ В БАЗУ. Суммы, даты, вендоры и файлы не трогаются.\n")
    else:
        print(f"ПРЕДПРОСМОТР {len(ids)} шт. — модель вызывается, в базу не пишем.\n")

    ok = 0
    for n, doc_id in enumerate(ids, 1):
        try:
            result = await _refill_one(doc_id, apply)
            ok += 1
        except Exception as exc:
            result = f"ОШИБКА: {str(exc)[:90]}"
        print(f"  {n:3d}/{len(ids)}  id={doc_id:4d}  {result}")
        await asyncio.sleep(PAUSE_S)

    print(f"\nОбработано без ошибок: {ok} из {len(ids)}")
    if apply:
        print("Синк подберёт их сам: updated_at обновился, monday_synced_at остался старым.")


if __name__ == "__main__":
    argv = sys.argv
    limit = None
    mode = "list"
    if "--apply" in argv:
        mode = "apply"
    elif "--preview" in argv:
        mode = "preview"
        limit = int(argv[argv.index("--preview") + 1])
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    asyncio.run(main(mode, limit))
