"""monday board sync, currency conversion, project link

Revision ID: a1f4c7d9e210
Revises: 623cc313ef44
Create Date: 2026-09-28

Спек №4. Все колонки nullable, все таблицы новые — откат не теряет данных
и не требует переноса.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a1f4c7d9e210"
down_revision = "623cc313ef44"
branch_labels = None
depends_on = None


DOC_KIND = sa.Enum(
    "invoice", "receipt", "credit_note", "proforma", "other", name="doc_kind_enum"
)
PROJECT_SOURCE = sa.Enum(
    "task_code", "filename", "text", "rule", "human", name="project_source_enum"
)


def upgrade() -> None:
    bind = op.get_bind()
    DOC_KIND.create(bind, checkfirst=True)
    PROJECT_SOURCE.create(bind, checkfirst=True)

    # ── documents ─────────────────────────────────────────────────────────
    op.add_column("documents", sa.Column("doc_kind", DOC_KIND, nullable=True))
    op.add_column("documents", sa.Column("doc_number", sa.String(64), nullable=True))
    op.create_index("ix_documents_doc_number", "documents", ["doc_number"])

    op.add_column("documents", sa.Column("amount_eur", sa.Numeric(14, 2), nullable=True))
    op.add_column("documents", sa.Column("fx_rate", sa.Numeric(18, 8), nullable=True))
    op.add_column("documents", sa.Column("fx_rate_date", sa.Date(), nullable=True))
    op.add_column("documents", sa.Column("fx_source", sa.String(16), nullable=True))
    op.add_column("documents", sa.Column("category", sa.String(64), nullable=True))

    op.add_column("documents", sa.Column("project_monday_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_documents_project_monday_id", "documents", ["project_monday_id"])
    op.add_column("documents", sa.Column("project_name", sa.String(255), nullable=True))
    op.add_column("documents", sa.Column("project_source", PROJECT_SOURCE, nullable=True))
    op.add_column("documents", sa.Column("task_codes", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("task_monday_id", sa.BigInteger(), nullable=True))
    op.add_column("documents", sa.Column("service_monday_id", sa.BigInteger(), nullable=True))

    op.add_column("documents", sa.Column("monday_item_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_documents_monday_item_id", "documents", ["monday_item_id"])
    op.add_column(
        "documents", sa.Column("monday_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("documents", sa.Column("monday_error", sa.Text(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("monday_last_written", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )

    op.add_column("documents", sa.Column("merged_into_id", sa.Integer(), nullable=True))
    op.create_index("ix_documents_merged_into_id", "documents", ["merged_into_id"])
    op.create_foreign_key(
        "fk_documents_merged_into_id", "documents", "documents", ["merged_into_id"], ["id"]
    )

    # ── routing_rules ─────────────────────────────────────────────────────
    # Исторически правило решало только компанию, поэтому target по умолчанию
    # company: существующие строки менять не нужно. company становится
    # nullable — у правила про проект её нет.
    op.add_column(
        "routing_rules",
        sa.Column("target", sa.String(16), nullable=False, server_default="company"),
    )
    op.add_column("routing_rules", sa.Column("project_monday_id", sa.BigInteger(), nullable=True))
    op.add_column("routing_rules", sa.Column("project_name", sa.String(255), nullable=True))
    op.alter_column("routing_rules", "company", existing_type=sa.String(64), nullable=True)

    # ── новые таблицы ─────────────────────────────────────────────────────
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("rate_date", "currency", "source", name="uq_fx_rate"),
    )

    op.create_table(
        "monday_pushes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_monday_pushes_document_id", "monday_pushes", ["document_id"], unique=True)
    op.create_index("ix_monday_pushes_next_attempt_at", "monday_pushes", ["next_attempt_at"])

    op.create_table(
        "monday_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("item_name", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_monday_intents_document_id", "monday_intents", ["document_id"])

    op.create_table(
        "monday_projects",
        sa.Column("monday_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized", sa.String(255), nullable=False),
        sa.Column("group_title", sa.String(120), nullable=True),
        sa.Column("shortcut", sa.String(32), nullable=True),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_monday_projects_normalized", "monday_projects", ["normalized"])

    op.create_table(
        "monday_tasks",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("task_monday_id", sa.BigInteger(), nullable=True),
        sa.Column("task_name", sa.String(255), nullable=True),
        sa.Column("project_monday_id", sa.BigInteger(), nullable=True),
        sa.Column("project_name", sa.String(255), nullable=True),
        sa.Column("service_monday_id", sa.BigInteger(), nullable=True),
        sa.Column("found", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("monday_tasks")
    op.drop_index("ix_monday_projects_normalized", table_name="monday_projects")
    op.drop_table("monday_projects")
    op.drop_index("ix_monday_intents_document_id", table_name="monday_intents")
    op.drop_table("monday_intents")
    op.drop_index("ix_monday_pushes_next_attempt_at", table_name="monday_pushes")
    op.drop_index("ix_monday_pushes_document_id", table_name="monday_pushes")
    op.drop_table("monday_pushes")
    op.drop_table("fx_rates")

    op.alter_column("routing_rules", "company", existing_type=sa.String(64), nullable=False)
    op.drop_column("routing_rules", "project_name")
    op.drop_column("routing_rules", "project_monday_id")
    op.drop_column("routing_rules", "target")

    op.drop_constraint("fk_documents_merged_into_id", "documents", type_="foreignkey")
    op.drop_index("ix_documents_merged_into_id", table_name="documents")
    op.drop_column("documents", "merged_into_id")
    op.drop_column("documents", "monday_last_written")
    op.drop_column("documents", "monday_error")
    op.drop_column("documents", "monday_synced_at")
    op.drop_index("ix_documents_monday_item_id", table_name="documents")
    op.drop_column("documents", "monday_item_id")
    op.drop_column("documents", "service_monday_id")
    op.drop_column("documents", "task_monday_id")
    op.drop_column("documents", "task_codes")
    op.drop_column("documents", "project_source")
    op.drop_column("documents", "project_name")
    op.drop_index("ix_documents_project_monday_id", table_name="documents")
    op.drop_column("documents", "project_monday_id")
    op.drop_column("documents", "category")
    op.drop_column("documents", "fx_source")
    op.drop_column("documents", "fx_rate_date")
    op.drop_column("documents", "fx_rate")
    op.drop_column("documents", "amount_eur")
    op.drop_index("ix_documents_doc_number", table_name="documents")
    op.drop_column("documents", "doc_number")
    op.drop_column("documents", "doc_kind")

    bind = op.get_bind()
    PROJECT_SOURCE.drop(bind, checkfirst=True)
    DOC_KIND.drop(bind, checkfirst=True)
