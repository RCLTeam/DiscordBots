"""initial_shared_tables

Revision ID: 0000
Revises:
Create Date: 2026-09-27 20:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "0000"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_PREMIER_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
DEFAULT_ASCEND_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")

SHARED_TABLES: set[str] = {
    "teams",
    "matches",
    "seasons",
    "divisions",
    "seasons_divisions",
    "discord_users",
    "players",
    "team_memberships",
    "roster_movements",
    "audit_logs",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Escenario de producción (tablas compartidas creadas por RCL-Next / Drizzle):
    # Omitir la creación sin lanzar error.
    if any(tbl in existing_tables for tbl in SHARED_TABLES):
        return

    # Escenario de base de datos local o de pruebas limpia:
    # 1. Creación de tipos ENUM nativos si el dialecto es PostgreSQL
    if bind.dialect.name == "postgresql":
        postgresql.ENUM("viewer", "admin", "owner", name="app_role").create(bind, checkfirst=True)
        postgresql.ENUM(
            "top",
            "jungle",
            "mid",
            "adc",
            "support",
            "substitute",
            "coach",
            "staff",
            "partners",
            name="roster_role",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "joined",
            "left",
            "promoted_to_captain",
            "demoted_from_captain",
            "role_changed",
            name="roster_movement_action",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "scheduled",
            "live",
            "completed",
            "cancelled",
            "forfeit",
            name="match_status",
        ).create(bind, checkfirst=True)

    # 2. Creación de tablas base independientes (seasons, divisions)
    op.create_table(
        "seasons",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
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
        sa.PrimaryKeyConstraint("name", name="pk_seasons"),
    )

    op.create_table(
        "divisions",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
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
        sa.PrimaryKeyConstraint("name", name="pk_divisions"),
    )

    op.create_table(
        "seasons_divisions",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("season_name", sa.String(length=120), nullable=False),
        sa.Column("division_name", sa.String(length=80), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_seasons_divisions"),
        sa.ForeignKeyConstraint(
            ["season_name"],
            ["seasons.name"],
            name="fk_seasons_divisions_season_name_seasons",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["division_name"],
            ["divisions.name"],
            name="fk_seasons_divisions_division_name_divisions",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "season_name",
            "division_name",
            name="uq_seasons_divisions_season_division",
        ),
    )
    op.create_index(
        "ix_seasons_divisions_season_name",
        "seasons_divisions",
        ["season_name"],
        unique=False,
    )
    op.create_index(
        "ix_seasons_divisions_division_name",
        "seasons_divisions",
        ["division_name"],
        unique=False,
    )

    op.create_table(
        "discord_users",
        sa.Column("discord_id", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("global_name", sa.String(length=64), nullable=True),
        sa.Column("avatar_hash", sa.String(length=128), nullable=True),
        sa.Column(
            "role",
            postgresql.ENUM("viewer", "admin", "owner", name="app_role", create_type=False),
            server_default="viewer",
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("discord_id", name="pk_discord_users"),
    )

    op.create_table(
        "players",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("discord_user_id", sa.String(length=32), nullable=True),
        sa.Column("game_name", sa.String(length=64), nullable=False),
        sa.Column("riot_tag", sa.String(length=16), nullable=True),
        sa.Column("puuid", sa.String(length=128), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column(
            "is_main",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_players"),
        sa.ForeignKeyConstraint(
            ["discord_user_id"],
            ["discord_users.discord_id"],
            name="fk_players_discord_user_id_discord_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("game_name", "riot_tag", name="players_game_name_riot_tag_key"),
    )
    op.create_index("ix_players_discord_user_id", "players", ["discord_user_id"], unique=False)

    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("season_division_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("short_name", sa.String(length=16), nullable=False),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("discord_role_id", sa.BigInteger(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_teams"),
        sa.ForeignKeyConstraint(
            ["season_division_id"],
            ["seasons_divisions.id"],
            name="fk_teams_season_division_id_seasons_divisions",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("name", name="uq_teams_name"),
        sa.UniqueConstraint("discord_role_id", name="uq_teams_discord_role_id"),
    )
    op.create_index(
        "ix_teams_season_division_id",
        "teams",
        ["season_division_id"],
        unique=False,
    )

    op.create_table(
        "team_memberships",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("discord_user_id", sa.String(length=32), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "top",
                "jungle",
                "mid",
                "adc",
                "support",
                "substitute",
                "coach",
                "staff",
                "partners",
                name="roster_role",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "is_captain",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("team_id", "discord_user_id", name="team_memberships_pkey"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_team_memberships_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["discord_user_id"],
            ["discord_users.discord_id"],
            name="fk_team_memberships_discord_user_id_discord_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support')",
            name="team_memberships_captain_role_check",
        ),
    )
    op.create_index(
        "ix_team_memberships_team_id",
        "team_memberships",
        ["team_id"],
        unique=False,
    )
    op.create_index(
        "ix_team_memberships_discord_user_id",
        "team_memberships",
        ["discord_user_id"],
        unique=False,
    )
    op.create_index(
        "team_memberships_unique_captain",
        "team_memberships",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("is_captain = true"),
    )

    op.create_table(
        "roster_movements",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("discord_user_id", sa.String(length=32), nullable=False),
        sa.Column(
            "action",
            postgresql.ENUM(
                "joined",
                "left",
                "promoted_to_captain",
                "demoted_from_captain",
                "role_changed",
                name="roster_movement_action",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "role",
            postgresql.ENUM(
                "top",
                "jungle",
                "mid",
                "adc",
                "support",
                "substitute",
                "coach",
                "staff",
                "partners",
                name="roster_role",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_roster_movements"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_roster_movements_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["discord_user_id"],
            ["discord_users.discord_id"],
            name="fk_roster_movements_discord_user_id_discord_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["discord_users.discord_id"],
            name="fk_roster_movements_actor_id_discord_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_roster_movements_team_id",
        "roster_movements",
        ["team_id"],
        unique=False,
    )
    op.create_index(
        "ix_roster_movements_discord_user_id",
        "roster_movements",
        ["discord_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_roster_movements_actor_id",
        "roster_movements",
        ["actor_id"],
        unique=False,
    )
    op.create_index(
        "ix_roster_movements_created_at",
        "roster_movements",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("actor_discord_user_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        sa.ForeignKeyConstraint(
            ["actor_discord_user_id"],
            ["discord_users.discord_id"],
            name="fk_audit_logs_actor_discord_user_id_discord_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_audit_logs_entity",
        "audit_logs",
        ["entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_actor_discord_user_id",
        "audit_logs",
        ["actor_discord_user_id"],
        unique=False,
    )

    op.create_table(
        "matches",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("jornada", sa.Integer(), nullable=False),
        sa.Column("id_season_division", sa.Uuid(), nullable=False),
        sa.Column("team1_id", sa.Uuid(), nullable=False),
        sa.Column("team2_id", sa.Uuid(), nullable=False),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "scheduled",
                "live",
                "completed",
                "cancelled",
                "forfeit",
                name="match_status",
                create_type=False,
            ),
            server_default="scheduled",
            nullable=False,
        ),
        sa.Column("stream_url", sa.Text(), nullable=True),
        sa.Column("stream_url_live", sa.String(length=255), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_matches"),
        sa.ForeignKeyConstraint(
            ["id_season_division"],
            ["seasons_divisions.id"],
            name="fk_matches_id_season_division",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team1_id"],
            ["teams.id"],
            name="fk_matches_team1_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team2_id"],
            ["teams.id"],
            name="fk_matches_team2_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("discord_channel_id", name="uq_matches_discord_channel_id"),
        sa.UniqueConstraint("jornada", "team1_id", "team2_id", name="uq_matches_jornada_teams"),
        sa.CheckConstraint("team1_id != team2_id", name="ck_matches_distinct_teams"),
    )
    op.create_index("ix_matches_jornada", "matches", ["jornada"], unique=False)
    op.create_index("ix_matches_status", "matches", ["status"], unique=False)
    op.create_index(
        "ix_matches_id_season_division", "matches", ["id_season_division"], unique=False
    )

    # 3. Inserción de semillas por defecto para testing/local
    op.bulk_insert(
        sa.table("seasons", sa.column("name", sa.String)),
        [{"name": "default"}],
    )
    op.bulk_insert(
        sa.table(
            "divisions",
            sa.column("name", sa.String),
            sa.column("sort_order", sa.SmallInteger),
        ),
        [
            {"name": "PREMIER", "sort_order": 1},
            {"name": "ASCEND", "sort_order": 2},
        ],
    )
    op.bulk_insert(
        sa.table(
            "seasons_divisions",
            sa.column("id", sa.Uuid),
            sa.column("season_name", sa.String),
            sa.column("division_name", sa.String),
        ),
        [
            {
                "id": DEFAULT_PREMIER_SEASON_DIVISION_ID,
                "season_name": "default",
                "division_name": "PREMIER",
            },
            {
                "id": DEFAULT_ASCEND_SEASON_DIVISION_ID,
                "season_name": "default",
                "division_name": "ASCEND",
            },
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Eliminación de tablas en orden inverso de claves foráneas
    drop_order = [
        "audit_logs",
        "roster_movements",
        "team_memberships",
        "matches",
        "players",
        "teams",
        "discord_users",
        "seasons_divisions",
        "divisions",
        "seasons",
    ]
    for table_name in drop_order:
        if table_name in existing_tables:
            if bind.dialect.name == "postgresql":
                op.execute(sa.text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
            else:
                op.drop_table(table_name)

    # Eliminación limpia de ENUMs en PostgreSQL/PGlite
    if bind.dialect.name == "postgresql":
        for enum_name, enum_values in [
            ("match_status", ("scheduled", "live", "completed", "cancelled", "forfeit")),
            (
                "roster_movement_action",
                ("joined", "left", "promoted_to_captain", "demoted_from_captain", "role_changed"),
            ),
            (
                "roster_role",
                (
                    "top",
                    "jungle",
                    "mid",
                    "adc",
                    "support",
                    "substitute",
                    "coach",
                    "staff",
                    "partners",
                ),
            ),
            ("app_role", ("viewer", "admin", "owner")),
        ]:
            postgresql.ENUM(*enum_values, name=enum_name).drop(bind, checkfirst=True)
