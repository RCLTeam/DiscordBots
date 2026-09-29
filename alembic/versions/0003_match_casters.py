"""match_casters and match_caster_cards

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29 20:00:00.000000

"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Creación de ENUM nativo de PostgreSQL para 'caster_role'
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        caster_role_enum = postgresql.ENUM("CASTER", "STREAMER", "BOTH", name="caster_role")
        caster_role_enum.create(bind, checkfirst=True)
        caster_role_type = postgresql.ENUM(
            "CASTER",
            "STREAMER",
            "BOTH",
            name="caster_role",
            create_type=False,
        )
    else:
        caster_role_type = sa.Enum("CASTER", "STREAMER", "BOTH", name="caster_role")

    # 2. Creación de la tabla 'match_casters'
    op.create_table(
        "match_casters",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("caster_role", caster_role_type, nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_match_casters"),
        sa.ForeignKeyConstraint(
            ["match_id"],
            ["matches.id"],
            name="fk_match_casters_match_id_matches",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("match_id", "discord_user_id", name="uq_match_casters_match_user"),
    )

    # 3. Creación de índices para 'match_casters'
    op.create_index("ix_match_casters_match_id", "match_casters", ["match_id"], unique=False)
    op.create_index(
        "ix_match_casters_discord_user_id", "match_casters", ["discord_user_id"], unique=False
    )
    op.create_index(
        "uq_match_casters_single_streamer",
        "match_casters",
        ["match_id"],
        unique=True,
        postgresql_where=sa.text("caster_role IN ('STREAMER', 'BOTH')"),
    )

    # 4. Creación de la tabla 'match_caster_cards'
    op.create_table(
        "match_caster_cards",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_match_caster_cards"),
        sa.ForeignKeyConstraint(
            ["match_id"],
            ["matches.id"],
            name="fk_match_caster_cards_match_id_matches",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("match_id", "channel_id", name="uq_match_caster_cards_match_channel"),
    )

    # 5. Creación de índices para 'match_caster_cards'
    op.create_index(
        "ix_match_caster_cards_match_id", "match_caster_cards", ["match_id"], unique=False
    )
    op.create_index(
        "ix_match_caster_cards_channel_id", "match_caster_cards", ["channel_id"], unique=False
    )


def downgrade() -> None:
    # 1. Eliminación de índices y tabla 'match_caster_cards'
    op.drop_index("ix_match_caster_cards_channel_id", table_name="match_caster_cards")
    op.drop_index("ix_match_caster_cards_match_id", table_name="match_caster_cards")
    op.drop_table("match_caster_cards")

    # 2. Eliminación de índices y tabla 'match_casters'
    op.drop_index(
        "uq_match_casters_single_streamer",
        table_name="match_casters",
        postgresql_where=sa.text("caster_role IN ('STREAMER', 'BOTH')"),
    )
    op.drop_index("ix_match_casters_discord_user_id", table_name="match_casters")
    op.drop_index("ix_match_casters_match_id", table_name="match_casters")
    op.drop_table("match_casters")

    # 3. Eliminación limpia de tipo ENUM en PostgreSQL/PGlite
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        caster_role_enum = postgresql.ENUM("CASTER", "STREAMER", "BOTH", name="caster_role")
        caster_role_enum.drop(bind, checkfirst=True)
