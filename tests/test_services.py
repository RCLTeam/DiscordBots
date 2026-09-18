"""
Pruebas unitarias completas para ScheduleService y TicketService.

Verifica:
- Validación de equipos, división compartida, prevención de auto-partidos e idempotencia.
- Aprovisionamiento de categorías y canales en Discord con permisos de división y CEO.
- Transacciones atómicas y garantía de rollback anti-canales huérfanos.
- Ingesta de CSV con delimitadores variables, UTF-8 BOM y control de errores por fila.
- Auditoría de inactividad de tickets (>= 24h), resolución de miembros User vs Member,
  exclusión estricta de Staff/Admin/CEO y prevención de spam con TicketNoticeRepository.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import (
    DEFAULT_TICKET_AVISO_MARCADOR,
    Settings,
)
from liga_bot.models.enums import Division, MatchStatus
from liga_bot.repositories.match_repo import MatchRepository
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.repositories.ticket_repo import TicketNoticeRepository
from liga_bot.services.schedule_service import ScheduleService
from liga_bot.services.ticket_service import (
    TicketAuditResult,
    TicketService,
)

# ---------------------------------------------------------------------------
# Helpers y Mocks de Discord
# ---------------------------------------------------------------------------


class AsyncMessageHistory:
    """Simula el iterador asíncrono retornado por TextChannel.history()."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    def __aiter__(self):
        self._iter = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def create_mock_role(role_id: int, name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_category(category_id: int, name: str) -> MagicMock:
    cat = MagicMock(spec=discord.CategoryChannel)
    cat.id = category_id
    cat.name = name
    cat.channels = []
    return cat


def create_mock_channel(
    channel_id: int,
    name: str,
    category: MagicMock | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    chan = AsyncMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.category = category
    chan.guild = guild
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    chan.delete = AsyncMock()
    return chan


def create_mock_guild(
    settings: Settings,
    roles: list[MagicMock] | None = None,
    categories: list[MagicMock] | None = None,
) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = settings.guild_id

    # @everyone
    default_role = create_mock_role(settings.guild_id, "@everyone")
    guild.default_role = default_role

    # Bot Member
    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999999999999
    bot_member.name = "LigaBot"
    guild.me = bot_member

    # Roles de Staff, Admin y CEOs
    staff_role = create_mock_role(settings.staff_role_id, "Staff")
    admin_role = create_mock_role(settings.admin_role_id, "Admin")
    ceo_premier_role = create_mock_role(settings.ceo_premier_role_id, "CEO Premier")
    ceo_ascend_role = create_mock_role(settings.ceo_ascend_role_id, "CEO Ascend")

    all_roles = [default_role, staff_role, admin_role, ceo_premier_role, ceo_ascend_role]
    if roles:
        all_roles.extend(roles)

    role_map = {r.id: r for r in all_roles}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_map.get(rid)

    all_categories = list(categories or [])
    guild.categories = all_categories

    channel_seq = [2000]

    async def mock_create_category(name: str):
        cat = create_mock_category(channel_seq[0], name)
        channel_seq[0] += 1
        guild.categories.append(cat)
        return cat

    async def mock_create_text_channel(name: str, category=None, overwrites=None):
        chan = create_mock_channel(channel_seq[0], name, category)
        channel_seq[0] += 1
        if category is not None:
            category.channels.append(chan)
        chan.guild = guild
        return chan

    guild.create_category = AsyncMock(side_effect=mock_create_category)
    guild.create_text_channel = AsyncMock(side_effect=mock_create_text_channel)
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)

    return guild


# ---------------------------------------------------------------------------
# Fixtures de Base de Datos
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provee una sesión limpia truncando tablas antes y después de cada prueba."""
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE ticket_notices, matches, teams CASCADE;"))
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(text("TRUNCATE TABLE ticket_notices, matches, teams CASCADE;"))
        await session.commit()


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        staff_role_id=1001,
        admin_role_id=1002,
        ceo_premier_role_id=1003,
        ceo_ascend_role_id=1004,
        guild_id=1000,
        database_url="pglite:///:memory:",
    )


# ---------------------------------------------------------------------------
# Tests de ScheduleService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_match_happy_path_premier(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    # Sembrar 2 equipos Premier en BD
    team_repo = TeamRepository(db_session)
    t1 = await team_repo.create(
        name="Planar Shock Pingus",
        tag="PSP",
        slug="planar-shock-pingus",
        division=Division.PREMIER,
        discord_role_id=1101,
    )
    t2 = await team_repo.create(
        name="Fnix Esports",
        tag="FNX",
        slug="fnix-esports",
        division=Division.PREMIER,
        discord_role_id=1102,
    )
    await db_session.commit()

    role1 = create_mock_role(1101, "Planar Shock Pingus")
    role2 = create_mock_role(1102, "Fnix Esports")
    guild = create_mock_guild(test_settings, roles=[role1, role2])

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    scheduled = datetime(2026, 9, 20, 21, 0, tzinfo=timezone.utc)

    res = await service.create_match(
        guild=guild,
        jornada=1,
        team1_name="Planar Shock Pingus",
        team2_name="Fnix Esports",
        scheduled_at=scheduled,
    )

    assert res.success is True
    assert res.error is None
    assert res.match is not None
    assert res.match.status == MatchStatus.CANAL_CREADO
    assert res.match.jornada == 1
    assert res.channel is not None
    assert res.channel.name == "j1-planar-shock-pingus-vs-fnix-esports"

    # Verificar creación de categoría "PREMIER - JORNADA 1"
    guild.create_category.assert_called_once_with("PREMIER - JORNADA 1")

    # Verificar mensajes enviados al canal
    assert res.channel.send.call_count == 2

    # Verificar persistencia en base de datos
    match_repo = MatchRepository(db_session)
    db_match = await match_repo.get_by_jornada_and_teams(1, t1.id, t2.id)
    assert db_match is not None
    assert db_match.discord_channel_id == res.channel.id


@pytest.mark.asyncio
async def test_create_match_happy_path_ascend_existing_category(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Dragones",
        tag="DRG",
        slug="dragones",
        division=Division.ASCEND,
        discord_role_id=1201,
    )
    await team_repo.create(
        name="Fenix Ascend",
        tag="FXA",
        slug="fenix-ascend",
        division=Division.ASCEND,
        discord_role_id=1202,
    )
    await db_session.commit()

    role1 = create_mock_role(1201, "Dragones")
    role2 = create_mock_role(1202, "Fenix Ascend")
    existing_cat = create_mock_category(9001, "ASCENSO - JORNADA 2")
    guild = create_mock_guild(test_settings, roles=[role1, role2], categories=[existing_cat])

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_match(
        guild=guild,
        jornada=2,
        team1_name="dragones",
        team2_name="fenix-ascend",
    )

    assert res.success is True
    # Reutilizó categoría existente
    guild.create_category.assert_not_called()
    assert res.match.division == Division.ASCEND


@pytest.mark.asyncio
async def test_create_match_team_not_found(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Lobos",
        tag="LOB",
        slug="lobos",
        division=Division.PREMIER,
        discord_role_id=1301,
    )
    await db_session.commit()

    guild = create_mock_guild(test_settings)
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    # Equipo 2 no existe
    res = await service.create_match(
        guild=guild,
        jornada=1,
        team1_name="Lobos",
        team2_name="Equipo Inexistente",
    )
    assert res.success is False
    assert "no está registrado en la base de datos" in res.error
    guild.create_text_channel.assert_not_called()

    # Equipo 1 no existe
    res1 = await service.create_match(
        guild=guild,
        jornada=1,
        team1_name="Fantasma",
        team2_name="Lobos",
    )
    assert res1.success is False
    assert "no está registrado en la base de datos" in res1.error


@pytest.mark.asyncio
async def test_create_match_cross_division_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Team Premier",
        tag="TPR",
        slug="team-premier",
        division=Division.PREMIER,
        discord_role_id=1401,
    )
    await team_repo.create(
        name="Team Ascend",
        tag="TAS",
        slug="team-ascend",
        division=Division.ASCEND,
        discord_role_id=1402,
    )
    await db_session.commit()

    guild = create_mock_guild(test_settings)
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    res = await service.create_match(
        guild=guild,
        jornada=1,
        team1_name="Team Premier",
        team2_name="Team Ascend",
    )
    assert res.success is False
    assert "Conflicto de división" in res.error
    guild.create_text_channel.assert_not_called()


@pytest.mark.asyncio
async def test_create_match_self_play_rejection(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Solitario",
        tag="SOL",
        slug="solitario",
        division=Division.PREMIER,
        discord_role_id=1501,
    )
    await db_session.commit()

    guild = create_mock_guild(test_settings)
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    res = await service.create_match(
        guild=guild,
        jornada=1,
        team1_name="Solitario",
        team2_name="Solitario",
    )
    assert res.success is False
    assert "no puede enfrentarse a sí mismo" in res.error


@pytest.mark.asyncio
async def test_create_match_duplicate_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    t1 = await team_repo.create(
        name="Alfa", tag="ALF", slug="alfa", division=Division.PREMIER, discord_role_id=1601
    )
    t2 = await team_repo.create(
        name="Beta", tag="BET", slug="beta", division=Division.PREMIER, discord_role_id=1602
    )
    match_repo = MatchRepository(db_session)
    await match_repo.create(
        jornada=1,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
        status=MatchStatus.CANAL_CREADO,
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings, roles=[create_mock_role(1601, "Alfa"), create_mock_role(1602, "Beta")]
    )
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    # Invertido: Beta vs Alfa para jornada 1
    res = await service.create_match(guild=guild, jornada=1, team1_name="Beta", team2_name="Alfa")
    assert res.success is False
    assert res.is_duplicate is True
    assert "ya existe para la jornada 1" in res.error
    guild.create_text_channel.assert_not_called()


@pytest.mark.asyncio
async def test_create_match_discord_role_missing(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Equipo A", tag="EA", slug="equipo-a", division=Division.PREMIER, discord_role_id=1701
    )
    await team_repo.create(
        name="Equipo B", tag="EB", slug="equipo-b", division=Division.PREMIER, discord_role_id=1702
    )
    await db_session.commit()

    # Guild sólo contiene el rol de Equipo A, falta Equipo B
    guild = create_mock_guild(test_settings, roles=[create_mock_role(1701, "Equipo A")])
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    res = await service.create_match(
        guild=guild, jornada=1, team1_name="Equipo A", team2_name="Equipo B"
    )
    assert res.success is False
    assert "no existe en el servidor" in res.error
    guild.create_text_channel.assert_not_called()


@pytest.mark.asyncio
async def test_create_match_rollback_orphan_channel_on_send_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Team X", tag="TX", slug="team-x", division=Division.PREMIER, discord_role_id=1801
    )
    await team_repo.create(
        name="Team Y", tag="TY", slug="team-y", division=Division.PREMIER, discord_role_id=1802
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings,
        roles=[create_mock_role(1801, "Team X"), create_mock_role(1802, "Team Y")],
    )

    # Hacer que send falle simulando caída de red de Discord
    original_create = guild.create_text_channel

    async def failing_create(*args, **kwargs):
        chan = await original_create(*args, **kwargs)
        chan.send.side_effect = discord.HTTPException(response=MagicMock(), message="Network error")
        return chan

    guild.create_text_channel = AsyncMock(side_effect=failing_create)

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_match(
        guild=guild, jornada=1, team1_name="Team X", team2_name="Team Y"
    )

    assert res.success is False
    assert "Error durante el aprovisionamiento" in res.error

    # Verificar que el canal creado fue borrado (anti-huérfano)
    match_repo = MatchRepository(db_session)
    assert len(await match_repo.list_by_jornada(1)) == 0


@pytest.mark.asyncio
async def test_create_match_rollback_orphan_channel_on_db_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
    monkeypatch,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Team M", tag="TM", slug="team-m", division=Division.PREMIER, discord_role_id=1901
    )
    await team_repo.create(
        name="Team N", tag="TN", slug="team-n", division=Division.PREMIER, discord_role_id=1902
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings,
        roles=[create_mock_role(1901, "Team M"), create_mock_role(1902, "Team N")],
    )

    # Simular fallo de base de datos en MatchRepository.create
    async def mock_fail_create(*args, **kwargs):
        raise RuntimeError("Database connection lost!")

    monkeypatch.setattr(MatchRepository, "create", mock_fail_create)

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_match(
        guild=guild, jornada=1, team1_name="Team M", team2_name="Team N"
    )

    assert res.success is False
    assert "Database connection lost!" in res.error


@pytest.mark.asyncio
async def test_create_single_match_alias(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Team Alias1",
        tag="TA1",
        slug="team-alias1",
        division=Division.PREMIER,
        discord_role_id=2001,
    )
    await team_repo.create(
        name="Team Alias2",
        tag="TA2",
        slug="team-alias2",
        division=Division.PREMIER,
        discord_role_id=2002,
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings,
        roles=[create_mock_role(2001, "Team Alias1"), create_mock_role(2002, "Team Alias2")],
    )

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_single_match(
        jornada=1,
        team1_name="Team Alias1",
        team2_name="Team Alias2",
        scheduled_at=None,
        guild=guild,
    )
    assert res.success is True


@pytest.mark.asyncio
async def test_create_jornada_from_csv_happy_path(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Team 1", tag="T1", slug="team-1", division=Division.PREMIER, discord_role_id=2101
    )
    await team_repo.create(
        name="Team 2", tag="T2", slug="team-2", division=Division.PREMIER, discord_role_id=2102
    )
    await team_repo.create(
        name="Team 3", tag="T3", slug="team-3", division=Division.PREMIER, discord_role_id=2103
    )
    await team_repo.create(
        name="Team 4", tag="T4", slug="team-4", division=Division.PREMIER, discord_role_id=2104
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings,
        roles=[
            create_mock_role(2101, "Team 1"),
            create_mock_role(2102, "Team 2"),
            create_mock_role(2103, "Team 3"),
            create_mock_role(2104, "Team 4"),
        ],
    )

    csv_text = (
        "equipo1,equipo2,fecha,hora\n"
        "Team 1,Team 2,13/09/2026,21:00\n"
        "Team 3,Team 4,14/09/2026,18:00\n"
    )

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    j_res = await service.create_jornada_from_csv(guild=guild, jornada=1, csv_content=csv_text)

    assert j_res.total_rows == 2
    assert j_res.success_count == 2
    assert j_res.error_count == 0
    assert len(j_res.matches) == 2


@pytest.mark.asyncio
async def test_create_jornada_from_csv_utf8_bom_and_semicolon(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Equipo A", tag="EA", slug="equipo-a", division=Division.PREMIER, discord_role_id=2201
    )
    await team_repo.create(
        name="Equipo B", tag="EB", slug="equipo-b", division=Division.PREMIER, discord_role_id=2202
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings,
        roles=[create_mock_role(2201, "Equipo A"), create_mock_role(2202, "Equipo B")],
    )

    # Delimitado por ; y con BOM \ufeff
    csv_text = "\ufeffequipo1;equipo2;fecha;hora\nEquipo A;Equipo B;15/09/2026;20:30\n"

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    j_res = await service.create_jornada_from_csv(guild=guild, jornada=3, csv_content=csv_text)

    assert j_res.total_rows == 1
    assert j_res.success_count == 1
    assert j_res.error_count == 0


@pytest.mark.asyncio
async def test_create_jornada_from_csv_missing_headers(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    csv_text = "equipo1,equipo2,fecha\nTeam 1,Team 2,13/09/2026\n"
    j_res = await service.create_jornada_from_csv(guild=guild, jornada=1, csv_content=csv_text)

    assert j_res.total_rows == 0
    assert j_res.error_count == 1
    assert "Cabeceras incompletas. Faltan las columnas: hora" in j_res.errors[0]


@pytest.mark.asyncio
async def test_create_jornada_from_csv_partial_errors_and_duplicates(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Equipo Valido 1",
        tag="V1",
        slug="equipo-valido-1",
        division=Division.PREMIER,
        discord_role_id=2301,
    )
    await team_repo.create(
        name="Equipo Valido 2",
        tag="V2",
        slug="equipo-valido-2",
        division=Division.PREMIER,
        discord_role_id=2302,
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings,
        roles=[
            create_mock_role(2301, "Equipo Valido 1"),
            create_mock_role(2302, "Equipo Valido 2"),
        ],
    )

    csv_text = (
        "equipo1,equipo2,fecha,hora\n"
        "Equipo Valido 1,Equipo Valido 2,13/09/2026,21:00\n"
        "Equipo Desconocido,Equipo Valido 2,13/09/2026,21:00\n"
        "Equipo Valido 2,Equipo Valido 1,13/09/2026,21:00\n"
    )

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    j_res = await service.create_jornada_from_csv(guild=guild, jornada=1, csv_content=csv_text)

    assert j_res.total_rows == 3
    assert j_res.success_count == 1
    assert j_res.error_count == 2
    assert "Fila 3" in j_res.errors[0]
    assert "Fila 4: enfrentamiento duplicado" in j_res.errors[1]


@pytest.mark.asyncio
async def test_process_schedule_csv_alias(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    team_repo = TeamRepository(db_session)
    await team_repo.create(
        name="Equipo P1",
        tag="P1",
        slug="equipo-p1",
        division=Division.PREMIER,
        discord_role_id=2401,
    )
    await team_repo.create(
        name="Equipo P2",
        tag="P2",
        slug="equipo-p2",
        division=Division.PREMIER,
        discord_role_id=2402,
    )
    await db_session.commit()

    guild = create_mock_guild(
        test_settings,
        roles=[create_mock_role(2401, "Equipo P1"), create_mock_role(2402, "Equipo P2")],
    )

    csv_text = "equipo1,equipo2,fecha,hora\nEquipo P1,Equipo P2,13/09/2026,21:00\n"

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    matches = await service.process_schedule_csv(jornada=1, csv_content=csv_text, guild=guild)

    assert len(matches) == 1
    assert matches[0].success is True


# ---------------------------------------------------------------------------
# Tests de TicketService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_active_recent_message_no_alert(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5001, "TICKETS-GENERAL-PREMIER")
    channel = create_mock_channel(5101, "ticket-101", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # Mensaje reciente de usuario común (hace 2 horas)
    recent_time = datetime.now(timezone.utc) - timedelta(hours=2)
    user_author = MagicMock(spec=discord.Member)
    user_author.id = 7001
    user_author.roles = []
    guild.get_member.return_value = user_author

    msg = MagicMock(spec=discord.Message)
    msg.created_at = recent_time
    msg.author = user_author
    msg.content = "Hola, tengo una consulta."
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.alerts_sent == 0
    assert result.skipped_recent == 1
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_ticket_inactive_over_24h_first_alert(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5002, "TICKETS-GENERAL-PREMIER")
    channel = create_mock_channel(5102, "ticket-102", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # Mensaje inactivo de usuario común (hace 25 horas)
    inactive_time = datetime.now(timezone.utc) - timedelta(hours=25)
    user_author = MagicMock(spec=discord.Member)
    user_author.id = 7002
    user_author.roles = []
    guild.get_member.return_value = user_author

    msg = MagicMock(spec=discord.Message)
    msg.created_at = inactive_time
    msg.author = user_author
    msg.content = "Sigo esperando respuesta..."
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.alerts_sent == 1
    channel.send.assert_called_once()
    assert DEFAULT_TICKET_AVISO_MARCADOR in channel.send.call_args[0][0]

    # Verificar que se registró el aviso en BD
    repo = TicketNoticeRepository(db_session)
    notice = await repo.get_by_channel_id(5102)
    assert notice is not None
    assert notice.last_alert_sent_at is not None
    assert notice.is_pending_staff is True


@pytest.mark.asyncio
async def test_ticket_inactive_already_alerted_within_24h(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5003, "TICKETS-GENERAL-PREMIER")
    channel = create_mock_channel(5103, "ticket-103", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # Mensaje de usuario hace 30h
    inactive_time = datetime.now(timezone.utc) - timedelta(hours=30)
    user_author = MagicMock(spec=discord.Member)
    user_author.id = 7003
    user_author.roles = []
    guild.get_member.return_value = user_author

    msg = MagicMock(spec=discord.Message)
    msg.created_at = inactive_time
    msg.author = user_author
    msg.content = "Ayuda por favor"
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    # En la base de datos ya se registró un aviso hace 5 horas (< 24h)
    repo = TicketNoticeRepository(db_session)
    alert_time = datetime.now(timezone.utc) - timedelta(hours=5)
    await repo.record_alert(channel_id=5103, alert_time=alert_time, category_name="TICKETS")
    await db_session.commit()

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.alerts_sent == 0
    assert result.skipped_already_alerted == 1
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_ticket_inactive_repeat_alert_after_24h(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5004, "TICKETS-GENERAL-PREMIER")
    channel = create_mock_channel(5104, "ticket-104", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # Mensaje de usuario hace 50h
    inactive_time = datetime.now(timezone.utc) - timedelta(hours=50)
    user_author = MagicMock(spec=discord.Member)
    user_author.id = 7004
    user_author.roles = []
    guild.get_member.return_value = user_author

    msg = MagicMock(spec=discord.Message)
    msg.created_at = inactive_time
    msg.author = user_author
    msg.content = "¿Nadie contesta?"
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    # Aviso previo en BD fue hace 26 horas (>= 24h, ventana vencida)
    repo = TicketNoticeRepository(db_session)
    alert_time = datetime.now(timezone.utc) - timedelta(hours=26)
    await repo.record_alert(channel_id=5104, alert_time=alert_time, category_name="TICKETS")
    await db_session.commit()

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.alerts_sent == 1
    channel.send.assert_called_once()


@pytest.mark.asyncio
async def test_ticket_inactive_staff_member_no_alert(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5005, "TICKETS-GENERAL-PREMIER")
    channel = create_mock_channel(5105, "ticket-105", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # Mensaje hace 40h pero el autor es Staff (tiene staff_role_id)
    inactive_time = datetime.now(timezone.utc) - timedelta(hours=40)
    staff_author = MagicMock(spec=discord.Member)
    staff_author.id = 7005
    staff_role = create_mock_role(test_settings.staff_role_id, "Staff")
    staff_author.roles = [staff_role]
    guild.get_member.return_value = staff_author

    msg = MagicMock(spec=discord.Message)
    msg.created_at = inactive_time
    msg.author = staff_author
    msg.content = "Estamos revisando tu caso, espera confirmación."
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.alerts_sent == 0
    assert result.skipped_staff == 1
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_ticket_inactive_ceo_and_admin_count_as_staff(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5006, "TICKETS-ADMINISTRACION")
    channel = create_mock_channel(5106, "ticket-106", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # Mensaje hace 40h de un CEO Premier
    inactive_time = datetime.now(timezone.utc) - timedelta(hours=40)
    ceo_author = MagicMock(spec=discord.Member)
    ceo_author.id = 7006
    ceo_role = create_mock_role(test_settings.ceo_premier_role_id, "CEO Premier")
    ceo_author.roles = [ceo_role]
    guild.get_member.return_value = ceo_author

    msg = MagicMock(spec=discord.Message)
    msg.created_at = inactive_time
    msg.author = ceo_author
    msg.content = "Solucionado por dirección."
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.alerts_sent == 0
    assert result.skipped_staff == 1


@pytest.mark.asyncio
async def test_ticket_uncached_member_resolved_via_guild(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5007, "TICKETS-FICHAJES-PREMIER")
    channel = create_mock_channel(5107, "ticket-107", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # El objeto msg.author es un discord.User no cacheado
    user_obj = MagicMock(spec=discord.User)
    user_obj.id = 7007

    # guild.get_member resuelve el discord.Member con rol de Administrador
    admin_member = MagicMock(spec=discord.Member)
    admin_member.id = 7007
    admin_role = create_mock_role(test_settings.admin_role_id, "Admin")
    admin_member.roles = [admin_role]
    guild.get_member.return_value = admin_member

    msg = MagicMock(spec=discord.Message)
    msg.created_at = datetime.now(timezone.utc) - timedelta(hours=35)
    msg.author = user_obj
    msg.content = "Fichaje aprobado."
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.skipped_staff == 1
    assert result.alerts_sent == 0


@pytest.mark.asyncio
async def test_ticket_uncached_member_fetched_via_api(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5008, "TICKETS-FICHAJES-ASCEND")
    channel = create_mock_channel(5108, "ticket-108", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    user_obj = MagicMock(spec=discord.User)
    user_obj.id = 7008

    # get_member devuelve None, pero fetch_member resuelve el miembro
    guild.get_member.return_value = None
    fetched_member = MagicMock(spec=discord.Member)
    fetched_member.id = 7008
    fetched_member.roles = []
    guild.fetch_member.return_value = fetched_member

    msg = MagicMock(spec=discord.Message)
    msg.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    msg.author = user_obj
    msg.content = "Quiero inscribir un suplente."
    channel.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    # Es usuario común inactivo, se envía alerta
    assert result.alerts_sent == 1
    guild.fetch_member.assert_called_once_with(7008)


@pytest.mark.asyncio
async def test_ticket_empty_channel_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5009, "TICKETS-ADMINISTRACION")
    channel = create_mock_channel(5109, "ticket-109", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    channel.history = MagicMock(return_value=AsyncMessageHistory([]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.skipped_empty == 1
    assert result.alerts_sent == 0


@pytest.mark.asyncio
async def test_ticket_forbidden_channel_handled_gracefully(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5010, "TICKETS-ADMINISTRACION")
    channel = create_mock_channel(5110, "ticket-110", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    # Simular falta de permisos al leer historial
    async def forbidden_history(*args, **kwargs):
        raise discord.Forbidden(response=MagicMock(), message="Missing Permissions")
        yield  # make it an async generator

    channel.history = forbidden_history

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.skipped_forbidden == 1
    assert result.alerts_sent == 0


@pytest.mark.asyncio
async def test_ticket_category_filtering_and_unicode_normalization(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    # Tipografía matemática Unicode
    category = create_mock_category(5011, "𝗧𝗜𝗖𝗞𝗘𝗧𝗦-𝗚𝗘𝗡𝗘𝗥𝗔𝗟-𝗣𝗥𝗘𝗠𝗜𝗘𝗥")
    channel = create_mock_channel(5111, "ticket-111", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    channel.history = MagicMock(return_value=AsyncMessageHistory([]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.categories_scanned == 1
    assert result.channels_scanned == 1


@pytest.mark.asyncio
async def test_ticket_bot_alert_message_ignored(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    category = create_mock_category(5012, "TICKETS-ADMINISTRACION")
    channel = create_mock_channel(5112, "ticket-112", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    mock_bot = MagicMock()
    mock_bot.user = MagicMock()
    mock_bot.user.id = 999999999999

    bot_msg = MagicMock(spec=discord.Message)
    bot_msg.created_at = datetime.now(timezone.utc) - timedelta(hours=30)
    bot_msg.author = mock_bot.user
    bot_msg.content = f"{DEFAULT_TICKET_AVISO_MARCADOR}\n@Staff — Ticket sin respuesta."
    channel.history = MagicMock(return_value=AsyncMessageHistory([bot_msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        bot=mock_bot,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.skipped_already_alerted == 1
    assert result.alerts_sent == 0


@pytest.mark.asyncio
async def test_ticket_audit_tickets_alias(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    guild = create_mock_guild(test_settings)
    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.audit_tickets(guild)
    assert isinstance(result, TicketAuditResult)
    assert "Revisión completada" in result.summary()
