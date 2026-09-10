from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
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


class Status(str, enum.Enum):
    new = "new"                          # собран, не разобран
    filed = "filed"                      # разложен в папку компании
    needs_review = "needs_review"        # разбор не уверен, ждёт человека
    probable_duplicate = "probable_duplicate"  # совпал по бизнес-ключу
    ignored = "ignored"                  # не документ расхода
    failed = "failed"                    # разбор упал N раз подряд


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

    original_name: Mapped[str] = mapped_column(String(255))
    dropbox_path: Mapped[str | None] = mapped_column(Text)
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
