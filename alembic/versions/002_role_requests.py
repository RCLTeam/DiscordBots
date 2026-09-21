"""role_requests

Revision ID: 002
Revises: 001
Create Date: 2026-09-19 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Creación de ENUM nativo de PostgreSQL para 'rolerequeststatus'
    rolerequeststatus_enum = postgresql.ENUM(
        "PENDING", "APPROVED", "DENIED", name="rolerequeststatus"
    )
    rolerequeststatus_enum.create(op.get_bind(), checkfirst=True)

    # 2. Creación de la tabla 'role_requests'
    op.create_table(
        "role_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("nombre_lol", sa.String(length=100), nullable=False),
        sa.Column("riot_tag", sa.String(length=20), nullable=False),
        sa.Column("equipo", sa.String(length=100), nullable=False),
        sa.Column("canal_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "estado",
            postgresql.ENUM(
                "PENDING",
                "APPROVED",
                "DENIED",
                name="rolerequeststatus",
                create_type=False,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("staff_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_role_requests"),
    )

    # 3. Creación de índices
    op.create_index("ix_role_requests_user_id", "role_requests", ["user_id"], unique=False)
    op.create_index("ix_role_requests_canal_id", "role_requests", ["canal_id"], unique=False)
    op.create_index("ix_role_requests_estado", "role_requests", ["estado"], unique=False)


def downgrade() -> None:
    # 1. Eliminación de índices y tabla 'role_requests'
    op.drop_index("ix_role_requests_estado", table_name="role_requests")
    op.drop_index("ix_role_requests_canal_id", table_name="role_requests")
    op.drop_index("ix_role_requests_user_id", table_name="role_requests")
    op.drop_table("role_requests")

    # 2. Eliminación limpia e idempotente de tipo ENUM de PostgreSQL
    rolerequeststatus_enum = postgresql.ENUM(
        "PENDING", "APPROVED", "DENIED", name="rolerequeststatus"
    )
    rolerequeststatus_enum.drop(op.get_bind(), checkfirst=True)
