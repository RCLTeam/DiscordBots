"""role_requests.posicion

Revision ID: 003
Revises: 002
Create Date: 2026-09-27 02:00:00.000000

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
    """Añade la posición de plantilla solicitada por el jugador."""
    op.add_column(
        "role_requests",
        sa.Column("posicion", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    """Elimina la columna de posición."""
    op.drop_column("role_requests", "posicion")
