from __future__ import annotations

import asyncio
import logging
import mimetypes
import re

import anthropic
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core import health
from app.core.config import get_settings
from app.core.text import clean
from app.db.models import (
    DocKind,
    Document,
    Kind,
    ParseAttempt,
    ProjectSource,
    RoutingAnswer,
    Source,
    Status,
)
from app.db.session import session_scope
from app.services import (
    alerts,
    company,
    dedupe,
    extract,
    filer,
    llm,
    projects,
    rules,
    validate,
)
from app.services.dropbox_client import get_dropbox

logger = logging.getLogger(__name__)
settings = get_settings()

BATCH = 10


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def _read_bytes(doc: Document) -> bytes | None:
    """Локальный буфер, если файл ещё не выгружен; иначе из Dropbox."""
    if doc.local_path and Path(doc.local_path).exists():
        return Path(doc.local_path).read_bytes()
    if doc.dropbox_path:
        return await get_dropbox().download(doc.dropbox_path)
    return None


async def _log_attempt(session, doc_id: int, text: str | None, model: str | None,
                       result: str | None, error: str | None) -> None:
    session.add(
        ParseAttempt(
            document_id=doc_id,
            extracted_text=(clean(text) or "")[:200000] or None,
            prompt_version=llm.PROMPT_VERSION,
            model=model,
            result=clean(result),
            error=clean(error),
        )
    )


# ── разбор сообщения из чата без файла ────────────────────────────────────


async def _handle_text_only(session, doc: Document) -> None:
    verdict = await llm.classify_text(doc.raw_caption or "")
    await _log_attempt(session, doc.id, doc.raw_caption, settings.parser_model,
                       verdict.model_dump_json(), None)

    if not verdict.is_expense:
        doc.status = Status.ignored
        doc.status_reason = "обсуждение в чате, не расход"
        return

    if verdict.amount is None:
        # Решение владельца: строку с пустой суммой не заводим,
        # отправляем на уточнение.
        doc.status = Status.needs_review
        doc.status_reason = "расход без документа, сумма не названа"
        return

    doc.company = settings.telegram_company
    doc.amount = verdict.amount
    doc.currency = verdict.currency
    doc.purpose = verdict.purpose
    doc.doc_date = _parse_date(verdict.doc_date) or doc.received_at.date()
    doc.status = Status.filed          # файла нет, перекладывать нечего
    doc.resolved_at = datetime.now(timezone.utc)


# ── разбор документа ──────────────────────────────────────────────────────


async def _handle_file(session, doc: Document) -> None:
    data = await _read_bytes(doc)
    if data is None:
        doc.status = Status.needs_review
        doc.status_reason = "файл недоступен ни в буфере, ни в Dropbox"
        return

    # Тип может отсутствовать: реконсилер берёт файл из Dropbox, где
    # заголовка Content-Type нет. Восстанавливаем по расширению.
    mime = doc.mime_type or mimetypes.guess_type(doc.original_name)[0]
    text, file_data, mime_type = await extract.prepare(data, mime)
    if text is None and file_data is None:
        doc.status = Status.needs_review
        doc.status_reason = f"тип {doc.mime_type} не поддержан автоматическим разбором"
        await _log_attempt(session, doc.id, None, None, None, doc.status_reason)
        return

    # Для телеграма компания известна из конфига, для найденных
    # реконсилером — из папки, куда их положил человек. Определять нечего.
    if doc.source == Source.telegram:
        detected, reason = settings.telegram_company, "источник телеграм, компания из конфига"
    elif doc.source == Source.manual and doc.company:
        detected, reason = doc.company, "разложено человеком, компания из папки"
    else:
        detected, reason = company.detect(text or "")
    hints = company.name_hints(text or "") if detected == company.UNKNOWN else {}

    # Имя файла — полноценный источник подсказок, а не украшение: человек
    # пишет туда и вендора, и объект. До этой правки `original_name` в
    # разбор не попадал вовсе.
    hint_text = f"Имя файла: {doc.original_name}"
    body = doc.raw_caption or doc.raw_email_body or ""
    if body:
        hint_text += f"\n\n{body}"
    if hints:
        hint_text += "\n\nНайденные в тексте названия (могут принадлежать и покупателю, и поставщику): " + str(hints)

    # Модель нужна всегда: сумму, дату, вендора и назначение правилами
    # не достать. Определение компании правилом лишь перекрывает её ответ.
    used_model = settings.parser_model
    parsed = await llm.extract_fields(
        text=text, file_data=file_data, mime_type=mime_type, hint=hint_text or None
    )

    await _log_attempt(session, doc.id, text, used_model, parsed.model_dump_json(), None)

    if not parsed.is_expense_document:
        doc.status = Status.ignored
        doc.status_reason = parsed.notes or "не является документом расхода"
        if not settings.parser_dry_run:
            await filer.discard_ignored(doc)
            doc.dropbox_path = None
        return

    # Порядок приоритетов, от сильного к слабому:
    #   1. идентификатор в документе — говорит об ЭТОМ документе;
    #   2. правило — говорит лишь о привычке вендора;
    #   3. ответ модели;
    #   4. компания по умолчанию.
    final_company = detected
    rule_note = None
    if final_company == company.UNKNOWN:
        hit = await rules.match(session, parsed.vendor, doc.sender)
        if hit:
            final_company, rule_note = hit
    if final_company == company.UNKNOWN:
        final_company = parsed.company
    defaulted = False

    if final_company == company.UNKNOWN:
        # Признаков нет ни у одной компании. Если задано умолчание —
        # применяем его, но помечаем документ, чтобы человек видел, что
        # решение принято по умолчанию, а не найдено в документе.
        if settings.default_company and not hints:
            final_company = settings.default_company
            defaulted = True
        else:
            doc.status = Status.needs_review
            doc.status_reason = f"компания не определена ({reason})"
            _apply_fields(doc, parsed, text)
            return

    _apply_fields(doc, parsed, text)
    await apply_project(session, doc, parsed.project_hint, text)

    problems: list[str] = []
    if not validate.amount_present(parsed.amount, text):
        problems.append("сумма не найдена в тексте документа")
    if parsed.doc_date is None:
        problems.append("дата документа не распознана")
    # Расхождение ставки НДС — замечание, а не блокер. Компанию определил
    # регистрационный номер, он надёжнее любой косвенной проверки, а ставки
    # у трансграничных счетов законно отличаются от домашней.
    notes: list[str] = []
    if company.vat_matches(final_company, parsed.vat_rate) is False:
        notes.append(f"ставка НДС {parsed.vat_rate}% не типична для {final_company}")

    doc.company = final_company

    if problems and doc.source == Source.manual:
        # Файл уже лежит в нужной папке, положен человеком. Держать его
        # вне реестра из-за непройденной проверки — значит оставить
        # реестр неполным, а неполный реестр бесполезен для закрытия
        # месяца. Пишем строку с пометкой, что проверить.
        doc.status = Status.filed
        doc.status_reason = "разложено вручную; проверить: " + "; ".join(problems + notes)
        doc.resolved_at = datetime.now(timezone.utc)
        return

    if problems:
        doc.status = Status.needs_review
        doc.status_reason = "; ".join(problems + notes)
        return

    # Человек уже сказал "разные" — правило не имеет права переигрывать
    # его при переразборе. Без этой проверки business_key вычисляется
    # заново и документ снова уезжает в очередь с тем же вопросом.
    said_different = (
        await session.execute(
            select(RoutingAnswer.id).where(
                RoutingAnswer.document_id == doc.id,
                RoutingAnswer.question == "duplicate",
                RoutingAnswer.answer == "different",
            )
        )
    ).first() is not None

    dup = None if said_different else await dedupe.find_exact(session, doc, doc.content_hash)
    if dup is None and not said_different:
        dup = await dedupe.find_business(
            session, doc, parsed.vendor, parsed.amount, parsed.currency, doc.doc_date
        )
    if dup is not None:
        doc.status = Status.probable_duplicate
        doc.status_reason = f"похож на документ {dup.id} ({dup.original_name})"
        doc.also_received_from = f"дубль {dup.id}"
        return

    # Документ, разложенный человеком, НЕ перекладываем и не переименовываем.
    # Разбор нужен только чтобы достать сумму, дату и вендора для реестра.
    # Переименовать файл фин директора — верный способ, чтобы систему
    # выключили в тот же день.
    if doc.source == Source.manual:
        doc.status = Status.filed
        doc.status_reason = "разложено вручную, разобрано для реестра"
        doc.resolved_at = datetime.now(timezone.utc)
        logger.info("Документ %s разобран без перемещения (положен человеком)", doc.id)
        return

    if settings.parser_dry_run:
        doc.status = Status.needs_review
        doc.status_reason = (
            f"[сухой прогон] уехал бы в {final_company}, "
            f"{filer.target_filename(doc, parsed.vendor, parsed.amount, parsed.currency, doc.doc_date)}"
        )
        logger.info("[сухой прогон] %s -> %s / %s", doc.id, final_company,
                    doc.doc_date.isoformat() if doc.doc_date else "nodate")
        return

    path, closed_note = await filer.file_document(
        session, doc,
        company=final_company,
        vendor=parsed.vendor,
        amount=parsed.amount,
        currency=parsed.currency,
        doc_date=doc.doc_date,
    )
    doc.dropbox_path = path
    doc.belongs_to_closed_month = closed_note
    doc.status = Status.filed
    doc.status_reason = (
        "компания по умолчанию, в документе признаков нет" if defaulted
        else (f"компания по правилу: {rule_note}" if rule_note else None)
    )
    doc.resolved_at = datetime.now(timezone.utc)
    logger.info("Документ %s разложен: %s%s", doc.id, path,
                " (по умолчанию)" if defaulted else "")


def _apply_fields(doc: Document, parsed: llm.ExtractedDoc, text: str | None) -> None:
    doc.vendor = clean(parsed.vendor)
    doc.amount = parsed.amount
    doc.currency = parsed.currency
    doc.purpose = clean(parsed.purpose)
    doc.service_period = clean(parsed.service_period)
    parsed_date = _parse_date(parsed.doc_date)
    doc.doc_date = parsed_date or doc.received_at.date()
    doc.month_from_receipt = parsed_date is None
    doc.business_key = dedupe.business_key(parsed.vendor, parsed.amount, parsed.currency)

    try:
        doc.doc_kind = DocKind(parsed.doc_kind)
    except ValueError:
        doc.doc_kind = DocKind.other
    doc.doc_number = (clean(parsed.doc_number) or "")[:64] or None
    doc.category = clean(parsed.category)


async def apply_project(session, doc: Document, project_hint: str | None,
                        text: str | None) -> None:
    """Уровни каскада привязки к проекту, от сильного к слабому.

    Публичная и берёт подсказку строкой, а не весь разбор: второй
    вызывающий — скрипт переразбора, и ему нужно пересчитать проекты по
    уже сохранённому тексту, без нового вызова модели.

    Ни один уровень не блокирует раскладку документа: пустой проект — это
    норма для накладного расхода, а не причина держать документ вне реестра.
    Неоднозначность уходит вопросом в дайджест отдельной строкой.
    """
    # Подсказка модели участвует наравне с именем файла и текстом: она
    # вытащена из документа дословно и часто чище, чем сырой текст с
    # развалившимся ивритом.
    haystack = " ".join(filter(None, [text, project_hint]))

    try:
        found = await projects.resolve(
            session,
            filename=doc.original_name,
            text=haystack,
            caption=doc.raw_caption or doc.raw_email_body,
        )
    except Exception:
        # Monday недоступен — это не повод не разложить документ.
        logger.exception("Привязка к проекту упала на документе %s", doc.id)
        return

    doc.task_codes = ", ".join(found.task_codes) or None
    if found.project_monday_id:
        doc.project_monday_id = found.project_monday_id
        doc.project_name = found.project_name
        doc.project_source = found.source
        doc.task_monday_id = found.task_monday_id
        doc.service_monday_id = found.service_monday_id
        logger.info("Документ %s -> проект %s (%s)", doc.id, found.project_name, found.reason)
        return

    # Уровень 3: правило по вендору. Слабее найденного в документе кода и
    # сильнее догадки: код говорит об ЭТОМ документе, правило — о привычке
    # вендора работать на одном объекте.
    hit = await rules.match_project(session, doc.vendor, doc.sender)
    if hit is not None:
        doc.project_monday_id, doc.project_name = hit
        doc.project_source = ProjectSource.rule
        logger.info("Документ %s -> проект %s по правилу", doc.id, doc.project_name)
        return

    logger.debug("Документ %s без проекта: %s", doc.id, found.reason)


# ── цикл ──────────────────────────────────────────────────────────────────


async def _process_one(doc_id: int) -> None:
    async with session_scope() as session:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.status != Status.new:
            return
        try:
            if doc.kind == Kind.text_only:
                await _handle_text_only(session, doc)
            else:
                await _handle_file(session, doc)
        except anthropic.AuthenticationError:
            # Не вина документа. Не засчитываем попытку и не трогаем
            # статус: иначе через три цикла весь инбокс уедет в failed
            # из-за одного неверного ключа.
            raise
        except Exception as exc:
            logger.exception("Разбор документа %s упал", doc_id)
            attempts = (
                await session.execute(
                    select(ParseAttempt).where(ParseAttempt.document_id == doc_id)
                )
            ).scalars().all()
            await _log_attempt(session, doc_id, None, settings.parser_model, None, str(exc)[:2000])
            if len(attempts) + 1 >= settings.parser_max_attempts:
                doc.status = Status.failed
                doc.status_reason = f"разбор не удался {len(attempts) + 1} раз: {exc}"[:2000]


async def _tick() -> None:
    async with session_scope() as session:
        ids = (
            await session.execute(
                select(Document.id)
                .where(Document.status == Status.new)
                .order_by(Document.id)
                .limit(BATCH)
            )
        ).scalars().all()

    if not ids:
        return
    logger.info("Разборщик: в работе %d документов", len(ids))
    for doc_id in ids:
        try:
            await _process_one(doc_id)
        except anthropic.AuthenticationError as exc:
            await alerts.alert(
                "anthropic-auth",
                f"Anthropic отклоняет ключ: {exc}. Разбор остановлен, "
                f"документы ждут в _inbox и не потеряны.",
            )
            return  # остальной батч не трогаем, ключ один на всех


async def run() -> None:
    if settings.anthropic_api_key == "changeme":
        logger.error("ANTHROPIC_API_KEY не задан, разборщик не запущен")
        return
    asyncio.create_task(health.beat_loop("parser"))
    logger.info("Разборщик запущен, модель %s, интервал %dс",
                settings.parser_model, settings.parser_interval_s)
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Разборщик упал на цикле")
        await asyncio.sleep(settings.parser_interval_s)
