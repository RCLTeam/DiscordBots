"""Alineación de los modelos con el esquema de las tablas compartidas con RCL-Next.

Las tablas compartidas las crea la migración ``0000_initial_shared_tables`` (que reproduce
el esquema de RCL-Next) y los modelos SQLAlchemy deben describirlas con las mismas columnas,
los mismos tipos y la misma nulabilidad.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Date, DateTime, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import liga_bot.models  # noqa: F401
import liga_bot.models.roster  # noqa: F401
from liga_bot.models.base import Base
from liga_bot.models.match import Match
from liga_bot.models.roster import Season
from liga_bot.models.team import Team

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
# Restricciones de temporada (issue #63)
#
# En producción las creó la migración de RCL-Next 0001_competitive_membership_per_season.sql
# sobre la base de datos compartida; aquí las reproduce 0000_initial_shared_tables.
# ---------------------------------------------------------------------------

CONSTRAINTS_ESPERADAS = {
    "seasons_divisions": {"seasons_divisions_id_season_name_key"},
    "teams": {"teams_id_season_name_key", "teams_season_division_season_fkey"},
    "team_memberships": {"team_memberships_team_season_fkey"},
}


@pytest.mark.parametrize("table_name", sorted(CONSTRAINTS_ESPERADAS))
def test_modelo_declara_las_restricciones_de_temporada(table_name: str):
    """Las restricciones compuestas están en los modelos, no solo en la base de datos."""
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
    """Y la migración 0000 las crea en el esquema de pruebas."""
    async with migrated_db.connect() as conn:
        uniques = await conn.run_sync(lambda c: inspect(c).get_unique_constraints(table_name))
        fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys(table_name))

    en_bd = {u["name"] for u in uniques if u.get("name")} | {
        fk["name"] for fk in fks if fk.get("name")
    }
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
