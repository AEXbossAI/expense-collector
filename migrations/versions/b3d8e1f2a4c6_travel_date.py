"""travel date for travel expenses

Revision ID: b3d8e1f2a4c6
Revises: a1f4c7d9e210
Create Date: 2026-10-22

Поездка относится к месяцу вылета или заезда (решение фин директора
22.10.2026). Колонка nullable — откат данных не теряет.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b3d8e1f2a4c6"
down_revision = "a1f4c7d9e210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("travel_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "travel_date")
