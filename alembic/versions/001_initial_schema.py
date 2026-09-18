"""initial_schema

Revision ID: 001
Revises:
Create Date: 2026-09-18 07:30:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Creación de ENUMs nativos de PostgreSQL
    division_enum = postgresql.ENUM("PREMIER", "ASCEND", name="division")
    matchstatus_enum = postgresql.ENUM(
        "PENDIENTE", "CANAL_CREADO", "JUGADO", "CANCELADO", name="matchstatus"
    )
    division_enum.create(op.get_bind(), checkfirst=True)
    matchstatus_enum.create(op.get_bind(), checkfirst=True)

    # 2. Creación de la tabla 'teams'
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("tag", sa.String(length=4), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column(
            "division",
            postgresql.ENUM("PREMIER", "ASCEND", name="division", create_type=False),
            nullable=False,
        ),
        sa.Column("discord_role_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_teams"),
        sa.UniqueConstraint("name", name="uq_teams_name"),
        sa.UniqueConstraint("discord_role_id", name="uq_teams_discord_role_id"),
    )
    op.create_index("ix_teams_slug", "teams", ["slug"], unique=False)
    op.create_index("ix_teams_division", "teams", ["division"], unique=False)

    # 3. Creación de la tabla 'matches'
    op.create_table(
        "matches",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("jornada", sa.Integer(), nullable=False),
        sa.Column(
            "division",
            postgresql.ENUM("PREMIER", "ASCEND", name="division", create_type=False),
            nullable=False,
        ),
        sa.Column("team1_id", sa.Uuid(), nullable=False),
        sa.Column("team2_id", sa.Uuid(), nullable=False),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDIENTE",
                "CANAL_CREADO",
                "JUGADO",
                "CANCELADO",
                name="matchstatus",
                create_type=False,
            ),
            server_default="PENDIENTE",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_matches"),
        sa.ForeignKeyConstraint(
            ["team1_id"], ["teams.id"], name="fk_matches_team1_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["team2_id"], ["teams.id"], name="fk_matches_team2_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("discord_channel_id", name="uq_matches_discord_channel_id"),
        sa.UniqueConstraint("jornada", "team1_id", "team2_id", name="uq_matches_jornada_teams"),
        sa.CheckConstraint("team1_id != team2_id", name="ck_matches_distinct_teams"),
    )
    op.create_index("ix_matches_jornada", "matches", ["jornada"], unique=False)
    op.create_index("ix_matches_status", "matches", ["status"], unique=False)

    # 4. Creación de la tabla 'ticket_notices'
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


def downgrade() -> None:
    # 1. Eliminación de tablas en orden inverso de claves foráneas
    op.drop_table("ticket_notices")
    op.drop_table("matches")
    op.drop_table("teams")

    # 2. Eliminación limpia e idempotente de tipos ENUM de PostgreSQL
    matchstatus_enum = postgresql.ENUM(
        "PENDIENTE", "CANAL_CREADO", "JUGADO", "CANCELADO", name="matchstatus"
    )
    matchstatus_enum.drop(op.get_bind(), checkfirst=True)

    division_enum = postgresql.ENUM("PREMIER", "ASCEND", name="division")
    division_enum.drop(op.get_bind(), checkfirst=True)
