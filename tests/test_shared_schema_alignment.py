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
