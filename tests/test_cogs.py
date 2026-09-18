"""
Suite completa de pruebas unitarias para los Cogs de LigaBot.

Cubre:
- TeamsCog: /registrar-equipo (idempotencia, normalización) y /equipos (filtros).
- ScheduleCog: /crear-partido e /importar-jornada (/crear-jornada) con gestión de errores.
- AdminCog: /sync y /sincronizar (local, global, validación de IDs y errores API).
- TicketsCog: tarea periódica @tasks.loop(hours=24) y slash command /revisar-tickets.
- Permissions: resolución resiliente de discord.Member y chequeos de roles.
- Carga idempotente de extensiones (setup).
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.cogs.admin import AdminCog
from liga_bot.cogs.admin import setup as admin_setup
from liga_bot.cogs.permissions import (
    is_admin,
    is_authorized_scheduler,
    is_ceo_ascend,
    is_ceo_premier,
    is_staff,
    is_staff_admin_or_ceo,
    is_staff_or_admin,
    resolve_member,
)
from liga_bot.cogs.schedule import ScheduleCog
from liga_bot.cogs.schedule import setup as schedule_setup
from liga_bot.cogs.teams import TeamsCog
from liga_bot.cogs.teams import setup as teams_setup
from liga_bot.cogs.tickets import TicketsCog
from liga_bot.cogs.tickets import setup as tickets_setup
from liga_bot.config import Settings
from liga_bot.database import get_session_factory
from liga_bot.models.enums import Division
from liga_bot.models.team import Team
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.services.schedule_service import JornadaResult, MatchResult, ScheduleService
from liga_bot.services.ticket_service import (
    ChannelAuditDetail,
    ChannelAuditStatus,
    TicketAuditResult,
    TicketService,
)

# ---------------------------------------------------------------------------
# Fixtures y Factories de Mocks de Discord
# ---------------------------------------------------------------------------


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
    member.roles = list(roles or [])
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = is_admin
    return member


def create_mock_user(user_id: int, name: str = "UsuarioExterno") -> MagicMock:
    user = MagicMock(spec=discord.User)
    user.id = user_id
    user.name = name
    user.display_name = name
    user.mention = f"<@{user_id}>"
    return user


def create_mock_guild(
    guild_id: int = 1547725310508667010,
    name: str = "Liga Discord",
) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    return guild


def create_mock_interaction(
    user: discord.Member | discord.User | None = None,
    guild: discord.Guild | None = None,
) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.guild = guild
    inter.user = user
    inter.response = MagicMock(spec=discord.InteractionResponse)
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock(spec=discord.Webhook)
    inter.followup.send = AsyncMock()
    inter.client = MagicMock()
    return inter


def create_mock_attachment(filename: str, content: bytes) -> MagicMock:
    att = MagicMock(spec=discord.Attachment)
    att.id = 998877
    att.filename = filename
    att.size = len(content)
    att.read = AsyncMock(return_value=content)
    return att


# ---------------------------------------------------------------------------
# Database Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factoría de sesiones conectada a la base de datos de pruebas."""
    return get_session_factory(migrated_db)


@pytest_asyncio.fixture
async def clean_teams(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None, None]:
    """Limpia la tabla de equipos antes y después de cada prueba."""
    from sqlalchemy import delete

    async with session_factory() as session:
        await session.execute(delete(Team))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(delete(Team))
        await session.commit()


# ===========================================================================
# 1. PRUEBAS DE PERMISOS (permissions.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_resolve_member_already_member():
    """Verifica que resolve_member devuelve el objeto si ya es discord.Member."""
    member = create_mock_member(12345)
    inter = create_mock_interaction(user=member)

    resolved = await resolve_member(inter)
    assert resolved is member


@pytest.mark.asyncio
async def test_resolve_member_cached_in_guild():
    """Verifica que resolve_member busca en la caché del servidor si es discord.User."""
    user = create_mock_user(12345)
    guild = create_mock_guild()
    cached_member = create_mock_member(12345)
    guild.get_member.return_value = cached_member

    inter = create_mock_interaction(user=user, guild=guild)
    resolved = await resolve_member(inter)

    assert resolved is cached_member
    guild.get_member.assert_called_once_with(12345)
    guild.fetch_member.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_member_fetched_from_api():
    """Verifica que resolve_member realiza fetch_member si no está en caché."""
    user = create_mock_user(12345)
    guild = create_mock_guild()
    guild.get_member.return_value = None
    fetched_member = create_mock_member(12345)
    guild.fetch_member.return_value = fetched_member

    inter = create_mock_interaction(user=user, guild=guild)
    resolved = await resolve_member(inter)

    assert resolved is fetched_member
    guild.fetch_member.assert_awaited_once_with(12345)


@pytest.mark.asyncio
async def test_resolve_member_dm_returns_none():
    """Verifica que resolve_member retorna None en interacciones de DM."""
    user = create_mock_user(12345)
    inter = create_mock_interaction(user=user, guild=None)

    resolved = await resolve_member(inter)
    assert resolved is None


@pytest.mark.asyncio
async def test_permission_checks_matrix():
    """Verifica la matriz de permisos para roles staff, admin, CEO y administrador nativo."""
    settings = Settings(
        staff_role_id=101,
        admin_role_id=102,
        ceo_premier_role_id=103,
        ceo_ascend_role_id=104,
    )

    # 1. Usuario sin roles
    member_none = create_mock_member(1, roles=[])
    inter = create_mock_interaction(user=member_none)
    assert not await is_staff(inter, settings)
    assert not await is_admin(inter, settings)
    assert not await is_staff_or_admin(inter, settings)
    assert not await is_ceo_premier(inter, settings)
    assert not await is_ceo_ascend(inter, settings)
    assert not await is_authorized_scheduler(inter, settings)
    assert not await is_staff_admin_or_ceo(inter, settings)

    # 2. Staff
    member_staff = create_mock_member(2, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=member_staff)
    assert await is_staff(inter, settings)
    assert not await is_admin(inter, settings)
    assert await is_staff_or_admin(inter, settings)
    assert await is_authorized_scheduler(inter, settings)

    # 3. Admin
    member_admin = create_mock_member(3, roles=[create_mock_role(102)])
    inter = create_mock_interaction(user=member_admin)
    assert not await is_staff(inter, settings)
    assert await is_admin(inter, settings)
    assert await is_staff_or_admin(inter, settings)
    assert await is_authorized_scheduler(inter, settings)

    # 4. CEO Premier
    member_ceo_p = create_mock_member(4, roles=[create_mock_role(103)])
    inter = create_mock_interaction(user=member_ceo_p)
    assert await is_ceo_premier(inter, settings)
    assert not await is_staff_or_admin(inter, settings)
    assert await is_authorized_scheduler(inter, settings)

    # 5. CEO Ascend
    member_ceo_a = create_mock_member(5, roles=[create_mock_role(104)])
    inter = create_mock_interaction(user=member_ceo_a)
    assert await is_ceo_ascend(inter, settings)
    assert not await is_staff_or_admin(inter, settings)
    assert await is_authorized_scheduler(inter, settings)

    # 6. Administrador nativo de Discord (sin roles específicos)
    member_nat_admin = create_mock_member(6, roles=[], is_admin=True)
    inter = create_mock_interaction(user=member_nat_admin)
    assert await is_staff(inter, settings)
    assert await is_admin(inter, settings)
    assert await is_staff_or_admin(inter, settings)
    assert await is_ceo_premier(inter, settings)
    assert await is_ceo_ascend(inter, settings)
    assert await is_authorized_scheduler(inter, settings)


# ===========================================================================
# 2. PRUEBAS DE TEAMS COG (teams.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_registrar_equipo_outside_guild(session_factory):
    """Verifica que /registrar-equipo rechaza ejecuciones fuera de un servidor."""
    cog = TeamsCog(MagicMock(), session_factory=session_factory)
    inter = create_mock_interaction(guild=None)

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(999),
        nombre="Pingus",
        tag="PSP",
        division="PREMIER",
    )

    inter.response.send_message.assert_awaited_once()
    args = inter.response.send_message.await_args
    assert "servidor de Discord" in args.args[0]


@pytest.mark.asyncio
async def test_registrar_equipo_unauthorized(session_factory):
    """Verifica que /registrar-equipo rechaza a usuarios sin permisos de Staff o Admin."""
    settings = Settings(staff_role_id=101, admin_role_id=102)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    unauth_member = create_mock_member(12345, roles=[create_mock_role(999)])
    guild = create_mock_guild()
    inter = create_mock_interaction(user=unauth_member, guild=guild)

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(999),
        nombre="Pingus",
        tag="PSP",
        division="PREMIER",
    )

    inter.response.send_message.assert_awaited_once()
    assert "No tienes permisos" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_registrar_equipo_empty_name(session_factory):
    """Verifica que /registrar-equipo rechaza nombres vacíos o solo espacios."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(999),
        nombre="   ",
        tag="PSP",
        division="PREMIER",
    )

    inter.response.send_message.assert_awaited_once()
    assert "no puede estar vacío" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_registrar_equipo_name_too_long(session_factory):
    """Verifica que /registrar-equipo rechaza nombres mayores a 100 caracteres."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(999),
        nombre="A" * 101,
        tag="PSP",
        division="PREMIER",
    )

    inter.response.send_message.assert_awaited_once()
    assert "no puede exceder los 100 caracteres" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_registrar_equipo_invalid_tag(session_factory):
    """Verifica que /registrar-equipo rechaza tags vacíos o mayores a 4 caracteres."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())

    # Tag muy largo (> 4)
    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(999),
        nombre="Planar Shock",
        tag="TOOLONG",
        division="PREMIER",
    )

    inter.response.send_message.assert_awaited_once()
    assert "entre 1 y 4 caracteres" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_registrar_equipo_success_create(clean_teams, session_factory):
    """Verifica que /registrar-equipo crea un equipo correctamente en la base de datos."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()
    inter = create_mock_interaction(user=staff_member, guild=guild)
    team_role = create_mock_role(55555, "Team Role")

    choice_division = app_commands.Choice(name="Premier", value="PREMIER")

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=team_role,
        nombre="Planar Shock Pingus",
        tag="psp",
        division=choice_division,
    )

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.followup.send.assert_awaited_once()

    embed = inter.followup.send.await_args.kwargs["embed"]
    assert "Registrado" in embed.title or "Éxito" in embed.title

    # Comprobar persistencia real en la base de datos
    async with session_factory() as session:
        repo = TeamRepository(session)
        team = await repo.get_by_role_id(55555)
        assert team is not None
        assert team.name == "Planar Shock Pingus"
        assert team.tag == "PSP"
        assert team.division == Division.PREMIER
        assert team.slug == "planar-shock-pingus"


@pytest.mark.asyncio
async def test_registrar_equipo_success_update(clean_teams, session_factory):
    """Verifica que /registrar-equipo actualiza un equipo existente con el mismo rol."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    # 1. Crear equipo inicial
    async with session_factory() as session:
        repo = TeamRepository(session)
        await repo.create(
            name="Nombre Antiguo",
            tag="OLD",
            slug="nombre-antiguo",
            division=Division.ASCEND,
            discord_role_id=55555,
        )
        await session.commit()

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())
    team_role = create_mock_role(55555, "Team Role")

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=team_role,
        nombre="Nombre Nuevo",
        tag="NEW",
        division="PREMIER",
    )

    inter.followup.send.assert_awaited_once()

    async with session_factory() as session:
        repo = TeamRepository(session)
        team = await repo.get_by_role_id(55555)
        assert team is not None
        assert team.name == "Nombre Nuevo"
        assert team.tag == "NEW"
        assert team.division == Division.PREMIER


@pytest.mark.asyncio
async def test_registrar_equipo_conflict_detected(clean_teams, session_factory):
    """Verifica que se detecta conflicto si el rol pertenece a un equipo y el nombre a otro."""
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)

    async with session_factory() as session:
        repo = TeamRepository(session)
        await repo.create(
            name="Equipo Alfa",
            tag="ALF",
            slug="equipo-alfa",
            division=Division.PREMIER,
            discord_role_id=11111,
        )
        await repo.create(
            name="Equipo Beta",
            tag="BET",
            slug="equipo-beta",
            division=Division.PREMIER,
            discord_role_id=22222,
        )
        await session.commit()

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())
    # Intenta asignar el rol de Alfa al nombre de Beta
    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(11111),
        nombre="Equipo Beta",
        tag="BET",
        division="PREMIER",
    )

    inter.followup.send.assert_awaited_once()
    embed = inter.followup.send.await_args.kwargs["embed"]
    assert "Conflicto" in embed.title


@pytest.mark.asyncio
async def test_registrar_equipo_db_error_handling(session_factory):
    """
    Verifica que /registrar-equipo envía un embed de error si ocurre un fallo
    relacional inesperado tras defer().
    """
    settings = Settings(staff_role_id=101)
    mock_factory = MagicMock(side_effect=RuntimeError("Database explosion"))
    cog = TeamsCog(MagicMock(), session_factory=mock_factory, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())

    await cog.registrar_equipo.callback(
        cog,
        inter,
        rol=create_mock_role(999),
        nombre="Valid Team",
        tag="VT",
        division="PREMIER",
    )

    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once()
    kwargs = inter.followup.send.await_args.kwargs
    assert "embed" in kwargs
    assert "Error al Registrar Equipo" in kwargs["embed"].title


@pytest.mark.asyncio
async def test_equipos_outside_guild(session_factory):
    """Verifica que /equipos rechaza peticiones en DMs."""
    cog = TeamsCog(MagicMock(), session_factory=session_factory)
    inter = create_mock_interaction(guild=None)

    await cog.equipos.callback(cog, inter)
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_equipos_empty_listing(clean_teams, session_factory):
    """Verifica que /equipos responde con aviso cuando la base de datos está vacía."""
    cog = TeamsCog(MagicMock(), session_factory=session_factory)
    inter = create_mock_interaction(guild=create_mock_guild())

    await cog.equipos.callback(cog, inter)

    inter.response.defer.assert_awaited_once_with(ephemeral=False)
    inter.followup.send.assert_awaited_once()
    embed = inter.followup.send.await_args.kwargs["embed"]
    assert "No hay equipos registrados" in embed.description


@pytest.mark.asyncio
async def test_equipos_listing_all_and_filtered(clean_teams, session_factory):
    """Verifica el listado completo de equipos y el filtrado por división."""
    cog = TeamsCog(MagicMock(), session_factory=session_factory)

    async with session_factory() as session:
        repo = TeamRepository(session)
        await repo.create(
            name="Premier Team",
            tag="PRM",
            slug="premier-team",
            division=Division.PREMIER,
            discord_role_id=1001,
        )
        await repo.create(
            name="Ascend Team",
            tag="ASC",
            slug="ascend-team",
            division=Division.ASCEND,
            discord_role_id=1002,
        )
        await session.commit()

    # 1. Todas las divisiones
    inter_all = create_mock_interaction(guild=create_mock_guild())
    await cog.equipos.callback(cog, inter_all, division=None)
    embed_all = inter_all.followup.send.await_args.kwargs["embed"]
    field_names = [f.name for f in embed_all.fields]
    assert any("Premier" in f for f in field_names)
    assert any("Ascenso" in f for f in field_names)

    # 2. Filtro Premier
    inter_prm = create_mock_interaction(guild=create_mock_guild())
    choice_prm = app_commands.Choice(name="Premier", value="PREMIER")
    await cog.equipos.callback(cog, inter_prm, division=choice_prm)
    embed_prm = inter_prm.followup.send.await_args.kwargs["embed"]
    field_names_prm = [f.name for f in embed_prm.fields]
    assert any("Premier" in f for f in field_names_prm)
    assert not any("Ascenso" in f for f in field_names_prm)


@pytest.mark.asyncio
async def test_teams_setup_idempotent():
    """Verifica que setup() de Teams registra el Cog sólo si no estaba añadido."""
    bot = MagicMock(spec=commands.Bot)
    bot.cogs = {}
    bot.add_cog = AsyncMock()

    await teams_setup(bot)
    bot.add_cog.assert_awaited_once()

    bot.cogs = {"Teams": MagicMock()}
    bot.add_cog.reset_mock()
    await teams_setup(bot)
    bot.add_cog.assert_not_awaited()


# ===========================================================================
# 3. PRUEBAS DE SCHEDULE COG (schedule.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_crear_partido_outside_guild():
    """Verifica que /crear-partido rechaza DMs."""
    cog = ScheduleCog(MagicMock(), schedule_service=MagicMock())
    inter = create_mock_interaction(guild=None)

    await cog.crear_partido.callback(cog, inter, jornada=1, equipo1="A", equipo2="B")
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_crear_partido_unauthorized():
    """Verifica que /crear-partido rechaza usuarios no autorizados."""
    settings = Settings(staff_role_id=101)
    cog = ScheduleCog(MagicMock(), schedule_service=MagicMock(), settings=settings)

    unauth = create_mock_member(12345, roles=[create_mock_role(999)])
    inter = create_mock_interaction(user=unauth, guild=create_mock_guild())

    await cog.crear_partido.callback(cog, inter, jornada=1, equipo1="A", equipo2="B")
    inter.response.send_message.assert_awaited_once()
    assert "No tienes permisos" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_crear_partido_invalid_jornada():
    """Verifica que /crear-partido valida que la jornada sea >= 1."""
    settings = Settings(staff_role_id=101)
    cog = ScheduleCog(MagicMock(), schedule_service=MagicMock(), settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())

    await cog.crear_partido.callback(cog, inter, jornada=0, equipo1="A", equipo2="B")
    inter.response.send_message.assert_awaited_once()
    assert "entero positivo" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_crear_partido_success_and_duplicate():
    """Verifica la respuesta de éxito y de duplicado en /crear-partido."""
    settings = Settings(staff_role_id=101)
    mock_service = MagicMock(spec=ScheduleService)
    cog = ScheduleCog(MagicMock(), schedule_service=mock_service, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 1. Éxito
    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.mention = "<#88888>"
    mock_channel.jump_url = "https://discord.com/channels/1/88888"

    mock_service.create_match = AsyncMock(
        return_value=MatchResult(
            success=True,
            jornada=1,
            team1_name="Equipo 1",
            team2_name="Equipo 2",
            channel=mock_channel,
            channel_mention=mock_channel.mention,
        )
    )

    inter_success = create_mock_interaction(user=staff_member, guild=guild)
    await cog.crear_partido.callback(
        cog,
        inter_success,
        jornada=1,
        equipo1="Equipo 1",
        equipo2="Equipo 2",
        fecha="18/09/2026",
        hora="21:00",
    )

    inter_success.response.defer.assert_awaited_once_with(ephemeral=True)
    inter_success.followup.send.assert_awaited_once()
    embed_succ = inter_success.followup.send.await_args.kwargs["embed"]
    assert "✅" in embed_succ.title

    # 2. Duplicado
    mock_service.create_match = AsyncMock(
        return_value=MatchResult(
            success=False,
            is_duplicate=True,
            jornada=1,
            team1_name="Equipo 1",
            team2_name="Equipo 2",
            error="El enfrentamiento ya existe",
        )
    )

    inter_dup = create_mock_interaction(user=staff_member, guild=guild)
    await cog.crear_partido.callback(
        cog,
        inter_dup,
        jornada=1,
        equipo1="Equipo 1",
        equipo2="Equipo 2",
    )

    embed_dup = inter_dup.followup.send.await_args.kwargs["embed"]
    assert "Ya Existente" in embed_dup.title


@pytest.mark.asyncio
async def test_importar_jornada_validations_and_aliases():
    """Verifica validaciones de archivo CSV y que el alias /crear-jornada funciona."""
    settings = Settings(staff_role_id=101)
    mock_service = MagicMock(spec=ScheduleService)
    cog = ScheduleCog(MagicMock(), schedule_service=mock_service, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 1. Archivo no CSV
    inter_not_csv = create_mock_interaction(user=staff_member, guild=guild)
    txt_att = create_mock_attachment("partidos.txt", b"dummy")
    await cog.importar_jornada.callback(cog, inter_not_csv, jornada=1, archivo=txt_att)
    assert "debe ser de tipo CSV" in inter_not_csv.response.send_message.await_args.args[0]

    # 2. Éxito con CSV y alias crear_jornada
    csv_bytes = "equipo1,equipo2,fecha,hora\nPSP,FNX,18/09/2026,21:00".encode("utf-8-sig")
    csv_att = create_mock_attachment("jornada1.csv", csv_bytes)

    mock_service.create_jornada_from_csv = AsyncMock(
        return_value=JornadaResult(
            jornada=1,
            total_rows=1,
            matches=[
                MatchResult(
                    success=True,
                    jornada=1,
                    team1_name="PSP",
                    team2_name="FNX",
                    channel_mention="<#112233>",
                )
            ],
            errors=[],
        )
    )

    inter_alias = create_mock_interaction(user=staff_member, guild=guild)
    await cog.crear_jornada.callback(cog, inter_alias, jornada=1, archivo=csv_att)

    inter_alias.response.defer.assert_awaited_once_with(ephemeral=True)
    inter_alias.followup.send.assert_awaited_once()
    embed = inter_alias.followup.send.await_args.kwargs["embed"]
    assert "Éxito" in embed.title


@pytest.mark.asyncio
async def test_schedule_setup_idempotent():
    """Verifica idempotencia de setup() en ScheduleCog."""
    bot = MagicMock(spec=commands.Bot)
    bot.cogs = {}
    bot.add_cog = AsyncMock()

    await schedule_setup(bot)
    bot.add_cog.assert_awaited_once()

    bot.cogs = {"Schedule": MagicMock()}
    bot.add_cog.reset_mock()
    await schedule_setup(bot)
    bot.add_cog.assert_not_awaited()


# ===========================================================================
# 4. PRUEBAS DE ADMIN COG (admin.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_unauthorized():
    """Verifica que /sync rechaza a usuarios que no son Staff ni Admin."""
    settings = Settings(staff_role_id=101, admin_role_id=102)
    cog = AdminCog(MagicMock(), settings=settings)

    unauth = create_mock_member(12345, roles=[create_mock_role(999)])
    inter = create_mock_interaction(user=unauth, guild=create_mock_guild())

    await cog.sync.callback(cog, inter)
    inter.response.send_message.assert_awaited_once()
    assert "No tienes permisos" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_sync_guild_and_global_success():
    """Verifica sincronización local (guild) y global con reporte de comandos."""
    settings = Settings(staff_role_id=101, admin_role_id=102)
    mock_bot = MagicMock(spec=commands.Bot)
    mock_cmd1 = MagicMock()
    mock_cmd1.name = "crear-partido"
    mock_cmd2 = MagicMock()
    mock_cmd2.name = "registrar-equipo"
    mock_bot.tree.sync = AsyncMock(return_value=[mock_cmd1, mock_cmd2])

    cog = AdminCog(mock_bot, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild(777)

    # 1. Sync Guild actual
    inter_guild = create_mock_interaction(user=staff_member, guild=guild)
    await cog.sync.callback(cog, inter_guild, guild_id=None, global_sync=False)

    inter_guild.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_bot.tree.sync.assert_awaited_once_with(guild=guild)
    embed = inter_guild.followup.send.await_args.kwargs["embed"]
    assert "Sincronizado" in embed.title
    assert "2" in embed.description

    # 2. Sync Global
    mock_bot.tree.sync.reset_mock()
    inter_global = create_mock_interaction(user=staff_member, guild=guild)
    await cog.sincronizar.callback(cog, inter_global, guild_id=None, global_sync=True)

    mock_bot.tree.sync.assert_awaited_once_with(guild=None)
    embed_global = inter_global.followup.send.await_args.kwargs["embed"]
    assert "Global" in embed_global.fields[0].value


@pytest.mark.asyncio
async def test_sync_invalid_guild_id():
    """Verifica que /sync valida IDs de servidor no numéricos."""
    settings = Settings(staff_role_id=101)
    cog = AdminCog(MagicMock(), settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())

    await cog.sync.callback(cog, inter, guild_id="no-es-un-numero")
    inter.followup.send.assert_awaited_once()
    assert "número entero válido" in inter.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_sync_api_exceptions_handled():
    """Verifica que AdminCog maneja excepciones de permisos y HTTP de Discord."""
    settings = Settings(staff_role_id=101)
    mock_bot = MagicMock(spec=commands.Bot)
    mock_bot.tree.sync = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(), "Missing Permissions")
    )
    cog = AdminCog(mock_bot, settings=settings)

    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    inter = create_mock_interaction(user=staff_member, guild=create_mock_guild())

    await cog.sync.callback(cog, inter)
    inter.followup.send.assert_awaited_once()
    embed = inter.followup.send.await_args.kwargs["embed"]
    assert "Error de Permisos" in embed.title


@pytest.mark.asyncio
async def test_admin_setup_idempotent():
    """Verifica idempotencia de setup() en AdminCog."""
    bot = MagicMock(spec=commands.Bot)
    bot.cogs = {}
    bot.add_cog = AsyncMock()

    await admin_setup(bot)
    bot.add_cog.assert_awaited_once()

    bot.cogs = {"Admin": MagicMock()}
    bot.add_cog.reset_mock()
    await admin_setup(bot)
    bot.add_cog.assert_not_awaited()


# ===========================================================================
# 5. PRUEBAS DE TICKETS COG (tickets.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_revisar_tickets_outside_guild():
    """Verifica que /revisar-tickets rechaza ejecuciones en DM."""
    cog = TicketsCog(MagicMock(), ticket_service=MagicMock(), auto_start=False)
    inter = create_mock_interaction(guild=None)

    await cog.revisar_tickets.callback(cog, inter)
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_revisar_tickets_unauthorized():
    """Verifica que /revisar-tickets rechaza a usuarios sin roles autorizados."""
    mock_bot = MagicMock(spec=commands.Bot)
    mock_bot.settings = Settings(staff_role_id=101)
    cog = TicketsCog(mock_bot, ticket_service=MagicMock(), auto_start=False)

    unauth = create_mock_member(12345, roles=[create_mock_role(999)])
    inter = create_mock_interaction(user=unauth, guild=create_mock_guild())

    await cog.revisar_tickets.callback(cog, inter)
    inter.response.send_message.assert_awaited_once_with(
        "No tienes permiso para usar este comando.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_revisar_tickets_success_and_service_error():
    """Verifica el flujo exitoso con reporte de tickets y captura de errores."""
    mock_bot = MagicMock(spec=commands.Bot)
    mock_bot.settings = Settings(staff_role_id=101)
    mock_service = MagicMock(spec=TicketService)

    cog = TicketsCog(mock_bot, ticket_service=mock_service, auto_start=False)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 1. Éxito
    audit_result = TicketAuditResult(
        categories_scanned=2,
        channels_scanned=5,
        alerts_sent=1,
        skipped_recent=3,
        skipped_staff=1,
        details=[
            ChannelAuditDetail(
                channel_id=9911,
                channel_name="ticket-001",
                category_name="TICKETS",
                status=ChannelAuditStatus.ALERT_SENT,
            )
        ],
    )
    mock_service.check_tickets = AsyncMock(return_value=audit_result)

    inter = create_mock_interaction(user=staff_member, guild=guild)
    await cog.revisar_tickets.callback(cog, inter)

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.followup.send.assert_awaited_once()
    embed = inter.followup.send.await_args.kwargs["embed"]
    assert "Auditoría de Inactividad" in embed.title
    assert "ticket-001" in embed.fields[2].value

    # 2. Error en servicio
    mock_service.check_tickets = AsyncMock(side_effect=RuntimeError("Discord Gateway Timeout"))
    inter_err = create_mock_interaction(user=staff_member, guild=guild)
    await cog.revisar_tickets.callback(cog, inter_err)
    assert "error inesperado" in inter_err.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_revisar_tickets_build_audit_embed_truncation():
    """
    Verifica que _build_audit_embed trunca defensivamente a <= 1024 chars
    cuando los nombres son largos.
    """
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, ticket_service=MagicMock(), auto_start=False)

    details = [
        ChannelAuditDetail(
            channel_id=1000 + i,
            channel_name=f"ticket-soporte-muy-largo-con-mucho-texto-descriptivo-staff-{i:02d}",
            category_name="TICKETS",
            status=ChannelAuditStatus.ALERT_SENT,
        )
        for i in range(20)
    ]
    result = TicketAuditResult(
        categories_scanned=1,
        channels_scanned=20,
        alerts_sent=20,
        details=details,
    )
    user = create_mock_member(12345, name="StaffUser")
    embed = cog._build_audit_embed(result, user)

    alert_field = next((f for f in embed.fields if f.name == "🚨 Tickets Notificados"), None)
    assert alert_field is not None
    assert len(alert_field.value) <= 1024
    assert "(truncado" in alert_field.value


@pytest.mark.asyncio
async def test_tickets_loop_lifecycle():
    """Verifica el ciclo de vida del bucle periódico de TicketsCog."""
    mock_bot = MagicMock(spec=commands.Bot)
    mock_bot.wait_until_ready = AsyncMock()
    mock_bot.settings = Settings(guild_id=1547725310508667010)
    mock_guild = create_mock_guild(1547725310508667010)
    mock_bot.get_guild.return_value = mock_guild

    mock_service = MagicMock(spec=TicketService)
    mock_service.check_tickets = AsyncMock(
        return_value=TicketAuditResult(channels_scanned=10, alerts_sent=0)
    )

    cog = TicketsCog(mock_bot, ticket_service=mock_service, auto_start=False)

    # 1. before_loop espera a que el bot esté listo
    await cog.before_check_tickets_loop()
    mock_bot.wait_until_ready.assert_awaited_once()

    # 2. Ejecución de una iteración del bucle
    await cog.check_tickets_loop.coro(cog)
    mock_service.check_tickets.assert_awaited_once_with(mock_guild)

    # 3. Resiliencia: si check_tickets falla, no se eleva excepción fatal
    mock_service.check_tickets.side_effect = Exception("DB Connection Timeout")
    await cog.check_tickets_loop.coro(cog)  # No debe lanzar excepción

    # 4. Parada limpia del loop
    cog.stop_loops()
    assert not cog.check_tickets_loop.is_running()


@pytest.mark.asyncio
async def test_tickets_setup_idempotent():
    """Verifica idempotencia de setup() en TicketsCog."""
    bot = MagicMock(spec=commands.Bot)
    bot.cogs = {}
    bot.add_cog = AsyncMock()

    await tickets_setup(bot)
    bot.add_cog.assert_awaited_once()

    bot.cogs = {"Tickets": MagicMock()}
    bot.add_cog.reset_mock()
    await tickets_setup(bot)
    bot.add_cog.assert_not_awaited()
