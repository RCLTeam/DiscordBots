"""Suite de integración contra un PostgreSQL real y desechable.

Existe porque PGlite, el motor de la suite normal, no empaqueta ``btree_gist``: sin esa
extensión no se puede crear ``one_competitive_team_per_player_and_season``, que es la
restricción de producción que el issue #63 puso al descubierto. Una suite verde en PGlite
no demuestra, por tanto, que el bot respete el esquema real.

Se activa con la variable de entorno ``LIGA_BOT_POSTGRES_DSN``; sin ella los tests se
omiten. Nunca debe apuntar a la base de datos compartida ni a la de producción: estos
tests crean y destruyen el esquema. Ver ``docs/testing/postgres-suite.md``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from liga_bot.config import Settings
from liga_bot.models.enums import RosterRole
from liga_bot.models.roster import (
    DiscordUser,
    DivisionModel,
    Season,
    SeasonDivision,
    TeamMembership,
)
from liga_bot.models.team import Team
from liga_bot.repositories.roster_repo import TeamMembershipRepository
from liga_bot.repositories.season_repo import current_season_name
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    RosterSyncService,
)

DSN_ENV = "LIGA_BOT_POSTGRES_DSN"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(DSN_ENV),
        reason=f"Define {DSN_ENV} con un PostgreSQL desechable para ejecutar esta suite.",
    ),
]

# Réplica de la parte de la migración de RCL-Next 0001_competitive_membership_per_season.sql
# (base de datos compartida) que PGlite no soporta. El resto lo crea nuestra 0000.
# Cada sentencia va suelta: asyncpg las prepara una a una.
EXCLUSION_STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
    """
    ALTER TABLE team_memberships
        ADD CONSTRAINT one_competitive_team_per_player_and_season
        EXCLUDE USING gist ("season_name" WITH =, "discord_user_id" WITH =)
        WHERE ("role" IN ('top', 'jungle', 'mid', 'adc', 'support', 'substitute'))
    """,
)


def _role(role_id: int, name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def _member(user_id: int, name: str = "Jugador") -> AsyncMock:
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.global_name = None
    member.display_name = name
    member.avatar = None
    member.roles = []
    return member


@pytest.fixture(scope="session")
def pg_schema() -> str:
    """Prepara el esquema una sola vez, con un driver síncrono.

    Alembic es síncrono y el esquema no depende del bucle de eventos: hacerlo aquí
    evita compartir un motor asíncrono entre bucles distintos (cada test tiene el suyo).
    """
    dsn = os.environ[DSN_ENV]
    sync_dsn = dsn.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_dsn)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    cfg = Config("alembic.ini")
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        conn.commit()

    with engine.begin() as conn:
        for statement in EXCLUSION_STATEMENTS:
            conn.execute(text(statement))
    engine.dispose()
    return dsn


@pytest_asyncio.fixture
async def pg_engine(pg_schema: str) -> AsyncGenerator[AsyncEngine, None]:
    """Motor asíncrono propio de cada test, sobre el esquema ya creado."""
    engine = create_async_engine(pg_schema)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session(pg_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Sesión por test con rollback: no deja residuos entre pruebas."""
    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


async def _seed_season(session: AsyncSession, season_name: str, starts_on: date) -> SeasonDivision:
    """Crea temporada, división y su seasons_divisions tal y como lo haría la web."""
    if await session.get(Season, season_name) is None:
        session.add(Season(name=season_name, starts_on=starts_on))
    if await session.get(DivisionModel, "PREMIER") is None:
        session.add(DivisionModel(name="PREMIER"))
    await session.flush()
    season_division = SeasonDivision(season_name=season_name, division_name="PREMIER")
    session.add(season_division)
    await session.flush()
    return season_division


async def _seed_team(session: AsyncSession, season_division: SeasonDivision, name: str) -> Team:
    team = Team(
        name=f"{name} {uuid.uuid4().hex[:6]}",
        tag=name[:4].upper(),
        season_division=season_division,
        discord_role_id=500_000_000_000_000_000 + uuid.uuid4().int % 10**12,
    )
    session.add(team)
    await session.flush()
    return team


async def _seed_user(session: AsyncSession, discord_id: str) -> DiscordUser:
    user = DiscordUser(discord_id=discord_id, username="jugador")
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
def service(pg_engine: AsyncEngine) -> RosterSyncService:
    """Servicio cuyas llamadas reciben siempre la sesión del test (session=...)."""
    return RosterSyncService(
        session_factory=async_sessionmaker(bind=pg_engine, expire_on_commit=False),
        settings=Settings(),
    )


@pytest.mark.asyncio
async def test_transfer_player_persiste_la_temporada(
    pg_session: AsyncSession, service: RosterSyncService
):
    """El caso del issue #63: el INSERT iba sin season_name y PostgreSQL lo rechazaba."""
    sd = await _seed_season(pg_session, "2026", date.today())
    team = await _seed_team(pg_session, sd, "Draconis")
    await _seed_user(pg_session, "1504481367679959141")

    membership, destino, origen = await service.transfer_player(
        member=_member(1504481367679959141),
        team_role=_role(team.discord_role_id, team.name),
        new_position=RosterRole.TOP,
        session=pg_session,
    )

    assert membership.season_name == "2026"
    assert destino.id == team.id
    assert origen is None


@pytest.mark.asyncio
async def test_traspaso_en_la_misma_temporada_libera_la_plaza(
    pg_session: AsyncSession, service: RosterSyncService
):
    """Sin liberar la anterior, la restricción de exclusión abortaría el traspaso."""
    sd = await _seed_season(pg_session, "2026", date.today())
    origen = await _seed_team(pg_session, sd, "Origen")
    destino = await _seed_team(pg_session, sd, "Destino")
    await _seed_user(pg_session, "900001")
    member = _member(900001)

    await service.transfer_player(
        member=member,
        team_role=_role(origen.discord_role_id, origen.name),
        new_position=RosterRole.MID,
        session=pg_session,
    )
    membership, equipo_destino, equipo_origen = await service.transfer_player(
        member=member,
        team_role=_role(destino.discord_role_id, destino.name),
        new_position=RosterRole.MID,
        session=pg_session,
    )

    assert equipo_destino.id == destino.id
    assert equipo_origen is not None and equipo_origen.id == origen.id
    restantes = (
        (
            await pg_session.execute(
                select(TeamMembership).where(TeamMembership.discord_user_id == "900001")
            )
        )
        .scalars()
        .all()
    )
    assert [m.team_id for m in restantes] == [destino.id]
    assert membership.season_name == "2026"


@pytest.mark.asyncio
async def test_la_plaza_de_otra_temporada_no_se_toca(
    pg_session: AsyncSession, service: RosterSyncService
):
    """La exclusividad competitiva es por temporada: el histórico se conserva."""
    pasada = await _seed_season(pg_session, "2025", date.today() - timedelta(days=400))
    actual = await _seed_season(pg_session, "2026", date.today())
    equipo_viejo = await _seed_team(pg_session, pasada, "Viejo")
    equipo_nuevo = await _seed_team(pg_session, actual, "Nuevo")
    await _seed_user(pg_session, "900002")
    member = _member(900002)

    await service.transfer_player(
        member=member,
        team_role=_role(equipo_viejo.discord_role_id, equipo_viejo.name),
        new_position=RosterRole.ADC,
        session=pg_session,
    )
    await service.transfer_player(
        member=member,
        team_role=_role(equipo_nuevo.discord_role_id, equipo_nuevo.name),
        new_position=RosterRole.ADC,
        session=pg_session,
    )

    temporadas = sorted(
        m.season_name
        for m in (
            await pg_session.execute(
                select(TeamMembership).where(TeamMembership.discord_user_id == "900002")
            )
        )
        .scalars()
        .all()
    )
    assert temporadas == ["2025", "2026"]


@pytest.mark.asyncio
async def test_cambio_de_posicion_falla_en_la_aplicacion_no_en_la_base_de_datos(
    pg_session: AsyncSession, service: RosterSyncService
):
    """El conflicto competitivo se detecta antes de llegar a la restricción de exclusión."""
    sd = await _seed_season(pg_session, "2026", date.today())
    equipo_a = await _seed_team(pg_session, sd, "Alfa")
    equipo_b = await _seed_team(pg_session, sd, "Beta")
    await _seed_user(pg_session, "900003")
    member = _member(900003)

    await service.transfer_player(
        member=member,
        team_role=_role(equipo_a.discord_role_id, equipo_a.name),
        new_position=RosterRole.SUPPORT,
        session=pg_session,
    )
    await service.transfer_player(
        member=member,
        team_role=_role(equipo_b.discord_role_id, equipo_b.name),
        new_position=RosterRole.COACH,
        session=pg_session,
    )

    with pytest.raises(CompetitivePositionConflictError):
        await service.change_player_position(
            team_id=equipo_b.id,
            discord_user_id="900003",
            new_role=RosterRole.MID,
            actor_id="900099",
            session=pg_session,
        )


@pytest.mark.asyncio
async def test_temporada_vigente_por_fechas(pg_session: AsyncSession):
    """El criterio de temporada vigente usa starts_on/ends_on, no una columna is_active."""
    hoy = date.today()
    pg_session.add(
        Season(name="2024", starts_on=hoy - timedelta(days=800), ends_on=hoy - timedelta(days=500))
    )
    pg_session.add(Season(name="2026", starts_on=hoy - timedelta(days=10)))
    await pg_session.flush()

    assert await current_season_name(pg_session) == "2026"


@pytest.mark.asyncio
async def test_restriccion_gist_rechaza_segunda_competitiva_en_misma_temporada(
    pg_session: AsyncSession,
):
    """La restricción GiST one_competitive_team_per_player_and_season actúa en PostgreSQL."""
    sd = await _seed_season(pg_session, "2026", date.today())
    equipo_a = await _seed_team(pg_session, sd, "AlfaGiST")
    equipo_b = await _seed_team(pg_session, sd, "BetaGiST")
    await _seed_user(pg_session, "900004")
    repo = TeamMembershipRepository(pg_session)

    await repo.create(
        team_id=equipo_a.id,
        discord_user_id="900004",
        role=RosterRole.MID,
    )

    with pytest.raises(IntegrityError):
        await repo.create(
            team_id=equipo_b.id,
            discord_user_id="900004",
            role=RosterRole.TOP,
        )
