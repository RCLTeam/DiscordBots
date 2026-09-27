"""bot_tables

Revision ID: 0001
Revises: 0000
Create Date: 2026-09-27 20:05:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "0001"
down_revision: str | None = "0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Creación de ENUM nativo de PostgreSQL para 'rolerequeststatus'
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        rolerequeststatus_enum = postgresql.ENUM(
            "PENDING", "APPROVED", "DENIED", name="rolerequeststatus"
        )
        rolerequeststatus_enum.create(bind, checkfirst=True)

    # 2. Creación de la tabla 'ticket_notices'
    op.create_table(
        "ticket_notices",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("category_name", sa.String(length=100), nullable=True),
        sa.Column("last_staff_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alert_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_pending_staff", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_notices"),
        sa.UniqueConstraint("discord_channel_id", name="uq_ticket_notices_discord_channel_id"),
    )

    # 3. Creación de la tabla 'role_requests'
    op.create_table(
        "role_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("nombre_lol", sa.String(length=100), nullable=False),
        sa.Column("riot_tag", sa.String(length=20), nullable=False),
        sa.Column("equipo", sa.String(length=100), nullable=False),
        sa.Column("posicion", sa.String(length=20), nullable=True),
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

    # 4. Creación de índices para 'role_requests'
    op.create_index("ix_role_requests_user_id", "role_requests", ["user_id"], unique=False)
    op.create_index("ix_role_requests_canal_id", "role_requests", ["canal_id"], unique=False)
    op.create_index("ix_role_requests_estado", "role_requests", ["estado"], unique=False)


def downgrade() -> None:
    # 1. Eliminación de índices y tabla 'role_requests'
    op.drop_index("ix_role_requests_estado", table_name="role_requests")
    op.drop_index("ix_role_requests_canal_id", table_name="role_requests")
    op.drop_index("ix_role_requests_user_id", table_name="role_requests")
    op.drop_table("role_requests")

    # 2. Eliminación de la tabla 'ticket_notices'
    op.drop_table("ticket_notices")

    # 3. Eliminación limpia de tipo ENUM en PostgreSQL/PGlite
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        rolerequeststatus_enum = postgresql.ENUM(
            "PENDING", "APPROVED", "DENIED", name="rolerequeststatus"
        )
        rolerequeststatus_enum.drop(bind, checkfirst=True)
