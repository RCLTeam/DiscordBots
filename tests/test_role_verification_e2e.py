"""
Suite de pruebas End-to-End (E2E) y de aceptación para el subsistema de roles:
1. Verificación de purga completa de ficheros monolíticos obsoletos (liga_bot.py, solicitud_rol.py).
2. Ciclo de vida completo 1: On-boarding -> auto-asignación 'Sin Verificar' -> modal de equipo
   -> creación de canal de ticket privado con PermissionOverwrites -> registro PENDING
   -> confirmación por staff -> asignación de rol, remoción de 'Sin Verificar', apodo
   '{lol} #{tag}' y actualización a APPROVED en PostgreSQL (PGlite).
3. Ciclo de vida completo 2: Solicitud de rol 'Libre' -> asignación directa de 'Libre', remoción de
   'Sin Verificar', actualización de apodo y registro APPROVED en BD sin ticket.
4. Ciclo de vida completo 3: Ticket creado -> denegación por staff -> actualización
   a DENIED en base de datos y programación de borrado de canal.
5. Prevención de duplicados: Solicitud PENDING activa impide abrir segundo ticket.
6. Serialización de DynamicItem: ConfirmarRolButton custom_id regex matching, parsing
   y reconstrucción ante reinicios simulados del bot.
7. Seguridad administrativa: default_permissions(manage_guild=True) en ScheduleCog y TicketsCog,
   y rechazo de invocaciones no autorizadas por usuarios no-staff.
8. Condiciones límite y resiliencia: Truncado de apodos a 32 caracteres y degradación elegante.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.bot import DEFAULT_EXTENSIONS, LigaBot
from liga_bot.cogs.roles import RolesCog
from liga_bot.cogs.schedule import ScheduleCog
from liga_bot.cogs.tickets import TicketsCog
from liga_bot.config import TEAMS_ALL, Settings
from liga_bot.models.enums import RoleRequestStatus
from liga_bot.models.role_request import RoleRequest
from liga_bot.services.role_service import RoleService
from liga_bot.ui.roles import (
    ConfirmarRolButton,
    EquipoSelectView,
    PanelPedirRolView,
    SolicitudRolModal,
    TicketView,
)

# ---------------------------------------------------------------------------
# Helpers de Construcción de Mocks de Discord
# ---------------------------------------------------------------------------


def make_mock_role(role_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.Role con atributos id, name y mention."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_mock_category(category_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.CategoryChannel."""
    category = MagicMock(spec=discord.CategoryChannel)
    category.id = category_id
    category.name = name
    category.channels = []
    return category


def make_mock_channel(
    channel_id: int,
    name: str,
    category: MagicMock | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.TextChannel con soporte de send y delete."""
    chan = AsyncMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.category = category
    chan.guild = guild
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    chan.delete = AsyncMock()
    return chan


def make_mock_member(
    user_id: int,
    name: str = "TestPlayer",
    roles: list[MagicMock] | None = None,
    guild: MagicMock | None = None,
    is_admin: bool = False,
    can_manage_guild: bool = False,
) -> AsyncMock:
    """Crea un mock de discord.Member con gestión reactiva de roles y apodo en memoria."""
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.nick = None
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])
    member.guild = guild

    # Permisos de servidor
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = is_admin
    member.guild_permissions.manage_guild = can_manage_guild or is_admin

    async def mock_add_roles(*new_roles: discord.Role, **_kwargs: object) -> None:
        for r in new_roles:
            if r not in member.roles:
                member.roles.append(r)

    async def mock_remove_roles(*rem_roles: discord.Role, **_kwargs: object) -> None:
        for r in rem_roles:
            if r in member.roles:
                member.roles.remove(r)

    async def mock_edit(*, nick: str | None = None, **_kwargs: object) -> None:
        if nick is not None:
            member.nick = nick
            member.display_name = nick

    member.add_roles = AsyncMock(side_effect=mock_add_roles)
    member.remove_roles = AsyncMock(side_effect=mock_remove_roles)
    member.edit = AsyncMock(side_effect=mock_edit)
    member.send = AsyncMock()
    return member


def make_mock_guild(
    settings: Settings,
    extra_roles: list[MagicMock] | None = None,
) -> MagicMock:
    """Crea un mock de discord.Guild con roles oficiales, categoría de tickets y canal builder."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = settings.guild_id
    guild.name = "RCL Official League"

    # Rol @everyone
    default_role = make_mock_role(settings.guild_id, "@everyone")
    guild.default_role = default_role

    # Roles base configurados
    staff_role = make_mock_role(settings.staff_role_id, "Staff") if settings.staff_role_id else None
    ceo_role = make_mock_role(settings.ceo_role_id, "CEO") if settings.ceo_role_id else None
    sin_verificar_role = (
        make_mock_role(settings.sin_verificar_role_id, "Sin Verificar")
        if settings.sin_verificar_role_id
        else None
    )
    free_role = make_mock_role(888001, settings.free_role_name)

    # 20 roles de equipos oficiales
    official_roles = [make_mock_role(7000 + i, team) for i, team in enumerate(TEAMS_ALL)]

    all_roles: list[MagicMock] = [default_role]
    if staff_role:
        all_roles.append(staff_role)
    if ceo_role:
        all_roles.append(ceo_role)
    if sin_verificar_role:
        all_roles.append(sin_verificar_role)
    all_roles.append(free_role)
    all_roles.extend(official_roles)
    if extra_roles:
        all_roles.extend(extra_roles)

    role_id_map = {r.id: r for r in all_roles}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_id_map.get(rid)

    # Bot Member
    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999999999999
    bot_member.name = "LigaBot"
    guild.me = bot_member

    # Categoría de tickets de rol
    ticket_category = (
        make_mock_category(settings.ticket_rol_category_id, "Tickets de Rol")
        if settings.ticket_rol_category_id
        else None
    )

    channels: list[AsyncMock] = []
    channel_map: dict[int, AsyncMock | MagicMock] = {}
    if ticket_category:
        channel_map[ticket_category.id] = ticket_category
    guild.channels = channels
    guild.get_channel.side_effect = lambda cid: channel_map.get(cid)

    # Registro de miembros
    members_map: dict[int, AsyncMock] = {}
    guild._members_map = members_map
    guild.get_member.side_effect = lambda uid: members_map.get(uid)

    async def mock_fetch_member(uid: int) -> AsyncMock:
        if uid in members_map:
            return members_map[uid]
        mock_resp = MagicMock(status=404, reason="Not Found")
        raise discord.NotFound(mock_resp, f"Member {uid} not found")

    guild.fetch_member = AsyncMock(side_effect=mock_fetch_member)

    # Creador de canales dinámicos
    channel_seq = [5500]

    async def mock_create_text_channel(
        name: str,
        overwrites: dict[object, discord.PermissionOverwrite] | None = None,
        category: discord.CategoryChannel | None = None,
    ) -> AsyncMock:
        chan_id = channel_seq[0]
        channel_seq[0] += 1
        chan = make_mock_channel(channel_id=chan_id, name=name, category=category, guild=guild)
        chan.overwrites = overwrites
        channels.append(chan)
        channel_map[chan.id] = chan
        return chan

    guild.create_text_channel = AsyncMock(side_effect=mock_create_text_channel)
    return guild


def make_mock_interaction(
    user: AsyncMock,
    guild: MagicMock,
    channel: AsyncMock | None = None,
    role_service: RoleService | None = None,
    bot: MagicMock | None = None,
) -> MagicMock:
    """Crea un mock de discord.Interaction con soporte asíncrono para response y followup."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild = guild
    interaction.channel = channel
    interaction.channel_id = channel.id if channel else 0
    interaction.data = {}

    interaction.response = MagicMock(spec=discord.InteractionResponse)
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    async def mock_defer(ephemeral: bool = False, thinking: bool = False) -> None:
        interaction.response.is_done.return_value = True

    interaction.response.defer = AsyncMock(side_effect=mock_defer)

    interaction.followup = MagicMock(spec=discord.Webhook)
    interaction.followup.send = AsyncMock()

    client = bot or MagicMock()
    if role_service is not None:
        client.role_service = role_service
    interaction.client = client

    return interaction


# ---------------------------------------------------------------------------
# Fixtures de Entorno y Persistencia E2E
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_settings() -> Settings:
    """Configuración determinista para la suite de pruebas E2E."""
    return Settings(
        guild_id=1547725310508667010,
        staff_role_id=1547729760384319518,
        admin_role_id=1548795786110967919,
        ceo_role_id=1548795782360993842,
        sin_verificar_role_id=1550000000000000001,
        ticket_rol_category_id=1550000000000000002,
        free_role_name="Libre",
    )


@pytest_asyncio.fixture
async def session_factory(
    migrated_db: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Factoría de sesiones asíncronas con truncado exhaustivo previo y posterior."""
    factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE role_requests CASCADE;"))
        await session.commit()
    yield factory
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE role_requests CASCADE;"))
        await session.commit()


@pytest.fixture
def e2e_role_service(
    session_factory: async_sessionmaker[AsyncSession],
    e2e_settings: Settings,
) -> RoleService:
    """Servicio RoleService conectado a la base de datos PostgreSQL real migrada."""
    return RoleService(session_factory=session_factory, settings=e2e_settings)


# ===========================================================================
# 1. Verificación de Purga de Archivos Monolíticos
# ===========================================================================


class TestMonolithPurgeVerification:
    """Valida la eliminación total de los archivos monolíticos obsoletos del repositorio."""

    def test_monolithic_files_do_not_exist_in_working_tree(self) -> None:
        """Verifica que src/liga_bot/cogs/liga_bot.py y solicitud_rol.py no existan físicamente."""
        repo_root = Path(__file__).resolve().parent.parent

        legacy_liga_bot = repo_root / "src" / "liga_bot" / "cogs" / "liga_bot.py"
        legacy_solicitud_rol = repo_root / "src" / "liga_bot" / "cogs" / "solicitud_rol.py"

        assert not legacy_liga_bot.exists(), (
            f"El archivo monolítico {legacy_liga_bot} aún existe en el árbol de trabajo."
        )
        assert not legacy_solicitud_rol.exists(), (
            f"El archivo monolítico {legacy_solicitud_rol} aún existe en el árbol de trabajo."
        )

    def test_monolithic_modules_are_not_importable(self) -> None:
        """Verifica que los módulos eliminados no puedan ser resueltos por Python."""
        assert importlib.util.find_spec("liga_bot.cogs.liga_bot") is None
        assert importlib.util.find_spec("liga_bot.cogs.solicitud_rol") is None

    def test_default_extensions_registers_roles_and_omits_legacy_monoliths(self) -> None:
        """Valida que DEFAULT_EXTENSIONS incluya 'liga_bot.cogs.roles' y no los monolitos."""
        assert "liga_bot.cogs.roles" in DEFAULT_EXTENSIONS
        assert "liga_bot.cogs.liga_bot" not in DEFAULT_EXTENSIONS
        assert "liga_bot.cogs.solicitud_rol" not in DEFAULT_EXTENSIONS

    @pytest.mark.asyncio
    async def test_roles_cog_lifecycle_registration_and_member_join(
        self, e2e_settings: Settings, e2e_role_service: RoleService
    ) -> None:
        """Valida que RolesCog registre vistas y dynamic items en cog_load."""
        bot = MagicMock(spec=LigaBot)
        bot.settings = e2e_settings
        bot.role_service = e2e_role_service
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()

        cog = RolesCog(bot)
        await cog.cog_load()

        assert bot.add_view.call_count == 2
        registered_views = [c[0][0] for c in bot.add_view.call_args_list]
        assert any(isinstance(v, PanelPedirRolView) for v in registered_views)
        assert any(isinstance(v, TicketView) for v in registered_views)
        bot.add_dynamic_items.assert_called_once_with(ConfirmarRolButton)

        # Verifica listener on_member_join delegando en RoleService
        guild = make_mock_guild(e2e_settings)
        new_member = make_mock_member(user_id=881122, roles=[guild.default_role], guild=guild)
        await cog.on_member_join(new_member)
        sin_verificar_role = guild.get_role(e2e_settings.sin_verificar_role_id)
        assert sin_verificar_role in new_member.roles


# ===========================================================================
# 2. Ciclo de Vida Completo 1: Solicitud y Aprobación de Equipo Oficial
# ===========================================================================


class TestFullLifecycleOfficialTeamApproval:
    """
    Prueba E2E del ciclo de vida completo de verificación de equipo oficial:
    Miembro entra -> auto-asignación "Sin Verificar" -> envío de bienvenida
    -> interacción con modal -> selección de equipo oficial -> creación de canal privado
    con PermissionOverwrites de seguridad -> persistencia transaccional PENDING
    -> aprobación por staff -> asignación de rol de equipo, remoción de "Sin Verificar",
    actualización de apodo y persistencia transaccional APPROVED.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_member_join_modal_ticket_staff_confirm_success(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        guild = make_mock_guild(e2e_settings)
        sin_verificar_role = guild.get_role(e2e_settings.sin_verificar_role_id)
        staff_role = guild.get_role(e2e_settings.staff_role_id)
        team_name = "Vanguard Gaming"
        team_role = discord.utils.get(guild.roles, name=team_name)

        assert sin_verificar_role is not None
        assert staff_role is not None
        assert team_role is not None

        # 1. On-boarding: Nuevo miembro entra al servidor
        member_id = 112233445566
        new_member = make_mock_member(
            user_id=member_id,
            name="PinguFaker",
            roles=[guild.default_role],
            guild=guild,
        )
        guild._members_map[member_id] = new_member

        # Invocación de handle_member_join
        join_ok = await e2e_role_service.handle_member_join(new_member)
        assert join_ok is True
        assert sin_verificar_role in new_member.roles
        new_member.send.assert_awaited_once()
        welcome_text = new_member.send.await_args[0][0]
        assert "¡Bienvenido/a" in welcome_text
        assert "/pedir-rol" in welcome_text

        # 2. El miembro solicita rol abriendo y enviando SolicitudRolModal
        modal = SolicitudRolModal()
        modal.nombre_lol._value = "PinguFaker"
        modal.riot_tag._value = "EUW"

        inter_modal = make_mock_interaction(
            user=new_member, guild=guild, role_service=e2e_role_service
        )
        await modal.on_submit(inter_modal)

        inter_modal.response.send_message.assert_awaited_once()
        modal_args, modal_kwargs = inter_modal.response.send_message.call_args
        assert "Selecciona tu equipo" in modal_args[0]
        assert modal_kwargs.get("ephemeral") is True
        select_view: EquipoSelectView = modal_kwargs.get("view")
        assert isinstance(select_view, EquipoSelectView)
        assert select_view.select.nombre_lol == "PinguFaker"
        assert select_view.select.riot_tag == "EUW"

        # 3. El miembro selecciona su equipo oficial "Vanguard Gaming"
        select_view.select.values = [team_name]
        inter_select = make_mock_interaction(
            user=new_member, guild=guild, role_service=e2e_role_service
        )

        await select_view.select.callback(inter_select)

        # Verificación de respuesta diferida y notificación efímera con mención
        inter_select.response.defer.assert_awaited_once_with(ephemeral=True)
        inter_select.followup.send.assert_awaited_once()
        followup_msg = inter_select.followup.send.await_args[0][0]
        assert "Ticket creado en" in followup_msg

        # Verificación del canal de ticket creado en Discord
        created_channels = [c for c in guild.channels if c.name == "rol-pingufaker"]
        assert len(created_channels) == 1
        ticket_channel = created_channels[0]
        assert ticket_channel.category.id == e2e_settings.ticket_rol_category_id

        # Verificación de sobreescritura de permisos (PermissionOverwrites) estrictos
        overwrites = ticket_channel.overwrites
        assert overwrites[guild.default_role].read_messages is False
        assert overwrites[guild.default_role].send_messages is False
        assert overwrites[new_member].read_messages is True
        assert overwrites[new_member].send_messages is True
        assert overwrites[new_member].attach_files is True
        assert overwrites[guild.me].read_messages is True
        assert overwrites[guild.me].send_messages is True
        assert overwrites[guild.me].manage_channels is True
        assert overwrites[staff_role].read_messages is True
        assert overwrites[staff_role].send_messages is True
        ceo_role = guild.get_role(e2e_settings.ceo_role_id)
        if ceo_role:
            assert overwrites[ceo_role].read_messages is True
            assert overwrites[ceo_role].send_messages is True

        # Verificación del estado en la base de datos PostgreSQL real (PGlite)
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.canal_id == ticket_channel.id)
            result = await session.execute(stmt)
            db_request = result.scalar_one_or_none()
            assert db_request is not None
            assert db_request.user_id == member_id
            assert db_request.nombre_lol == "PinguFaker"
            assert db_request.riot_tag == "EUW"
            assert db_request.equipo == team_name
            assert db_request.estado == RoleRequestStatus.PENDING
            assert db_request.staff_id is None

        # Verificación de publicación del mensaje en el canal de ticket con TicketView
        ticket_channel.send.assert_awaited_once()
        send_kwargs = ticket_channel.send.await_args.kwargs
        ticket_view: TicketView = send_kwargs.get("view")
        assert isinstance(ticket_view, TicketView)
        assert ticket_view.confirm_button is not None
        assert ticket_view.confirm_button.custom_id == f"confirmar_rol:{member_id}:Vanguard Gaming"

        # 4. Un miembro del staff interactúa con el botón ConfirmarRolButton
        staff_id = 998877665544
        staff_member = make_mock_member(
            user_id=staff_id,
            name="StaffAdmin",
            roles=[guild.default_role, staff_role],
            guild=guild,
            can_manage_guild=True,
        )
        guild._members_map[staff_id] = staff_member

        inter_confirm = make_mock_interaction(
            user=staff_member,
            guild=guild,
            channel=ticket_channel,
            role_service=e2e_role_service,
        )

        # Confirmación del botón ejecutada por el staff
        with patch.object(ticket_view.confirm_button, "_schedule_deletion") as mock_schedule:
            await ticket_view.confirm_button.callback(inter_confirm)
            mock_schedule.assert_called_once()

        # Verificaciones en Discord:
        # - Rol del equipo asignado
        assert team_role in new_member.roles
        # - Rol "Sin Verificar" removido
        assert sin_verificar_role not in new_member.roles
        # - Apodo actualizado con formato "PinguFaker #EUW"
        assert new_member.nick == "PinguFaker #EUW"

        # Verificación en la base de datos PostgreSQL: estado APPROVED y staff_id registrado
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.canal_id == ticket_channel.id)
            result = await session.execute(stmt)
            updated_req = result.scalar_one()
            assert updated_req.estado == RoleRequestStatus.APPROVED
            assert updated_req.staff_id == staff_id
            assert updated_req.updated_at is not None

        # Verificación de mensaje de confirmación emitido en el canal
        inter_confirm.response.send_message.assert_awaited_once()
        confirm_msg = inter_confirm.response.send_message.await_args[0][0]
        assert "Vanguard Gaming confirmado para PinguFaker" in confirm_msg
        assert "Este canal se eliminará en 5 segundos." in confirm_msg


# ===========================================================================
# 3. Ciclo de Vida Completo 2: Solicitud de Rol "Libre" (Sin Ticket)
# ===========================================================================


class TestFullLifecycleFreeAgentAssignment:
    """
    Prueba E2E de asignación directa de Agente Libre:
    Miembro con 'Sin Verificar' solicita rol -> modal -> selecciona 'Libre'
    -> asignación directa de rol 'Libre' -> remoción de 'Sin Verificar'
    -> actualización de apodo -> registro transaccional APPROVED sin creación de canal.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_member_modal_select_libre_immediate_approval(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        guild = make_mock_guild(e2e_settings)
        sin_verificar_role = guild.get_role(e2e_settings.sin_verificar_role_id)
        free_role = discord.utils.get(guild.roles, name=e2e_settings.free_role_name)

        assert sin_verificar_role is not None
        assert free_role is not None

        # Miembro inicializado con rol "Sin Verificar"
        member_id = 778899001122
        member = make_mock_member(
            user_id=member_id,
            name="SoloQueueWarrior",
            roles=[guild.default_role, sin_verificar_role],
            guild=guild,
        )
        guild._members_map[member_id] = member

        # Envío del modal
        modal = SolicitudRolModal()
        modal.nombre_lol._value = "SoloQueueWarrior"
        modal.riot_tag._value = "1337"

        inter_modal = make_mock_interaction(user=member, guild=guild, role_service=e2e_role_service)
        await modal.on_submit(inter_modal)

        select_view: EquipoSelectView = inter_modal.response.send_message.call_args.kwargs["view"]

        # Selección de "Libre" en el selector
        select_view.select.values = [e2e_settings.free_role_name]
        inter_select = make_mock_interaction(
            user=member, guild=guild, role_service=e2e_role_service
        )

        await select_view.select.callback(inter_select)

        # Verificaciones en Discord:
        # - Asignación del rol "Libre"
        assert free_role in member.roles
        # - Remoción del rol "Sin Verificar"
        assert sin_verificar_role not in member.roles
        # - Apodo formateado a "SoloQueueWarrior #1337"
        assert member.nick == "SoloQueueWarrior #1337"
        # - Cero canales de tickets creados
        assert len(guild.channels) == 0

        # Verificación de respuesta efímera positiva
        inter_select.response.send_message.assert_awaited_once()
        resp_msg = inter_select.response.send_message.await_args[0][0]
        assert "Rol Libre asignado correctamente." in resp_msg

        # Verificación en la base de datos PostgreSQL real (PGlite):
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.user_id == member_id)
            result = await session.execute(stmt)
            db_req = result.scalar_one_or_none()
            assert db_req is not None
            assert db_req.nombre_lol == "SoloQueueWarrior"
            assert db_req.riot_tag == "1337"
            assert db_req.equipo == e2e_settings.free_role_name
            assert db_req.canal_id is None
            assert db_req.estado == RoleRequestStatus.APPROVED
            assert db_req.staff_id is None


# ===========================================================================
# 4. Ciclo de Vida Completo 3: Creación de Ticket y Denegación por Staff
# ===========================================================================


class TestFullLifecycleStaffDenial:
    """
    Prueba E2E de denegación de solicitud por parte del staff:
    Creación de ticket para equipo oficial -> persistencia PENDING
    -> staff pulsa 'Denegar Rol' en TicketView -> persistencia DENIED
    -> rol de equipo no asignado, 'Sin Verificar' conservado y borrado programado.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_ticket_created_and_staff_denies_request(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        guild = make_mock_guild(e2e_settings)
        sin_verificar_role = guild.get_role(e2e_settings.sin_verificar_role_id)
        staff_role = guild.get_role(e2e_settings.staff_role_id)
        team_name = "Nexus Esports"
        team_role = discord.utils.get(guild.roles, name=team_name)

        assert sin_verificar_role is not None
        assert staff_role is not None
        assert team_role is not None

        # Miembro solicitante
        member_id = 334455667788
        member = make_mock_member(
            user_id=member_id,
            name="SuspectPlayer",
            roles=[guild.default_role, sin_verificar_role],
            guild=guild,
        )
        guild._members_map[member_id] = member

        # Creación de ticket mediante RoleService
        ok, _msg, channel = await e2e_role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="SuspectPlayer",
            riot_tag="SUS",
            equipo=team_name,
        )
        assert ok is True
        assert channel is not None

        # Instancia de TicketView asociada al ticket
        ticket_view = TicketView(user_id=member_id, equipo=team_name)
        deny_button = next(
            item
            for item in ticket_view.children
            if getattr(item, "custom_id", None) == "solicitud_rol:ticket_view:denegar"
        )

        # 1. Intento de denegación por usuario no staff (debe ser rechazado)
        non_staff = make_mock_member(user_id=999911, name="Imposter", guild=guild)
        inter_denied_fail = make_mock_interaction(
            user=non_staff,
            guild=guild,
            channel=channel,
            role_service=e2e_role_service,
        )
        await deny_button.callback(inter_denied_fail)
        inter_denied_fail.response.send_message.assert_awaited_once_with(
            "Solo el staff puede denegar solicitudes de rol.", ephemeral=True
        )

        # Verificar que en base de datos sigue PENDING
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.canal_id == channel.id)
            res = await session.execute(stmt)
            assert res.scalar_one().estado == RoleRequestStatus.PENDING

        # 2. Denegación legítima por miembro del staff
        staff_id = 1234509876
        staff_member = make_mock_member(
            user_id=staff_id,
            name="ModeratorStaff",
            roles=[guild.default_role, staff_role],
            guild=guild,
        )
        inter_deny_ok = make_mock_interaction(
            user=staff_member,
            guild=guild,
            channel=channel,
            role_service=e2e_role_service,
        )

        with patch.object(ticket_view, "_schedule_deletion") as mock_schedule:
            await deny_button.callback(inter_deny_ok)
            mock_schedule.assert_called_once()

        # Verificación en base de datos PostgreSQL: estado DENIED y staff_id registrado
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.canal_id == channel.id)
            res = await session.execute(stmt)
            denied_req = res.scalar_one()
            assert denied_req.estado == RoleRequestStatus.DENIED
            assert denied_req.staff_id == staff_id

        # Verificación de invariantes en Discord:
        # - NO se asignó el rol de equipo
        assert team_role not in member.roles
        # - El rol "Sin Verificar" NO se removió
        assert sin_verificar_role in member.roles
        # - Mensaje de denegación y aviso de eliminación
        inter_deny_ok.response.send_message.assert_awaited_once()
        deny_msg = inter_deny_ok.response.send_message.await_args[0][0]
        assert "Solicitud de rol denegada." in deny_msg
        assert "Este canal se eliminará en 5 segundos." in deny_msg


# ===========================================================================
# 5. Prevención de Solicitudes Duplicadas
# ===========================================================================


class TestDuplicateRequestPrevention:
    """Valida que un usuario con solicitud PENDING activa no pueda crear un segundo ticket."""

    @pytest.mark.asyncio
    async def test_active_pending_request_prevents_second_ticket_creation(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        guild = make_mock_guild(e2e_settings)
        member = make_mock_member(user_id=445566778899, name="MultiRequester", guild=guild)
        guild._members_map[member.id] = member

        # 1. Primera solicitud exitosa para "Frostbite Esports"
        ok1, _msg1, channel1 = await e2e_role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="MultiRequester",
            riot_tag="RCL",
            equipo="Frostbite Esports",
        )
        assert ok1 is True
        assert channel1 is not None

        # 2. Intento de segunda solicitud a través del componente EquipoSelect
        component_select = EquipoSelectView(
            nombre_lol="MultiRequester",
            riot_tag="RCL",
        ).select
        component_select.values = ["Vanguard Gaming"]

        inter_second = make_mock_interaction(
            user=member, guild=guild, role_service=e2e_role_service
        )
        await component_select.callback(inter_second)

        # Verificación de rechazo con mensaje descriptivo
        inter_second.response.defer.assert_awaited_once_with(ephemeral=True)
        inter_second.followup.send.assert_awaited_once_with(
            "Ya tienes una solicitud de rol pendiente.", ephemeral=True
        )

        # Verificación de que no se creó un segundo canal
        assert len(guild.channels) == 1

        # Verificación en la base de datos: únicamente 1 registro PENDING para este usuario
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.user_id == member.id)
            res = await session.execute(stmt)
            records = res.scalars().all()
            assert len(records) == 1
            assert records[0].equipo == "Frostbite Esports"
            assert records[0].estado == RoleRequestStatus.PENDING

        # 3. Tras resolver la solicitud (ej. DENIED), el usuario puede solicitar nuevamente
        staff_member = make_mock_member(
            user_id=111222, roles=[guild.get_role(e2e_settings.staff_role_id)], guild=guild
        )
        ok_deny, _ = await e2e_role_service.deny_role_request(
            guild=guild, channel_id=channel1.id, staff_member=staff_member
        )
        assert ok_deny is True

        # Ahora la segunda solicitud es aceptada y crea el nuevo canal
        ok2, _msg2, channel2 = await e2e_role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="MultiRequester",
            riot_tag="RCL",
            equipo="Vanguard Gaming",
        )
        assert ok2 is True
        assert channel2 is not None
        assert channel2.id != channel1.id


# ===========================================================================
# 6. Serialización y Ciclo de Vida de DynamicItem (ConfirmarRolButton)
# ===========================================================================


class TestDynamicItemSerializationAndLifecycle:
    """Valida regex, parsing y reconstrucción de ConfirmarRolButton ante reinicios."""

    def test_confirmar_rol_button_template_regex_matching_and_rejection(self) -> None:
        """Verifica la validez estricta de la plantilla regex de ConfirmarRolButton."""
        template = ConfirmarRolButton.__discord_ui_compiled_template__

        # Casos válidos
        m1 = template.match("confirmar_rol:123456789:Nexus Esports")
        assert m1 is not None
        assert m1.group("user_id") == "123456789"
        assert m1.group("equipo") == "Nexus Esports"

        m2 = template.match("confirmar_rol:987654321012345678:Vanguard Gaming")
        assert m2 is not None
        assert m2.group("user_id") == "987654321012345678"
        assert m2.group("equipo") == "Vanguard Gaming"

        # Casos inválidos (rechazados)
        assert template.match("confirmar_rol:no_number:Nexus Esports") is None
        assert template.match("confirmar_rol::Nexus Esports") is None
        assert template.match("confirmar_rol:123456789:") is None
        assert template.match("denegar_rol:123456789:Nexus Esports") is None
        assert template.match("random_prefix:confirmar_rol:123:Team") is None

    @pytest.mark.asyncio
    async def test_confirmar_rol_button_bot_restart_deserialization_and_callback(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Simula el reinicio del bot: reconstrucción desde custom_id y ejecución por staff."""
        guild = make_mock_guild(e2e_settings)
        staff_role = guild.get_role(e2e_settings.staff_role_id)
        team_role = discord.utils.get(guild.roles, name="Storm Legion")
        assert staff_role is not None
        assert team_role is not None

        member_id = 667788990011
        member = make_mock_member(
            user_id=member_id,
            name="StormChaser",
            roles=[guild.default_role],
            guild=guild,
        )
        guild._members_map[member_id] = member

        # Crear solicitud en BD asociada a un canal de ticket
        channel_id = 998877
        ticket_channel = make_mock_channel(
            channel_id=channel_id, name="rol-stormchaser", guild=guild
        )
        guild.channels.append(ticket_channel)
        guild.get_channel.side_effect = lambda cid: ticket_channel if cid == channel_id else None

        async with session_factory():
            repo = e2e_role_service.session_factory()
            async with repo as sess:
                from liga_bot.repositories.role_request_repo import RoleRequestRepository

                r_repo = RoleRequestRepository(sess)
                await r_repo.create_request(
                    user_id=member_id,
                    nombre_lol="StormChaser",
                    riot_tag="EUW",
                    equipo="Storm Legion",
                    canal_id=channel_id,
                )
                await sess.commit()

        # SIMULACIÓN DE REINICIO DEL BOT:
        # La memoria RAM de vistas se reinicia. Discord despacha custom_id.
        custom_id_deserialized = f"confirmar_rol:{member_id}:Storm Legion"
        match = ConfirmarRolButton.__discord_ui_compiled_template__.match(custom_id_deserialized)
        assert match is not None

        # Reconstrucción mediante from_custom_id
        reconstructed_button = await ConfirmarRolButton.from_custom_id(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            match,
        )

        assert isinstance(reconstructed_button, ConfirmarRolButton)
        assert reconstructed_button.user_id == member_id
        assert reconstructed_button.equipo == "Storm Legion"
        assert reconstructed_button.custom_id == custom_id_deserialized

        # Ejecución del callback por parte del staff tras el reinicio
        staff_member = make_mock_member(
            user_id=554433,
            name="RestartStaff",
            roles=[guild.default_role, staff_role],
            guild=guild,
        )
        inter_staff = make_mock_interaction(
            user=staff_member,
            guild=guild,
            channel=ticket_channel,
            role_service=e2e_role_service,
        )

        with patch.object(reconstructed_button, "_schedule_deletion"):
            await reconstructed_button.callback(inter_staff)

        # Validación de que la confirmación se aplicó en BD tras el reinicio
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.canal_id == channel_id)
            res = await session.execute(stmt)
            approved_req = res.scalar_one()
            assert approved_req.estado == RoleRequestStatus.APPROVED
            assert approved_req.staff_id == staff_member.id

        # Validación en Discord
        assert team_role in member.roles
        assert member.nick == "StormChaser #EUW"


# ===========================================================================
# 7. Seguridad Administrativa y Control de Acceso
# ===========================================================================


class TestAdminCommandSecurityGating:
    """Valida los permisos de gestión de servidor en Cogs de administración."""

    def test_schedule_cog_commands_have_manage_guild_default_permission(self) -> None:
        """Verifica @app_commands.default_permissions(manage_guild=True) en ScheduleCog."""
        bot = MagicMock(spec=LigaBot)
        cog = ScheduleCog(bot)

        for cmd_name in ("crear_partido", "crear_jornada", "importar_jornada"):
            cmd = getattr(cog, cmd_name)
            assert cmd.default_permissions is not None, f"{cmd_name} carece de default_permissions"
            assert cmd.default_permissions.manage_guild is True, (
                f"{cmd_name} debe requerir manage_guild=True"
            )

    def test_tickets_cog_commands_have_manage_guild_default_permission(self) -> None:
        """Verifica @app_commands.default_permissions(manage_guild=True) en TicketsCog."""
        bot = MagicMock(spec=LigaBot)
        cog = TicketsCog(bot, auto_start=False)

        for cmd_name in ("revisar_tickets", "revisar_tickets_manual"):
            cmd = getattr(cog, cmd_name)
            assert cmd.default_permissions is not None, f"{cmd_name} carece de default_permissions"
            assert cmd.default_permissions.manage_guild is True, (
                f"{cmd_name} debe requerir manage_guild=True"
            )

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked_from_admin_commands(
        self, e2e_settings: Settings
    ) -> None:
        """Valida que usuarios no autorizados sean rechazados en comandos y botones staff."""
        guild = make_mock_guild(e2e_settings)
        unauth_user = make_mock_member(user_id=123999, name="NormalUser", guild=guild)

        # 1. Intento de ejecutar /crear-partido
        bot = MagicMock(spec=LigaBot)
        bot.settings = e2e_settings
        schedule_cog = ScheduleCog(bot, settings=e2e_settings)

        inter_schedule = make_mock_interaction(user=unauth_user, guild=guild)
        await schedule_cog.crear_partido.callback(
            schedule_cog,
            inter_schedule,
            jornada=1,
            equipo1="Team1",
            equipo2="Team2",
        )
        inter_schedule.response.send_message.assert_awaited_once()
        msg_schedule = inter_schedule.response.send_message.await_args[0][0]
        assert "No tienes permisos" in msg_schedule

        # 2. Intento de ejecutar /revisar-tickets
        tickets_cog = TicketsCog(bot, auto_start=False)
        inter_tickets = make_mock_interaction(user=unauth_user, guild=guild)
        await tickets_cog.revisar_tickets.callback(tickets_cog, inter_tickets)
        inter_tickets.response.send_message.assert_awaited_once()
        msg_tickets = inter_tickets.response.send_message.await_args[0][0]
        assert "No tienes permiso" in msg_tickets

        # 3. Intento de pulsar ConfirmarRolButton
        btn = ConfirmarRolButton(user_id=111, equipo="Nexus Esports")
        inter_btn = make_mock_interaction(user=unauth_user, guild=guild)
        await btn.callback(inter_btn)
        inter_btn.response.send_message.assert_awaited_once_with(
            "Solo el staff puede confirmar solicitudes de rol.", ephemeral=True
        )


# ===========================================================================
# 8. Condiciones Límite y Resiliencia E2E
# ===========================================================================


class TestBoundaryAndEdgeCasesE2E:
    """Valida casos de borde: truncado de apodo a 32 caracteres y manejo de entidades faltantes."""

    @pytest.mark.asyncio
    async def test_nickname_truncation_to_32_characters_on_approval(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifica que apodos que exceden 32 caracteres sean recortados con seguridad."""
        guild = make_mock_guild(e2e_settings)
        staff_role = guild.get_role(e2e_settings.staff_role_id)
        team_role = discord.utils.get(guild.roles, name="Vanguard Gaming")

        member_id = 990011223344
        long_member = make_mock_member(
            user_id=member_id,
            name="SuperLongSummonerNameExceedingLimits",
            roles=[guild.default_role],
            guild=guild,
        )
        guild._members_map[member_id] = long_member

        ok, _, channel = await e2e_role_service.create_role_request_ticket(
            guild=guild,
            member=long_member,
            nombre_lol="SuperLongSummonerNameExceedingLimits",
            riot_tag="TAG99999",
            equipo="Vanguard Gaming",
        )
        assert ok is True
        assert channel is not None

        staff_user = make_mock_member(user_id=888111, roles=[staff_role], guild=guild)
        ok_confirm, _ = await e2e_role_service.confirm_role_request(
            guild=guild, channel_id=channel.id, staff_member=staff_user
        )
        assert ok_confirm is True
        assert team_role in long_member.roles

        # El apodo resultante debe tener longitud <= 32
        assert len(long_member.nick) <= 32
        expected_nick = "SuperLongSummonerNameExceedingLimits #TAG99999"[:32]
        assert long_member.nick == expected_nick

    @pytest.mark.asyncio
    async def test_missing_team_role_on_server_degrades_gracefully(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Valida que si el rol del equipo fue eliminado del servidor, el flujo complete en BD."""
        guild = make_mock_guild(e2e_settings)
        staff_role = guild.get_role(e2e_settings.staff_role_id)

        member = make_mock_member(user_id=556677, name="PlayerX", guild=guild)
        guild._members_map[member.id] = member

        ok, _, channel = await e2e_role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="PlayerX",
            riot_tag="TAG",
            equipo="NonExistentTeam",
        )
        assert ok is True
        assert channel is not None

        staff_user = make_mock_member(user_id=888222, roles=[staff_role], guild=guild)
        ok_confirm, msg = await e2e_role_service.confirm_role_request(
            guild=guild, channel_id=channel.id, staff_member=staff_user
        )
        assert ok_confirm is True
        assert "NonExistentTeam confirmado para PlayerX" in msg

        # Verificación en BD: estado APPROVED registrado
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.canal_id == channel.id)
            res = await session.execute(stmt)
            assert res.scalar_one().estado == RoleRequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_member_not_found_on_confirm_reports_error(
        self,
        e2e_role_service: RoleService,
        e2e_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Valida error si el miembro abandonó el servidor antes de la confirmación."""
        guild = make_mock_guild(e2e_settings)
        staff_role = guild.get_role(e2e_settings.staff_role_id)

        member = make_mock_member(user_id=999888777, name="Leaver", guild=guild)
        guild._members_map[member.id] = member

        ok, _, channel = await e2e_role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Leaver",
            riot_tag="TAG",
            equipo="Nexus Esports",
        )
        assert ok is True
        assert channel is not None

        # El miembro se va del servidor (se remueve de la caché y de fetch_member)
        del guild._members_map[member.id]

        staff_user = make_mock_member(user_id=888333, roles=[staff_role], guild=guild)
        ok_confirm, err_msg = await e2e_role_service.confirm_role_request(
            guild=guild, channel_id=channel.id, staff_member=staff_user
        )
        assert ok_confirm is False
        assert "El usuario solicitante no se encuentra en el servidor." in err_msg

        # Verificación en BD: estado permanece PENDING
        async with session_factory() as session:
            stmt = select(RoleRequest).where(RoleRequest.canal_id == channel.id)
            res = await session.execute(stmt)
            assert res.scalar_one().estado == RoleRequestStatus.PENDING
