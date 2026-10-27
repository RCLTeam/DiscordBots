"""Alineación de los modelos con el esquema de las tablas compartidas con RCL-Next.

Las tablas compartidas las crea la migración ``0000_initial_shared_tables`` (que reproduce
el esquema de RCL-Next) y los modelos SQLAlchemy deben describirlas con las mismas columnas,
los mismos tipos y la misma nulabilidad.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from importlib import util
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Date, DateTime, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import liga_bot.models  # noqa: F401
import liga_bot.models.roster  # noqa: F401
from alembic import command
from liga_bot.models.base import Base
from liga_bot.models.enums import AppRole, MatchStatus
from liga_bot.models.match import Match
from liga_bot.models.roster import DiscordUser, Player, Season
from liga_bot.models.team import Team

REPO_ROOT = Path(__file__).resolve().parent.parent

SHARED_TABLES = (
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
)

_DIALECT = postgresql.dialect()


def _describe(type_) -> str:
    return type_.compile(dialect=_DIALECT)


@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", SHARED_TABLES)
async def test_model_matches_migrated_shared_table(migrated_db: AsyncEngine, table_name: str):
    """Columnas, tipos y nulabilidad del modelo coinciden con la tabla migrada."""
    async with migrated_db.connect() as conn:
        reflected = await conn.run_sync(lambda c: inspect(c).get_columns(table_name))

    db_columns = {col["name"]: (_describe(col["type"]), col["nullable"]) for col in reflected}
    model_columns = {
        col.name: (_describe(col.type), col.nullable)
        for col in Base.metadata.tables[table_name].columns
    }

    assert model_columns == db_columns


def test_season_dates_are_date_columns():
    for column in (Season.__table__.c.starts_on, Season.__table__.c.ends_on):
        assert isinstance(column.type, Date)
        assert not isinstance(column.type, DateTime)


def test_match_has_updated_at():
    column = Match.__table__.c.updated_at
    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True
    assert column.nullable is False
    assert column.server_default is not None


def test_team_name_length_is_120():
    assert Team.__table__.c.name.type.length == 120


@pytest.mark.asyncio
async def test_team_name_of_120_characters_is_persisted(session: AsyncSession):
    name = "N" * 120
    team = Team(
        name=name,
        tag="LONG",
        discord_role_id=uuid.uuid4().int >> 66,
    )
    session.add(team)
    await session.flush()

    stored = (await session.execute(select(Team.name).where(Team.id == team.id))).scalar_one()
    assert stored == name


# ---------------------------------------------------------------------------
# Restricciones de temporada (issue #63) y MVP / rol editor (issue #64)
#
# En producción las crearon las migraciones de RCL-Next (0001..0004) sobre la
# base de datos compartida; aquí las reproducen 0000_initial_shared_tables y
# 0004_match_mvp_and_editor_role.
# ---------------------------------------------------------------------------

CONSTRAINTS_ESPERADAS = {
    "seasons_divisions": {"seasons_divisions_id_season_name_key"},
    "teams": {"teams_id_season_name_key", "teams_season_division_season_fkey"},
    "team_memberships": {"team_memberships_team_season_fkey"},
    "matches": {
        "matches_mvp_player_id_fkey",
        "matches_mvp_score_nonnegative_check",
        "matches_mvp_player_score_check",
    },
}


@pytest.mark.parametrize("table_name", sorted(CONSTRAINTS_ESPERADAS))
def test_modelo_declara_las_restricciones_de_temporada(table_name: str):
    """Las restricciones compuestas y de MVP están en los modelos, no solo en la base de datos."""
    tabla = Base.metadata.tables[table_name]
    declaradas = {c.name for c in tabla.constraints if c.name} | {
        fk.constraint.name for fk in tabla.foreign_keys if fk.constraint.name
    }
    assert CONSTRAINTS_ESPERADAS[table_name] <= declaradas


@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", sorted(CONSTRAINTS_ESPERADAS))
async def test_migracion_crea_las_restricciones_de_temporada(
    migrated_db: AsyncEngine, table_name: str
):
    """Y las migraciones de Alembic las crean en el esquema de pruebas."""
    async with migrated_db.connect() as conn:
        uniques = await conn.run_sync(lambda c: inspect(c).get_unique_constraints(table_name))
        fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys(table_name))
        checks = await conn.run_sync(lambda c: inspect(c).get_check_constraints(table_name))

    en_bd = (
        {u["name"] for u in uniques if u.get("name")}
        | {fk["name"] for fk in fks if fk.get("name")}
        | {ck["name"] for ck in checks if ck.get("name")}
    )
    assert CONSTRAINTS_ESPERADAS[table_name] <= en_bd


# Las claves foráneas compuestas solo sirven si arrastran el renombrado de la temporada:
# comprobar únicamente su nombre dejaría pasar un ON UPDATE perdido.
CASCADAS_ESPERADAS = {
    "teams_season_division_season_fkey": ("teams", ["season_division_id", "season_name"]),
    "team_memberships_team_season_fkey": ("team_memberships", ["team_id", "season_name"]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint_name", sorted(CASCADAS_ESPERADAS))
async def test_claves_compuestas_cascadean_en_update_y_delete(
    migrated_db: AsyncEngine, constraint_name: str
):
    """Las dos FK compuestas son ON UPDATE CASCADE y ON DELETE CASCADE, y sobre esas columnas."""
    table_name, columnas = CASCADAS_ESPERADAS[constraint_name]
    async with migrated_db.connect() as conn:
        fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys(table_name))

    fk = next(f for f in fks if f.get("name") == constraint_name)
    options = fk.get("options") or {}
    assert fk["constrained_columns"] == columnas
    assert options.get("onupdate") == "CASCADE"
    assert options.get("ondelete") == "CASCADE"


@pytest.mark.asyncio
async def test_seasons_divisions_no_cascadea_el_renombrado_de_la_temporada(
    migrated_db: AsyncEngine,
):
    """En producción esa FK no tiene ON UPDATE: renombrar una temporada con divisiones falla.

    Declararlo aquí evitaría que alguien diera por hecha una cascada que no existe.
    """
    async with migrated_db.connect() as conn:
        fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("seasons_divisions"))

    fk = next(f for f in fks if f["referred_table"] == "seasons")
    assert (fk.get("options") or {}).get("onupdate") is None


def test_season_no_declara_columnas_inexistentes():
    """``seasons`` no tiene is_active: declararla rompería en producción con UndefinedColumn."""
    columnas = set(Base.metadata.tables["seasons"].columns.keys())
    assert columnas == {"name", "starts_on", "ends_on", "created_at", "updated_at"}


@pytest.mark.asyncio
async def test_app_role_enum_values_and_order(migrated_db: AsyncEngine):
    """AppRole y el tipo nativo app_role incluyen 'editor' entre 'viewer' y 'admin'."""
    expected = ["viewer", "editor", "admin", "owner"]
    assert [e.value for e in AppRole] == expected
    assert DiscordUser.__table__.c.role.type.enums == expected

    async with migrated_db.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT e.enumlabel "
                "FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'app_role' "
                "ORDER BY e.enumsortorder"
            )
        )
        assert [row[0] for row in res.fetchall()] == expected


@pytest.mark.asyncio
async def test_matches_mvp_fk_and_partial_index(migrated_db: AsyncEngine):
    """matches declara y migra matches_mvp_player_id_fkey (SET NULL) y el índice parcial."""
    matches_table = Base.metadata.tables["matches"]
    model_indexes = {idx.name for idx in matches_table.indexes}
    assert "matches_round_mvp_score_idx" in model_indexes

    async with migrated_db.connect() as conn:
        fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("matches"))
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("matches"))

    mvp_fk = next(f for f in fks if f.get("name") == "matches_mvp_player_id_fkey")
    assert mvp_fk["referred_table"] == "players"
    assert mvp_fk["constrained_columns"] == ["mvp_player_id"]
    assert mvp_fk["referred_columns"] == ["id"]
    assert (mvp_fk.get("options") or {}).get("ondelete") == "SET NULL"

    db_index_names = {idx["name"] for idx in indexes}
    assert "matches_round_mvp_score_idx" in db_index_names


# ---------------------------------------------------------------------------
# Coexistencia en PGlite con las migraciones Drizzle de RCL-Next (MergeBranch)
# ---------------------------------------------------------------------------

_FALLBACK_RCL_NEXT_DRIZZLE_SQL: dict[str, str] = {
    "0000_initial_schema.sql": """
CREATE TYPE "public"."app_role" AS ENUM('viewer', 'admin', 'owner');--> statement-breakpoint
CREATE TYPE "public"."game_side" AS ENUM('blue', 'red');--> statement-breakpoint
CREATE TYPE "public"."stage" AS ENUM('regular', 'playoff');--> statement-breakpoint
CREATE TYPE "public"."match_status" AS ENUM(
    'scheduled', 'live', 'completed', 'cancelled', 'forfeit'
);--> statement-breakpoint
CREATE TYPE "public"."roster_role" AS ENUM(
    'top', 'jungle', 'mid', 'adc', 'support', 'substitute', 'coach', 'staff', 'partners'
);--> statement-breakpoint
CREATE TYPE "public"."roster_movement_action" AS ENUM(
    'joined', 'left', 'promoted_to_captain', 'demoted_from_captain', 'role_changed'
);--> statement-breakpoint
CREATE TABLE "discord_users" (
    "discord_id" varchar(32) PRIMARY KEY NOT NULL,
    "username" varchar(64) NOT NULL,
    "global_name" varchar(64),
    "avatar_hash" varchar(128),
    "role" "public"."app_role" DEFAULT 'viewer' NOT NULL,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL
);--> statement-breakpoint
CREATE TABLE "audit_logs" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "actor_discord_user_id" varchar(32) REFERENCES "public"."discord_users"("discord_id")
        ON DELETE set null ON UPDATE no action,
    "action" varchar(120) NOT NULL,
    "entity_type" varchar(64) NOT NULL,
    "entity_id" uuid,
    "before" jsonb,
    "after" jsonb,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL
);--> statement-breakpoint
CREATE TABLE "seasons" (
    "name" varchar(120) PRIMARY KEY NOT NULL,
    "starts_on" date,
    "ends_on" date,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "seasons_dates_check" CHECK (
        "ends_on" IS NULL OR "starts_on" IS NULL OR "ends_on" >= "starts_on"
    )
);--> statement-breakpoint
CREATE TABLE "divisions" (
    "name" varchar(80) PRIMARY KEY NOT NULL,
    "sort_order" smallint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL
);--> statement-breakpoint
CREATE TABLE "seasons_divisions" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "season_name" varchar(120) NOT NULL REFERENCES "public"."seasons"("name")
        ON DELETE cascade ON UPDATE no action,
    "division_name" varchar(80) NOT NULL REFERENCES "public"."divisions"("name")
        ON DELETE cascade ON UPDATE no action,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "seasons_divisions_season_division_key" UNIQUE ("season_name", "division_name")
);--> statement-breakpoint
CREATE TABLE "players" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "discord_user_id" varchar(32) REFERENCES "public"."discord_users"("discord_id")
        ON DELETE set null ON UPDATE no action,
    "game_name" varchar(64) NOT NULL,
    "riot_tag" varchar(16),
    "puuid" varchar(128),
    "country_code" varchar(2),
    "is_main" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "players_game_name_riot_tag_key" UNIQUE ("game_name", "riot_tag")
);--> statement-breakpoint
CREATE TABLE "teams" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "season_division_id" uuid NOT NULL REFERENCES "public"."seasons_divisions"("id")
        ON DELETE cascade ON UPDATE no action,
    "name" varchar(120) NOT NULL,
    "short_name" varchar(16),
    "logo_url" text,
    "color" varchar(7),
    "is_active" boolean DEFAULT true NOT NULL,
    "discord_role_id" bigint,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "teams_season_division_name_key" UNIQUE ("season_division_id", "name"),
    CONSTRAINT "teams_discord_role_id_unique" UNIQUE("discord_role_id")
);--> statement-breakpoint
CREATE TABLE "team_memberships" (
    "team_id" uuid NOT NULL REFERENCES "public"."teams"("id")
        ON DELETE cascade ON UPDATE no action,
    "discord_user_id" varchar(32) NOT NULL REFERENCES "public"."discord_users"("discord_id")
        ON DELETE cascade ON UPDATE no action,
    "role" "public"."roster_role" NOT NULL,
    "is_captain" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "team_memberships_captain_role_check" CHECK (
        "is_captain" = false OR "role" IN ('top', 'jungle', 'mid', 'adc', 'support')
    ),
    PRIMARY KEY ("team_id", "discord_user_id")
);--> statement-breakpoint
CREATE TABLE "roster_movements" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "team_id" uuid NOT NULL REFERENCES "public"."teams"("id")
        ON DELETE cascade ON UPDATE no action,
    "discord_user_id" varchar(32) NOT NULL REFERENCES "public"."discord_users"("discord_id")
        ON DELETE cascade ON UPDATE no action,
    "action" "public"."roster_movement_action" NOT NULL,
    "role" "public"."roster_role",
    "actor_id" varchar(32) REFERENCES "public"."discord_users"("discord_id")
        ON DELETE set null ON UPDATE no action,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL
);--> statement-breakpoint
CREATE TABLE "rounds" (
    "id" smallint NOT NULL,
    "id_season_division" uuid NOT NULL REFERENCES "public"."seasons_divisions"("id")
        ON DELETE cascade ON UPDATE no action,
    "stage" "public"."stage" DEFAULT 'regular' NOT NULL,
    "name" varchar(120),
    "starts_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY ("id", "id_season_division")
);--> statement-breakpoint
CREATE TABLE "matches" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "id_season_division" uuid NOT NULL REFERENCES "public"."seasons_divisions"("id")
        ON DELETE cascade ON UPDATE no action,
    "id_round" smallint,
    "team1_id" uuid NOT NULL REFERENCES "public"."teams"("id")
        ON DELETE cascade ON UPDATE no action,
    "team2_id" uuid NOT NULL REFERENCES "public"."teams"("id")
        ON DELETE cascade ON UPDATE no action,
    "best_of" smallint DEFAULT 1 NOT NULL,
    "status" "public"."match_status" DEFAULT 'scheduled' NOT NULL,
    "scheduled_at" timestamp with time zone,
    "finished_at" timestamp with time zone,
    "winner_team_id" uuid REFERENCES "public"."teams"("id")
        ON DELETE set null ON UPDATE no action,
    "team1_score" smallint DEFAULT 0 NOT NULL,
    "team2_score" smallint DEFAULT 0 NOT NULL,
    "stream_url" text,
    "stream_url_live" varchar(255),
    "notes" text,
    "jornada" integer,
    "discord_channel_id" bigint,
    "created_at" timestamp with time zone DEFAULT now() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "matches_different_teams_check" CHECK ("team1_id" <> "team2_id"),
    CONSTRAINT "matches_best_of_check" CHECK ("best_of" IN (1, 3, 5)),
    CONSTRAINT "matches_scores_check" CHECK ("team1_score" >= 0 AND "team2_score" >= 0),
    CONSTRAINT "matches_winner_participant_check" CHECK (
        "winner_team_id" IS NULL OR "winner_team_id" IN ("team1_id", "team2_id")
    ),
    CONSTRAINT "matches_unique_combination" UNIQUE (
        "id_season_division", "id_round", "team1_id", "team2_id"
    ),
    CONSTRAINT "matches_round_fkey" FOREIGN KEY ("id_round", "id_season_division")
        REFERENCES "public"."rounds"("id", "id_season_division")
        ON DELETE set null ON UPDATE no action,
    CONSTRAINT "matches_discord_channel_id_unique" UNIQUE("discord_channel_id")
);--> statement-breakpoint
CREATE TABLE "editorial_articles" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "title" varchar(180) NOT NULL,
    "excerpt" varchar(500) NOT NULL,
    "body" text NOT NULL,
    "kind" varchar(20) NOT NULL,
    "author" varchar(120) NOT NULL,
    "cover_url" text DEFAULT '' NOT NULL,
    "cover_alt" varchar(240) DEFAULT '' NOT NULL,
    "published" boolean DEFAULT false NOT NULL,
    "show_on_home" boolean DEFAULT false NOT NULL,
    "home_order" integer DEFAULT 0 NOT NULL,
    "published_at" timestamp with time zone,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "editorial_kind_check" CHECK (
        "editorial_articles"."kind" in ('noticia', 'reportaje', 'entrevista', 'otro')
    ),
    CONSTRAINT "editorial_order_check" CHECK ("editorial_articles"."home_order" >= 0)
);--> statement-breakpoint
CREATE UNIQUE INDEX "team_memberships_unique_captain"
    ON "team_memberships" USING btree ("team_id")
    WHERE "team_memberships"."is_captain" = true;
""",
    "0001_competitive_membership_per_season.sql": """
LOCK TABLE seasons_divisions, teams, team_memberships
    IN ACCESS EXCLUSIVE MODE;--> statement-breakpoint
ALTER TABLE teams ADD COLUMN season_name varchar(120);--> statement-breakpoint
UPDATE teams AS t
SET season_name = sd.season_name
FROM seasons_divisions AS sd
WHERE sd.id = t.season_division_id;--> statement-breakpoint
ALTER TABLE teams ALTER COLUMN season_name SET NOT NULL;--> statement-breakpoint
ALTER TABLE seasons_divisions
ADD CONSTRAINT seasons_divisions_id_season_name_key
    UNIQUE (id, season_name);--> statement-breakpoint
ALTER TABLE teams
ADD CONSTRAINT teams_season_division_season_fkey
FOREIGN KEY (season_division_id, season_name)
REFERENCES seasons_divisions (id, season_name)
ON UPDATE CASCADE ON DELETE CASCADE;--> statement-breakpoint
ALTER TABLE teams
ADD CONSTRAINT teams_id_season_name_key UNIQUE (id, season_name);--> statement-breakpoint
ALTER TABLE team_memberships ADD COLUMN season_name varchar(120);--> statement-breakpoint
UPDATE team_memberships AS tm
SET season_name = t.season_name
FROM teams AS t
WHERE t.id = tm.team_id;--> statement-breakpoint
ALTER TABLE team_memberships ALTER COLUMN season_name SET NOT NULL;--> statement-breakpoint
ALTER TABLE team_memberships
ADD CONSTRAINT team_memberships_team_season_fkey
FOREIGN KEY (team_id, season_name)
REFERENCES teams (id, season_name)
ON UPDATE CASCADE ON DELETE CASCADE;--> statement-breakpoint
CREATE EXTENSION IF NOT EXISTS btree_gist;--> statement-breakpoint
ALTER TABLE team_memberships
ADD CONSTRAINT one_competitive_team_per_player_and_season
EXCLUDE USING gist (
  season_name WITH =,
  discord_user_id WITH =
)
WHERE (role IN ('top', 'jungle', 'mid', 'adc', 'support', 'substitute'));
""",
    "0002_match_mvp.sql": """
ALTER TABLE "matches" ADD COLUMN "mvp_player_id" uuid;--> statement-breakpoint
ALTER TABLE "matches" ADD COLUMN "mvp_score" numeric(5, 1);--> statement-breakpoint
ALTER TABLE "matches" ADD CONSTRAINT "matches_mvp_player_id_fkey"
    FOREIGN KEY ("mvp_player_id") REFERENCES "public"."players"("id")
    ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "matches_round_mvp_score_idx"
    ON "matches" USING btree ("id_season_division", "id_round", "mvp_score" DESC NULLS LAST)
    WHERE "status" = 'completed' AND "mvp_player_id" IS NOT NULL;
""",
    "0003_match_mvp_checks.sql": """
ALTER TABLE "matches" ADD CONSTRAINT "matches_mvp_score_nonnegative_check"
    CHECK ("mvp_score" IS NULL OR "mvp_score" >= 0);--> statement-breakpoint
ALTER TABLE "matches" ADD CONSTRAINT "matches_mvp_player_score_check"
    CHECK ("mvp_player_id" IS NULL OR "mvp_score" IS NOT NULL);
""",
    "0004_editor_role.sql": """
ALTER TYPE "public"."app_role" ADD VALUE 'editor' BEFORE 'admin';
""",
}


def _load_rcl_next_drizzle_statements() -> list[str]:
    """Carga las sentencias SQL de las migraciones 0000..0004 de RCL-Next (MergeBranch).

    Lee los ficheros reales de ``RCL-Next/packages/database/drizzle`` cuando están
    presentes en el entorno local y recurre al respaldo canónico en CI (donde solo se
    clona ``DiscordBots``). Omite únicamente ``btree_gist`` y ``EXCLUDE USING gist``
    porque PGlite WASM no empaqueta la extensión ``btree_gist``.
    """
    drizzle_dir = next(
        (
            parent / "RCL-Next" / "packages" / "database" / "drizzle"
            for parent in REPO_ROOT.parents
            if (parent / "RCL-Next" / "packages" / "database" / "drizzle").is_dir()
        ),
        None,
    )
    statements: list[str] = []
    for filename, fallback_sql in _FALLBACK_RCL_NEXT_DRIZZLE_SQL.items():
        if drizzle_dir is not None and (drizzle_dir / filename).is_file():
            raw_sql = (drizzle_dir / filename).read_text(encoding="utf-8")
        else:
            raw_sql = fallback_sql

        for chunk in raw_sql.split("--> statement-breakpoint"):
            active_lines = [
                line for line in chunk.splitlines() if not line.strip().startswith("--")
            ]
            stmt = "\n".join(active_lines).strip()
            if not stmt:
                continue
            if "btree_gist" in stmt or "EXCLUDE USING gist" in stmt:
                continue
            statements.append(stmt)
    return statements


def _load_migration_0004_module():
    migration_path = REPO_ROOT / "alembic" / "versions" / "0004_match_mvp_and_editor_role.py"
    spec = util.spec_from_file_location("migration_0004", migration_path)
    assert spec is not None and spec.loader is not None
    migration_0004 = util.module_from_spec(spec)
    spec.loader.exec_module(migration_0004)
    return migration_0004


async def _restore_migrated_db_state(migrated_db: AsyncEngine) -> None:
    """Restaura el esquema ``public`` al estado canónico de la fixture ``migrated_db``."""
    cfg = Config("alembic.ini")
    async with migrated_db.connect() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

        def _do_restore(sync_conn):
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")
            Base.metadata.create_all(sync_conn)

        await conn.run_sync(_do_restore)
        await conn.commit()


@pytest.mark.asyncio
async def test_web_mergebranch_migrations_then_discord_bot_migrations_coexist_on_pglite(
    migrated_db: AsyncEngine,
):
    """Aplicar las migraciones de RCL-Next (MergeBranch 0000..0004) y después las de
    DiscordBots (0000..0004) sobre la misma instancia de PGlite no lanza errores ni
    altera tablas, columnas o datos exclusivos de la web.
    """
    cfg = Config("alembic.ini")
    sd_id = uuid.uuid4()
    player_id = uuid.uuid4()
    team1_id = uuid.uuid4()
    team2_id = uuid.uuid4()
    match_id = uuid.uuid4()
    article_id = uuid.uuid4()

    try:
        async with migrated_db.connect() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            for stmt in _load_rcl_next_drizzle_statements():
                await conn.execute(text(stmt))

            # Sembrar datos del lado web (incluyendo rol editor, MVP y tabla exclusiva web)
            await conn.execute(text("INSERT INTO seasons (name) VALUES ('2026 Split 1')"))
            await conn.execute(
                text("INSERT INTO divisions (name, sort_order) VALUES ('PREMIER', 1)")
            )
            await conn.execute(
                text(
                    "INSERT INTO seasons_divisions (id, season_name, division_name) "
                    "VALUES (:id, '2026 Split 1', 'PREMIER')"
                ),
                {"id": sd_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO discord_users (discord_id, username, role) "
                    "VALUES ('900000000000000001', 'editor_web', 'editor')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO players (id, discord_user_id, game_name, riot_tag, is_main) "
                    "VALUES (:id, '900000000000000001', 'Caps', 'EUW', true)"
                ),
                {"id": player_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO teams ("
                    "  id, season_division_id, season_name, name, short_name, discord_role_id"
                    ") VALUES "
                    "  (:t1, :sd, '2026 Split 1', 'G2 Esports', 'G2', 710000000000000001), "
                    "  (:t2, :sd, '2026 Split 1', 'Fnatic', 'FNC', 710000000000000002)"
                ),
                {"t1": team1_id, "t2": team2_id, "sd": sd_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO rounds (id, id_season_division, stage, name) "
                    "VALUES (1, :sd, 'regular', 'Jornada 1')"
                ),
                {"sd": sd_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO matches ("
                    "  id, id_season_division, id_round, jornada, team1_id, team2_id, "
                    "  best_of, status, winner_team_id, team1_score, team2_score, "
                    "  mvp_player_id, mvp_score"
                    ") VALUES ("
                    "  :id, :sd, 1, 1, :t1, :t2, "
                    "  3, 'completed', :t1, 2, 1, "
                    "  :mvp, 91.5"
                    ")"
                ),
                {"id": match_id, "sd": sd_id, "t1": team1_id, "t2": team2_id, "mvp": player_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO editorial_articles (id, title, excerpt, body, kind, author) "
                    "VALUES ("
                    "  :id, 'Crónica J1', 'Resumen J1', 'Texto completo', 'noticia', 'Redacción'"
                    ")"
                ),
                {"id": article_id},
            )

            # Ejecutar todas las migraciones de DiscordBots (0000 -> 0004) sobre el esquema web
            def _run_bot_migrations(sync_conn):
                cfg.attributes["connection"] = sync_conn
                command.upgrade(cfg, "head")

            await conn.run_sync(_run_bot_migrations)
            await conn.commit()

        # Verificar estado tras la coexistencia de migraciones
        async with migrated_db.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0004"

            article_title = (
                await conn.execute(
                    text("SELECT title FROM editorial_articles WHERE id = :id"),
                    {"id": article_id},
                )
            ).scalar_one()
            assert article_title == "Crónica J1"

            web_match_row = (
                await conn.execute(
                    text(
                        "SELECT best_of, winner_team_id, team1_score, team2_score, "
                        "mvp_player_id, mvp_score "
                        "FROM matches WHERE id = :id"
                    ),
                    {"id": match_id},
                )
            ).one()
            assert web_match_row.best_of == 3
            assert web_match_row.winner_team_id == team1_id
            assert web_match_row.team1_score == 2
            assert web_match_row.team2_score == 1
            assert web_match_row.mvp_player_id == player_id
            assert web_match_row.mvp_score == Decimal("91.5")

            # Verificar carga y escritura desde el ORM de DiscordBots (AsyncSession)
            async_session = AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                editor_user = await async_session.get(DiscordUser, "900000000000000001")
                assert editor_user is not None
                assert editor_user.role == AppRole.EDITOR

                loaded_match = await async_session.get(Match, match_id)
                assert loaded_match is not None
                assert loaded_match.id_round == 1
                assert loaded_match.mvp_player_id == player_id
                assert loaded_match.mvp_score == Decimal("91.5")
                assert loaded_match.status == MatchStatus.COMPLETED

                # Comprobar que PostgreSQL rechaza mvp_score negativo
                with pytest.raises(IntegrityError):
                    async with async_session.begin_nested():
                        loaded_match.mvp_score = Decimal("-1.0")
                        await async_session.flush()
                loaded_match.mvp_score = Decimal("91.5")

                # Comprobar que PostgreSQL rechaza mvp_player_id con mvp_score NULL
                with pytest.raises(IntegrityError):
                    async with async_session.begin_nested():
                        loaded_match.mvp_score = None
                        await async_session.flush()
                loaded_match.mvp_score = Decimal("91.5")

                # Comprobar que eliminar el jugador MVP aplica ON DELETE SET NULL en matches
                mvp_player = await async_session.get(Player, player_id)
                assert mvp_player is not None
                await async_session.delete(mvp_player)
                await async_session.flush()
                await async_session.refresh(loaded_match)
                assert loaded_match.mvp_player_id is None
                assert loaded_match.mvp_score == Decimal("91.5")
            finally:
                await async_session.close()
                await conn.rollback()
    finally:
        await _restore_migrated_db_state(migrated_db)


@pytest.mark.asyncio
async def test_0004_migration_upgrades_pre_0004_schema_and_is_idempotent(
    migrated_db: AsyncEngine,
):
    """La migración 0004 crea rol 'editor', columnas MVP, FK, checks e índice parcial si
    faltaban en una BD previa a 0004, y volver a ejecutar su upgrade() es idempotente.
    """
    cfg = Config("alembic.ini")
    migration_0004 = _load_migration_0004_module()

    try:
        async with migrated_db.connect() as conn:
            # 1. Llevar la BD a 0003 y simular estado previo a 0004 (sin 'editor' ni objetos MVP)
            def _setup_pre_0004(sync_conn):
                cfg.attributes["connection"] = sync_conn
                command.downgrade(cfg, "0003")

            await conn.run_sync(_setup_pre_0004)
            await conn.execute(
                text(
                    "DELETE FROM pg_enum "
                    "WHERE enumtypid = 'app_role'::regtype AND enumlabel = 'editor'"
                )
            )
            await conn.execute(text("DROP INDEX IF EXISTS matches_round_mvp_score_idx"))
            await conn.execute(
                text("ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_mvp_player_score_check")
            )
            await conn.execute(
                text(
                    "ALTER TABLE matches "
                    "DROP CONSTRAINT IF EXISTS matches_mvp_score_nonnegative_check"
                )
            )
            await conn.execute(
                text("ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_mvp_player_id_fkey")
            )
            await conn.execute(text("ALTER TABLE matches DROP COLUMN IF EXISTS mvp_score"))
            await conn.execute(text("ALTER TABLE matches DROP COLUMN IF EXISTS mvp_player_id"))
            await conn.commit()

            # 2. Ejecutar upgrade a 0004 (head) y después re-ejecutar upgrade() de 0004
            await conn.run_sync(
                lambda sync_conn: _run_upgrade_twice(sync_conn, cfg, migration_0004)
            )
            await conn.commit()

            # 3. Comprobar que 'editor' y todos los objetos MVP existen tras el upgrade de 0004
            enum_labels = [
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT e.enumlabel "
                            "FROM pg_enum e "
                            "JOIN pg_type t ON e.enumtypid = t.oid "
                            "WHERE t.typname = 'app_role' "
                            "ORDER BY e.enumsortorder"
                        )
                    )
                ).fetchall()
            ]
            assert enum_labels == ["viewer", "editor", "admin", "owner"]

            cols = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("matches")}
            )
            assert {"mvp_player_id", "mvp_score"} <= cols

            fks = await conn.run_sync(
                lambda c: {fk["name"] for fk in inspect(c).get_foreign_keys("matches")}
            )
            assert "matches_mvp_player_id_fkey" in fks

            checks = await conn.run_sync(
                lambda c: {ck["name"] for ck in inspect(c).get_check_constraints("matches")}
            )
            assert {
                "matches_mvp_score_nonnegative_check",
                "matches_mvp_player_score_check",
            } <= checks

            indexes = await conn.run_sync(
                lambda c: {idx["name"] for idx in inspect(c).get_indexes("matches")}
            )
            assert "matches_round_mvp_score_idx" in indexes
    finally:
        await _restore_migrated_db_state(migrated_db)


def _run_upgrade_twice(sync_conn, cfg: Config, migration_0004) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    cfg.attributes["connection"] = sync_conn
    command.upgrade(cfg, "head")
    mc = MigrationContext.configure(sync_conn)
    with Operations.context(mc):
        migration_0004.upgrade()
