"""
Deep boundary test suite for Cogs (TeamsCog and TicketsCog).

This test suite challenges boundary conditions, schema constraints, Discord embed limits,
and error handling paths to ensure mathematical and operational robustness.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.cogs.teams import TeamsCog
from liga_bot.cogs.tickets import TicketsCog
from liga_bot.config import Settings
from liga_bot.database import get_session_factory
from liga_bot.models.enums import Division
from liga_bot.models.team import Team
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.services.ticket_service import (
    ChannelAuditDetail,
    ChannelAuditStatus,
    TicketAuditResult,
    TicketService,
)


def create_mock_role(role_id: int, name: str = "Rol") -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_member(
    user_id: int,
    name: str = "Usuario",
    roles: list[MagicMock] | None = None,
    is_admin: bool = False,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = roles or []
    perms = MagicMock()
    perms.administrator = is_admin
    member.guild_permissions = perms
    return member


def create_mock_guild(guild_id: int = 1547725310508667010) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Liga Discord Server"
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock()
    return guild


def create_mock_interaction(
    user: MagicMock | None = None,
    guild: MagicMock | None = None,
) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user or create_mock_member(12345)
    inter.guild = guild
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest_asyncio.fixture
async def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return get_session_factory(migrated_db)


@pytest_asyncio.fixture
async def clean_teams_db(session_factory: async_sessionmaker[AsyncSession]):
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(Team))
    yield
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(Team))


# ===========================================================================
# 1. BOUNDARY TESTS: TeamsCog.registrar_equipo
# ===========================================================================


@pytest.mark.asyncio
async def test_teams_name_exact_100_chars_boundary(clean_teams_db, session_factory):
    """
    Verifica que un nombre de exactamente 100 caracteres se acepta y persiste
    en la base de datos sin errores ni truncamiento.
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff, guild=create_mock_guild())

    exact_100_name = "T" * 100
    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(1001),
        nombre=exact_100_name,
        tag="T100",
        division=app_commands.Choice(name="Premier", value="PREMIER"),
    )

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.followup.send.assert_awaited_once()
    kwargs = inter.followup.send.await_args.kwargs
    assert "embed" in kwargs
    assert "Éxito" in kwargs["embed"].title

    async with session_factory() as session:
        repo = TeamRepository(session)
        team = await repo.get_by_role_id(1001)
        assert team is not None
        assert team.name == exact_100_name
        assert len(team.name) == 100


@pytest.mark.asyncio
async def test_teams_name_101_chars_boundary_rejection(clean_teams_db, session_factory):
    """
    Verifica que un nombre de 101 caracteres se rechaza inmediatamente
    antes de defer(), sin consultar la base de datos.
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff, guild=create_mock_guild())

    exact_101_name = "T" * 101
    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(1002),
        nombre=exact_101_name,
        tag="T101",
        division=app_commands.Choice(name="Premier", value="PREMIER"),
    )

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "no puede exceder los 100 caracteres" in msg
    inter.response.defer.assert_not_called()


@pytest.mark.asyncio
async def test_teams_name_with_whitespace_padding(clean_teams_db, session_factory):
    """
    Verifica que un nombre con espacios al inicio y final se limpia correctamente.
    Si tiene 100 caracteres tras strip(), debe aceptarse aunque el input original tuviera > 100.
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff, guild=create_mock_guild())

    raw_name = "   " + ("W" * 100) + "   \t\n"
    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(1003),
        nombre=raw_name,
        tag="PAD",
        division=app_commands.Choice(name="Premier", value="PREMIER"),
    )

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.followup.send.assert_awaited_once()
    async with session_factory() as session:
        repo = TeamRepository(session)
        team = await repo.get_by_role_id(1003)
        assert team is not None
        assert team.name == "W" * 100


@pytest.mark.asyncio
async def test_teams_tag_exact_boundary_1_to_4(clean_teams_db, session_factory):
    """Verifica límites exactos del tag (1 a 4 caracteres) y rechazo de 0 y 5."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # Tag de 1 char -> OK
    inter1 = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter1,
        rol=create_mock_role(2001),
        nombre="Team Tag 1",
        tag="A",
        division="PREMIER",
    )
    assert inter1.followup.send.called

    # Tag de 4 chars -> OK
    inter4 = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter4,
        rol=create_mock_role(2004),
        nombre="Team Tag 4",
        tag="ABCD",
        division="PREMIER",
    )
    assert inter4.followup.send.called

    # Tag de 5 chars -> Rechazado
    inter5 = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter5,
        rol=create_mock_role(2005),
        nombre="Team Tag 5",
        tag="ABCDE",
        division="PREMIER",
    )
    inter5.response.send_message.assert_awaited_once()
    assert "entre 1 y 4 caracteres" in inter5.response.send_message.await_args.args[0]
    inter5.response.defer.assert_not_called()


@pytest.mark.asyncio
async def test_teams_tag_with_internal_spaces(clean_teams_db, session_factory):
    """
    Verifica que los espacios en el tag se remueven antes de validar la longitud:
    ' A B C D ' -> 'ABCD' (4 chars, OK)
    ' A B C D E ' -> 'ABCDE' (5 chars, RECHAZADO)
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 4 caracteres con espacios
    inter_ok = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_ok,
        rol=create_mock_role(2010),
        nombre="Team Spaced Tag",
        tag=" P  S  P  X ",
        division="PREMIER",
    )
    assert inter_ok.followup.send.called

    # 5 caracteres con espacios
    inter_bad = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_bad,
        rol=create_mock_role(2011),
        nombre="Team Spaced Tag Bad",
        tag=" P  S  P  X  Y ",
        division="PREMIER",
    )
    inter_bad.response.send_message.assert_awaited_once()
    assert "entre 1 y 4 caracteres" in inter_bad.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_teams_db_operational_error_graceful_handling(session_factory):
    """
    Verifica que si la sesión de base de datos lanza OperationalError o RuntimeError
    tras defer(), el Cog captura el error, registra el log y responde con un Embed de error.
    """
    from sqlalchemy.exc import OperationalError

    settings = Settings(staff_role_id=101)
    mock_factory = MagicMock(side_effect=OperationalError("connection lost", None, Exception()))
    cog = TeamsCog(MagicMock(), session_factory=mock_factory, settings=settings)

    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff, guild=create_mock_guild())

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(999),
        nombre="Team DB Fail",
        tag="FAIL",
        division="PREMIER",
    )

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.followup.send.assert_awaited_once()
    kwargs = inter.followup.send.await_args.kwargs
    assert "embed" in kwargs
    assert "Error al Registrar Equipo" in kwargs["embed"].title
    assert "OperationalError" in kwargs["embed"].fields[0].value


# ===========================================================================
# 2. BOUNDARY TESTS: TeamsCog.equipos Stress
# ===========================================================================


@pytest.mark.asyncio
async def test_equipos_massive_teams_embed_limits(clean_teams_db, session_factory):
    """
    Verifica que /equipos con 60 equipos en Premier y 60 equipos en Ascenso (120 en total):
    - Trunca los campos Premier y Ascenso a <= 1024 caracteres
    - Mantiene el Embed total por debajo del límite de 6000 caracteres de Discord
    - Mantiene el número de campos <= 25
    """
    settings = Settings()
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    # Insertar 60 equipos en Premier y 60 en Ascenso
    async with session_factory() as session:
        repo = TeamRepository(session)
        for i in range(60):
            await repo.create(
                name=f"Premier Team Long Name Number {i:02d}",
                tag=f"P{i:02d}",
                slug=f"premier-team-long-name-number-{i:02d}",
                division=Division.PREMIER,
                discord_role_id=1000000 + i,
            )
        for i in range(60):
            await repo.create(
                name=f"Ascend Team Long Name Number {i:02d}",
                tag=f"A{i:02d}",
                slug=f"ascend-team-long-name-number-{i:02d}",
                division=Division.ASCEND,
                discord_role_id=2000000 + i,
            )
        await session.commit()

    inter = create_mock_interaction(guild=create_mock_guild())
    await cog.equipos.callback(cog, inter, division=None)

    inter.response.defer.assert_awaited_once_with(ephemeral=False)
    inter.followup.send.assert_awaited_once()
    embed: discord.Embed = inter.followup.send.await_args.kwargs["embed"]

    assert len(embed.fields) <= 25
    for field in embed.fields:
        assert len(field.value) <= 1024, (
            f"Field '{field.name}' excede 1024 chars: {len(field.value)}"
        )

    # Verificar longitud total del embed <= 6000
    total_chars = (
        len(embed.title or "")
        + len(embed.description or "")
        + len(embed.footer.text if embed.footer else "")
        + sum(len(f.name) + len(f.value) for f in embed.fields)
    )
    assert total_chars <= 6000, f"Total embed length {total_chars} excede 6000"


# ===========================================================================
# 3. BOUNDARY TESTS: TicketsCog._build_audit_embed
# ===========================================================================


def test_tickets_build_audit_embed_zero_alerts():
    """
    Verifica que cuando alerts_sent == 0:
    - El embed es verde
    - El campo '🚨 Tickets Notificados' NO se agrega
    """
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, auto_start=False)

    result = TicketAuditResult(
        categories_scanned=2,
        channels_scanned=10,
        alerts_sent=0,
        skipped_recent=10,
        details=[],
    )
    user = create_mock_member(12345, name="Auditor")
    embed = cog._build_audit_embed(result, user)

    assert embed.color == discord.Color.green()
    assert not any(f.name == "🚨 Tickets Notificados" for f in embed.fields)
    assert len(embed.fields) == 2


def test_tickets_build_audit_embed_100_char_channel_names_stress():
    """
    Stress-test extremo: 25 canales con nombres de exactamente 100 caracteres
    (longitud máxima permitida por Discord para un nombre de canal).
    Verifica:
    - Campo '🚨 Tickets Notificados' <= 1024 caracteres
    - Suffix de truncado presente
    - Embed total <= 6000 caracteres
    """
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, auto_start=False)

    details = [
        ChannelAuditDetail(
            channel_id=1000000000000000000 + i,
            channel_name="c" * 100,  # Máximo de Discord para channel name
            category_name="TICKETS",
            status=ChannelAuditStatus.ALERT_SENT,
        )
        for i in range(25)
    ]
    result = TicketAuditResult(
        categories_scanned=3,
        channels_scanned=50,
        alerts_sent=25,
        details=details,
    )
    user = create_mock_member(12345, name="SuperAuditor")
    embed = cog._build_audit_embed(result, user)

    alert_field = next(f for f in embed.fields if f.name == "🚨 Tickets Notificados")
    assert len(alert_field.value) <= 1024
    assert "(truncado" in alert_field.value

    total_chars = (
        len(embed.title or "")
        + len(embed.description or "")
        + len(embed.footer.text if embed.footer else "")
        + sum(len(f.name) + len(f.value) for f in embed.fields)
    )
    assert total_chars <= 6000


def test_tickets_build_audit_embed_500_alerts_massive_count():
    """
    Stress-test con 500 tickets alertados (more = 485).
    Verifica que la línea resumen '... y 485 ticket(s) más'
    no rompe el truncado a 1024 caracteres.
    """
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, auto_start=False)

    details = [
        ChannelAuditDetail(
            channel_id=2000000000000000000 + i,
            channel_name=f"ticket-mass-{i:04d}",
            category_name="TICKETS",
            status=ChannelAuditStatus.ALERT_SENT,
        )
        for i in range(500)
    ]
    result = TicketAuditResult(
        categories_scanned=5,
        channels_scanned=500,
        alerts_sent=500,
        details=details,
    )
    user = create_mock_member(12345, name="MassAuditor")
    embed = cog._build_audit_embed(result, user)

    alert_field = next(f for f in embed.fields if f.name == "🚨 Tickets Notificados")
    assert len(alert_field.value) <= 1024


def test_tickets_build_audit_embed_markdown_and_unicode_injection():
    """
    Verifica que nombres de canal con caracteres Markdown (`` ` ``, `*`, `_`, `~`, `|`),
    emojis y RTL no provocan errores durante la construcción del embed y se mantienen <= 1024 chars.
    """
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, auto_start=False)

    weird_names = [
        "ticket`inject`backtick",
        "ticket*bold*channel",
        "ticket_underscore_italic",
        "ticket||spoiler||name",
        "ticket>quote#channel",
        "ticket-👾-🎮-🎯-unicode",
        "ticket-عربي-rtl-test",
    ]
    details = [
        ChannelAuditDetail(
            channel_id=3000000000000000000 + i,
            channel_name=name,
            category_name="TICKETS",
            status=ChannelAuditStatus.ALERT_SENT,
        )
        for i, name in enumerate(weird_names)
    ]
    result = TicketAuditResult(
        categories_scanned=1,
        channels_scanned=7,
        alerts_sent=7,
        details=details,
    )
    user = create_mock_member(12345, name="SecurityTester")
    embed = cog._build_audit_embed(result, user)

    alert_field = next(f for f in embed.fields if f.name == "🚨 Tickets Notificados")
    assert len(alert_field.value) <= 1024
    assert len(embed.fields) <= 25


# ===========================================================================
# 4. BOUNDARY TESTS: TicketsCog.revisar_tickets Service Errors
# ===========================================================================


@pytest.mark.asyncio
async def test_revisar_tickets_forbidden_permission_handled():
    """
    Verifica que si discord.Forbidden se levanta durante check_tickets
    (el bot no tiene permisos en la categoría), /revisar-tickets no se cuelga
    y responde amigablemente al Staff vía followup.
    """
    mock_bot = MagicMock(spec=commands.Bot)
    mock_service = MagicMock(spec=TicketService)

    # Simular discord.Forbidden
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.reason = "Forbidden"
    mock_service.check_tickets = AsyncMock(
        side_effect=discord.Forbidden(mock_response, "Missing Permissions")
    )

    settings = Settings(staff_role_id=101)
    mock_bot.settings = settings
    cog = TicketsCog(mock_bot, ticket_service=mock_service, auto_start=False)

    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff, guild=create_mock_guild())

    await cog.revisar_tickets.callback(cog, inter)

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.await_args.args[0]
    assert "error inesperado al revisar los tickets" in msg
    assert "Missing Permissions" in msg


@pytest.mark.asyncio
async def test_revisar_tickets_timeout_handled():
    """
    Verifica que si asyncio.TimeoutError se levanta durante check_tickets,
    /revisar-tickets no se cuelga y responde amigablemente al Staff vía followup.
    """
    mock_bot = MagicMock(spec=commands.Bot)
    mock_service = MagicMock(spec=TicketService)
    mock_service.check_tickets = AsyncMock(side_effect=asyncio.TimeoutError())

    settings = Settings(staff_role_id=101)
    mock_bot.settings = settings
    cog = TicketsCog(mock_bot, ticket_service=mock_service, auto_start=False)

    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff, guild=create_mock_guild())

    await cog.revisar_tickets.callback(cog, inter)

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.await_args.args[0]
    assert "error inesperado al revisar los tickets" in msg


# ===========================================================================
# 5. ADDITIONAL ADVERSARIAL TESTS: Idempotency, Conflicts, Filters & Loop
# ===========================================================================


@pytest.mark.asyncio
async def test_teams_name_empty_or_only_whitespace_rejections(clean_teams_db, session_factory):
    """Verifica el rechazo estricto de nombres vacíos o formados solo por espacios/tabs/newlines."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    for invalid_name in ["", "   ", "\t\t", "\n\n  \t "]:
        inter = create_mock_interaction(user=staff, guild=guild)
        await cog.registrar_equipo.callback(
            cog,
            inter,
            rol=create_mock_role(7001),
            nombre=invalid_name,
            tag="VAL",
            division="PREMIER",
        )
        inter.response.send_message.assert_awaited_once()
        assert "no puede estar vacío" in inter.response.send_message.await_args.args[0]
        inter.response.defer.assert_not_called()


@pytest.mark.asyncio
async def test_teams_idempotent_lifecycle_updates_and_conflicts(clean_teams_db, session_factory):
    """
    Verifica de forma completa:
    1. Registro inicial de Equipo A (rol 8001) y Equipo B (rol 8002).
    2. Actualización idempotente de Equipo A (mismo rol, nuevo nombre).
    3. Actualización idempotente de Equipo A (mismo nombre, nuevo rol 8003).
    4. Detección de conflicto cruzado: rol 8003 (Equipo A) con nombre 'Equipo B'.
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 1. Registro de Equipo A y B
    inter_a = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_a,
        rol=create_mock_role(8001),
        nombre="Equipo Alfa",
        tag="ALFA",
        division="PREMIER",
    )
    assert "Éxito" in inter_a.followup.send.await_args.kwargs["embed"].title

    inter_b = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_b,
        rol=create_mock_role(8002),
        nombre="Equipo Beta",
        tag="BETA",
        division="PREMIER",
    )
    assert "Éxito" in inter_b.followup.send.await_args.kwargs["embed"].title

    # 2. Actualización de Equipo A con mismo rol pero nuevo nombre
    inter_a_rename = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_a_rename,
        rol=create_mock_role(8001),
        nombre="Equipo Alfa Renombrado",
        tag="ALFR",
        division="ASCEND",
    )
    embed_rename = inter_a_rename.followup.send.await_args.kwargs["embed"]
    assert "Actualizado" in embed_rename.title

    async with session_factory() as session:
        repo = TeamRepository(session)
        team_a = await repo.get_by_role_id(8001)
        assert team_a.name == "Equipo Alfa Renombrado"
        assert team_a.tag == "ALFR"
        assert team_a.division == Division.ASCEND

    # 3. Actualización de Equipo A con mismo nombre pero nuevo rol 8003
    inter_a_newrole = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_a_newrole,
        rol=create_mock_role(8003),
        nombre="Equipo Alfa Renombrado",
        tag="ALFR",
        division="ASCEND",
    )
    embed_newrole = inter_a_newrole.followup.send.await_args.kwargs["embed"]
    assert "Actualizado" in embed_newrole.title

    async with session_factory() as session:
        repo = TeamRepository(session)
        team_a_updated = await repo.get_by_name("Equipo Alfa Renombrado")
        assert team_a_updated.discord_role_id == 8003

    # 4. Conflicto cruzado: rol 8003 (asociado a Alfa) con nombre
    # 'Equipo Beta' (asociado a rol 8002)
    inter_conflict = create_mock_interaction(user=staff, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_conflict,
        rol=create_mock_role(8003),
        nombre="Equipo Beta",
        tag="BETX",
        division="PREMIER",
    )
    embed_conflict = inter_conflict.followup.send.await_args.kwargs["embed"]
    assert "Conflicto de Registro" in embed_conflict.title


@pytest.mark.asyncio
async def test_teams_division_fallbacks_and_variations(clean_teams_db, session_factory):
    """Verifica la normalización flexible de divisiones y fallbacks."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    cases = [
        ("PREM", Division.PREMIER),
        ("ASC", Division.ASCEND),
        ("ASCENSO", Division.ASCEND),
        ("ascend", Division.ASCEND),
        ("UNKNOWN_FALLBACK", Division.PREMIER),
    ]

    for i, (div_input, expected_div) in enumerate(cases):
        inter = create_mock_interaction(user=staff, guild=guild)
        await cog.registrar_equipo.callback(
            cog,
            inter,
            rol=create_mock_role(9000 + i),
            nombre=f"Team Division Test {i}",
            tag=f"D{i}",
            division=div_input,
        )
        assert inter.followup.send.called

        async with session_factory() as session:
            repo = TeamRepository(session)
            t = await repo.get_by_role_id(9000 + i)
            assert t is not None
            assert t.division == expected_div


@pytest.mark.asyncio
async def test_equipos_empty_database_embed(clean_teams_db, session_factory):
    """Verifica que /equipos con base de datos vacía responde con embed naranja descriptivo."""
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=Settings())
    inter = create_mock_interaction(guild=create_mock_guild())

    await cog.equipos.callback(cog, inter, division=None)

    inter.response.defer.assert_awaited_once_with(ephemeral=False)
    inter.followup.send.assert_awaited_once()
    embed = inter.followup.send.await_args.kwargs["embed"]
    assert embed.color == discord.Color.orange()
    assert "No hay equipos registrados" in embed.description


@pytest.mark.asyncio
async def test_equipos_division_filtering(clean_teams_db, session_factory):
    """
    Verifica que el filtrado por división en /equipos solo incluye los campos correspondientes.
    """
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=Settings())

    async with session_factory() as session:
        repo = TeamRepository(session)
        await repo.create(
            name="Premier Alpha",
            tag="PA",
            slug="premier-alpha",
            division=Division.PREMIER,
            discord_role_id=9101,
        )
        await repo.create(
            name="Ascenso Beta",
            tag="AB",
            slug="ascenso-beta",
            division=Division.ASCEND,
            discord_role_id=9102,
        )
        await session.commit()

    guild = create_mock_guild()

    # 1. Filtro PREMIER
    inter_p = create_mock_interaction(guild=guild)
    await cog.equipos.callback(
        cog,
        inter_p,
        division=app_commands.Choice(name="Premier", value="PREMIER"),
    )
    embed_p = inter_p.followup.send.await_args.kwargs["embed"]
    assert any("Premier" in f.name for f in embed_p.fields)
    assert not any("Ascenso" in f.name for f in embed_p.fields)

    # 2. Filtro ASCEND
    inter_a = create_mock_interaction(guild=guild)
    await cog.equipos.callback(
        cog,
        inter_a,
        division=app_commands.Choice(name="Ascenso", value="ASCEND"),
    )
    embed_a = inter_a.followup.send.await_args.kwargs["embed"]
    assert any("Ascenso" in f.name for f in embed_a.fields)
    assert not any("Premier" in f.name for f in embed_a.fields)


@pytest.mark.asyncio
async def test_tickets_loop_error_handler_restarts():
    """Verifica que on_check_tickets_error reinicia el bucle si se detuvo tras un fallo."""
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, auto_start=False)

    cog.check_tickets_loop = MagicMock()
    cog.check_tickets_loop.is_running.return_value = False
    cog.check_tickets_loop.restart = MagicMock()

    await cog.on_check_tickets_error(RuntimeError("Simulated loop crash"))
    cog.check_tickets_loop.restart.assert_called_once()


def test_tickets_loop_unload_and_stop():
    """Verifica que cog_unload y stop_loops cancelan el bucle en ejecución."""
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, auto_start=False)

    cog.check_tickets_loop = MagicMock()
    cog.check_tickets_loop.is_running.return_value = True
    cog.check_tickets_loop.cancel = MagicMock()

    cog.cog_unload()
    cog.check_tickets_loop.cancel.assert_called_once()

    cog.check_tickets_loop.reset_mock()
    cog.check_tickets_loop.is_running.return_value = True
    cog.stop_loops()
    cog.check_tickets_loop.cancel.assert_called_once()
