"""matches.id_round

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28 01:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Asegura la columna de jornada del lado web en matches.

    En producción la crea RCL-Next junto con su clave foránea compuesta a rounds,
    así que se añade solo si falta (entornos de prueba y despliegues nuevos).
    """
    op.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS id_round smallint")


def downgrade() -> None:
    """No elimina la columna: pertenece al esquema de RCL-Next."""
