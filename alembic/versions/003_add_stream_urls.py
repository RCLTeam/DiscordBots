"""add_stream_urls

Revision ID: 003
Revises: 002
Create Date: 2026-09-26 23:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("stream_url", sa.Text(), nullable=True))
    op.add_column("matches", sa.Column("stream_url_live", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("matches", "stream_url_live")
    op.drop_column("matches", "stream_url")
