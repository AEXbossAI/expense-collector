from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Source(str, enum.Enum):
    telegram = "telegram"
    mail = "mail"
    manual = "manual"   # положено человеком, найдено реконсилером


class Status(str, enum.Enum):
    new = "new"                          # собран, не разобран
    filed = "filed"                      # разложен в папку компании
    needs_review = "needs_review"        # разбор не уверен, ждёт человека
    probable_duplicate = "probable_duplicate"  # совпал по бизнес-ключу
    ignored = "ignored"                  # не документ расхода
    failed = "failed"                    # разбор упал N раз подряд


class Kind(str, enum.Enum):
    file = "file"            # сообщение или письмо с документом
    text_only = "text_only"  # сообщение без файла: расход без документа ЛИБО обсуждение


class DocKind(str, enum.Enum):
    """Род документа. НЕ путать с Kind: тот про наличие файла, этот про то,
    что в файле написано. Нужен в трёх местах сразу — выбор представителя
    группы дублей (квитанция побеждает счёт, потому что группа Out фиксирует
    оплату), колонка Document type на доске и дата оплаты."""

    invoice = "invoice"
    receipt = "receipt"
    credit_note = "credit_note"
    proforma = "proforma"
    other = "other"


class ProjectSource(str, enum.Enum):
    """Откуда взялась привязка к проекту.

    На доску отдельной колонкой НЕ выносится: фин директору там нужен сам
    проект, а источник привязки нужен на отладке, в сухом прогоне и чтобы
    отличить ответ человека от догадки матчера.
    """

    task_code = "task_code"   # код Drawings Num, детерминированно
    filename = "filename"     # нечёткое совпадение в имени файла
    text = "text"             # нечёткое совпадение в тексте документа
    rule = "rule"             # правило по вендору, заведено человеком
    human = "human"           # ответ человека или правка прямо на доске


class Document(Base):
    """Один документ. Одно письмо может дать несколько документов:
    счёт и квитанция на один расход приходят вместе (наблюдалось живьём)."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[Source] = mapped_column(Enum(Source, name="source_enum"))
    # Стабильный ключ источника: X-GM-MSGID для почты, file_unique_id для ТГ.
    # НЕ message_id и НЕ IMAP UID: первый меняется при пересылке, второй при
    # смене UIDVALIDITY.
    source_key: Mapped[str] = mapped_column(String(255))
    # Группирует документы, приехавшие одним письмом
    email_id: Mapped[str | None] = mapped_column(String(255), index=True)

    # content_hash Dropbox (блочный SHA-256). Точный уровень дедупа: ловит
    # вендоров, шлющих одно вложение двумя письмами.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Бизнес-ключ: нормализованный вендор + сумма + валюта + дата ± N дней.
    # Ловит кросс-канальные дубли (PDF из почты против фото из ТГ).
    business_key: Mapped[str | None] = mapped_column(String(255), index=True)
    also_received_from: Mapped[str | None] = mapped_column(Text)

    kind: Mapped[Kind] = mapped_column(Enum(Kind, name="kind_enum"), default=Kind.file)

    original_name: Mapped[str] = mapped_column(String(255))
    dropbox_path: Mapped[str | None] = mapped_column(Text)
    # Путь в локальном буфере. NULL после успешной выгрузки в Dropbox
    # и у записей kind=text_only, у которых файла нет вовсе.
    local_path: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(120))

    # Сырьё для разбора и для повторного прогона задним числом
    raw_caption: Mapped[str | None] = mapped_column(Text)
    raw_email_body: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(String(320), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Ключ папки — дата документа, а не дата проводки по банку и не период
    # услуги. Расхождение с выпиской на границах месяца — норма.
    doc_date: Mapped[date | None] = mapped_column(Date)
    service_period: Mapped[str | None] = mapped_column(String(64))
    month_from_receipt: Mapped[bool] = mapped_column(Boolean, default=False)
    belongs_to_closed_month: Mapped[str | None] = mapped_column(String(32))

    company: Mapped[str | None] = mapped_column(String(64), index=True)
    vendor: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    # Свободнотекстовая подсказка из документа, НЕ категория учёта
    purpose: Mapped[str | None] = mapped_column(Text)

    # Род документа. Решает, кто победит в группе дублей: группа Out на доске
    # фиксирует оплату, поэтому квитанция сильнее счёта.
    doc_kind: Mapped[DocKind | None] = mapped_column(
        Enum(DocKind, name="doc_kind_enum"), default=None
    )
    # Номер документа у поставщика (40378 в «חשבון עסקה 40378»). Надёжнее
    # связки вендор+сумма+дата: у одного вендора два платежа в один день —
    # законный сценарий, а два документа с одним номером — нет.
    doc_number: Mapped[str | None] = mapped_column(String(64), index=True)

    # ── пересчёт в EUR ────────────────────────────────────────────────────
    # Оригинальные amount/currency не трогаем: они в реестре и в имени файла.
    # Курс и его дату храним рядом с суммой, иначе через полгода цифру в EUR
    # нечем объяснить.
    amount_eur: Mapped[float | None] = mapped_column(Numeric(14, 2))
    fx_rate: Mapped[float | None] = mapped_column(Numeric(18, 8))
    # Дата курса, а не дата документа: провайдеры сами откатываются на
    # последний опубликованный день, и документ от субботы получает пятничный
    # курс. Записываем фактическую дату, чтобы это было видно.
    fx_rate_date: Mapped[date | None] = mapped_column(Date)
    fx_source: Mapped[str | None] = mapped_column(String(16))   # ecb | nbg | none

    # Категория расхода. Выводится из вендора устойчиво: отправитель,
    # вендор и назначение сцеплены, и на боевых данных это подтвердилось.
    category: Mapped[str | None] = mapped_column(String(64))

    # ── привязка к проекту ────────────────────────────────────────────────
    project_monday_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    project_name: Mapped[str | None] = mapped_column(String(255))
    project_source: Mapped[ProjectSource | None] = mapped_column(
        Enum(ProjectSource, name="project_source_enum"), default=None
    )
    # Все найденные коды задач, когда их больше одного. Колонка Projects на
    # доске принимает ровно один элемент, поэтому остальные проекты уходят
    # в Update айтема, а не теряются.
    task_codes: Mapped[str | None] = mapped_column(Text)
    task_monday_id: Mapped[int | None] = mapped_column(BigInteger)
    service_monday_id: Mapped[int | None] = mapped_column(BigInteger)

    # ── зеркало на доске Monday ───────────────────────────────────────────
    monday_item_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    monday_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    monday_error: Mapped[str | None] = mapped_column(Text)
    # Что именно мы записали в прошлый раз. Поле, которое человек поправил
    # руками, мы перезаписывать не имеем права — сравниваем с этим слепком.
    monday_last_written: Mapped[dict | None] = mapped_column(JSON)

    # Не представитель группы дублей: указывает на того, кто поехал на доску.
    # NULL у представителя и у одиночных документов.
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id"), index=True
    )

    status: Mapped[Status] = mapped_column(
        Enum(Status, name="status_enum"), default=Status.new, index=True
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    resolved_by: Mapped[str | None] = mapped_column(String(120))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    attempts: Mapped[list["ParseAttempt"]] = relationship(back_populates="document")

    __table_args__ = (
        UniqueConstraint("source", "source_key", "original_name", name="uq_source_entry"),
        Index("ix_documents_company_month", "company", "doc_date"),
    )


class ParseAttempt(Base):
    """История разбора. Без сохранённого текста и версии промпта повторный
    прогон задним числом нереализуем, а это заявленный плюс архитектуры."""

    __tablename__ = "parse_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    extracted_text: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="attempts")


class MonthState(Base):
    """Закрытый месяц. Поздние документы не дописываются в сданный отчёт
    молча: иначе доверие к реестру заканчивается на первом же случае."""

    __tablename__ = "months"

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(64))
    month: Mapped[str] = mapped_column(String(32))  # ключ папки, напр. August_2026
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("company", "month", name="uq_company_month"),)


class PendingUpload(Base):
    """Очередь выгрузки из локального буфера в Dropbox. Строка живёт, пока
    файл не лёг в _inbox; ретраи бесконечные с экспоненциальным бэкоффом.
    Алерт по ВОЗРАСТУ самой старой строки, а не по объёму очереди: час
    непрерывных ретраев означает протухший токен, и это чинится руками."""

    __tablename__ = "pending_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), unique=True, index=True
    )
    local_path: Mapped[str] = mapped_column(Text)
    target_path: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MoveIntent(Base):
    """Намерение переместить файл. Транзакции между Dropbox и БД нет,
    поэтому пишем намерение до операции, а sweeper подбирает подвисшие."""

    __tablename__ = "move_intents"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    from_path: Mapped[str] = mapped_column(Text)
    to_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class SyncCursor(Base):
    """Курсоры источников: offset Telegram, cursor list_folder Dropbox для
    reconciler, последний обработанный X-GM-MSGID."""

    __tablename__ = "sync_cursors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Heartbeat(Base):
    """Пульс компонентов. Молчащий коллектор обнаруживается за час, а не
    через месяц на закрытии."""

    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(primary_key=True)
    component: Mapped[str] = mapped_column(String(64), unique=True)
    beat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RoutingAnswer(Base):
    """Ответ человека на вопрос очереди — вместе с ПРИЗНАКАМИ документа.

    Запись "документ 5 -> эстонская" через месяц бесполезна. Запись
    "вендор bolt, отправитель receipts-cyprus@bolt.eu, валюта EUR ->
    эстонская" — материал, на котором видна закономерность.
    """

    __tablename__ = "routing_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    question: Mapped[str] = mapped_column(String(32))      # company | duplicate
    answer: Mapped[str] = mapped_column(String(64))        # ключ компании, skip, same, different
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    answered_by: Mapped[str | None] = mapped_column(String(120))

    # признаки на момент ответа
    vendor_normalized: Mapped[str | None] = mapped_column(String(255), index=True)
    sender: Mapped[str | None] = mapped_column(String(320))
    currency: Mapped[str | None] = mapped_column(String(8))
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    source: Mapped[str | None] = mapped_column(String(32))


class RoutingRule(Base):
    """Правило маршрутизации. Создаётся ТОЛЬКО явным действием человека:
    ни счётчик ответов, ни отсутствие разногласий его не включают."""

    __tablename__ = "routing_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))          # vendor | sender
    pattern: Mapped[str] = mapped_column(String(255), index=True)
    # Что именно решает правило. Историческое значение — company, поэтому
    # оно и стоит по умолчанию: существующие строки менять не надо.
    target: Mapped[str] = mapped_column(String(16), default="company")  # company | project
    company: Mapped[str | None] = mapped_column(String(64))
    project_monday_id: Mapped[int | None] = mapped_column(BigInteger)
    project_name: Mapped[str | None] = mapped_column(String(255))
    created_from: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class FxRate(Base):
    """Курс на дату. Кэш обязателен: без него каждый документ дёргает
    провайдера заново, а курс на прошедшую дату не меняется никогда.

    rate_date — дата, которую ВЕРНУЛ провайдер, а не которую мы просили:
    и ECB, и НБГ откатываются на последний опубликованный день сами, и
    документ от субботы честно получает пятничный курс.
    """

    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(8))
    # Смысл зависит от источника: у ECB это «единиц валюты за 1 EUR»,
    # у НБГ «лари за 1 единицу валюты». Направления противоположные,
    # поэтому источник хранится рядом и в пересчёте участвует.
    rate: Mapped[float] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(String(16))   # ecb | nbg
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("rate_date", "currency", "source", name="uq_fx_rate"),
    )


class MondayPush(Base):
    """Очередь записи на доску. Устроена как pending_uploads и по той же
    причине: разбор и раскладка не имеют права падать из-за того, что
    Monday отвечает 500. У расхода уже есть файл в папке и строка в
    реестре, доска — производное.

    Алерт по ВОЗРАСТУ старейшей строки, а не по глубине очереди.
    """

    __tablename__ = "monday_pushes"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), unique=True, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MondayIntent(Base):
    """Намерение создать айтем, записанное ДО вызова Monday.

    Та же болезнь, что у MoveIntent в спеке №2: транзакции между Monday и
    Postgres нет. Без намерения падение между create_item и записью
    monday_item_id даёт второй айтем на тот же расход при следующем проходе,
    и сумма месяца на доске тихо удваивается.
    """

    __tablename__ = "monday_intents"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    group_id: Mapped[str] = mapped_column(String(64))
    item_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class MondayProject(Base):
    """Справочник проектов с доски Projects. Кэш на сутки: проекты заводят
    редко, а тянуть 380 items четырьмя страницами на каждый документ дорого.

    normalized — имя, приведённое к виду, по которому идёт нечёткий матч.
    Хранится рядом, чтобы не пересчитывать на каждое сравнение.
    """

    __tablename__ = "monday_projects"

    monday_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized: Mapped[str] = mapped_column(String(255), index=True)
    group_title: Mapped[str | None] = mapped_column(String(120))
    shortcut: Mapped[str | None] = mapped_column(String(32))
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MondayTask(Base):
    """Код Drawings Num -> задача, проект, вид услуги. Кэш бессрочный:
    код неизменяем, задача за ним не переезжает на другой проект."""

    __tablename__ = "monday_tasks"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_monday_id: Mapped[int | None] = mapped_column(BigInteger)
    task_name: Mapped[str | None] = mapped_column(String(255))
    project_monday_id: Mapped[int | None] = mapped_column(BigInteger)
    project_name: Mapped[str | None] = mapped_column(String(255))
    service_monday_id: Mapped[int | None] = mapped_column(BigInteger)
    # Код найден в документе, но задачи с таким кодом в Monday нет. Пишем
    # отрицательный результат тоже, иначе каждый проход будет спрашивать
    # Monday про заведомо отсутствующую задачу.
    found: Mapped[bool] = mapped_column(Boolean, default=True)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
