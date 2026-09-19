"""
Pruebas unitarias para RolesCog, su ciclo de vida, comandos slash y verificación de
permisos administrativos (@app_commands.default_permissions) en ScheduleCog y TicketsCog.

Cubre:
1. RolesCog:
   - Registro de vistas persistentes y dynamic items en cog_load().
   - Listener on_member_join y delegación a RoleService.
   - /pedir-rol: apertura de SolicitudRolModal.
   - /asignar-rol: comprobación de staff, asignación de Libre y de equipos oficiales,
     eliminación de rol sin verificar, actualización de apodo y persistencia transaccional.
   - /publicar-panel-rol: comprobación de staff, publicación en canal destino y embed.
   - Idempotencia de la función setup().
2. Permisos administrativos (@app_commands.default_permissions(manage_guild=True)):
   - ScheduleCog: crear_partido, crear_jornada, importar_jornada.
   - TicketsCog: revisar_tickets, revisar_tickets_manual.
   - Alias /revisar-tickets-manual delegando en revisar_tickets.
3. DEFAULT_EXTENSIONS en bot.py incluye 'liga_bot.cogs.roles'.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.bot import DEFAULT_EXTENSIONS, LigaBot
from liga_bot.cogs.roles import RolesCog
from liga_bot.cogs.roles import setup as roles_setup
from liga_bot.cogs.schedule import ScheduleCog
from liga_bot.cogs.tickets import TicketsCog
from liga_bot.config import Settings
from liga_bot.models.enums import RoleRequestStatus
from liga_bot.models.role_request import RoleRequest
from liga_bot.repositories.role_request_repo import RoleRequestRepository
from liga_bot.services.role_service import RoleService
from liga_bot.services.ticket_service import TicketAuditResult, TicketService
from liga_bot.ui.roles import (
    ConfirmarRolButton,
    PanelPedirRolView,
    SolicitudRolModal,
    TicketView,
)

# ---------------------------------------------------------------------------
# Mocks auxiliares
# ---------------------------------------------------------------------------


def make_mock_member(
    user_id: int = 123456789,
    name: str = "TestUser",
    roles: list[discord.Role] | None = None,
    is_admin: bool = False,
    can_manage_guild: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Member."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = is_admin
    member.guild_permissions.manage_guild = can_manage_guild
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.edit = AsyncMock()
    return member


def make_mock_role(role_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.Role."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_mock_channel(channel_id: int = 987654321, name: str = "general") -> MagicMock:
    """Crea un mock de discord.TextChannel."""
    chan = MagicMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return chan


def make_mock_guild(guild_id: int = 1547725310508667010) -> MagicMock:
    """Crea un mock de discord.Guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "RCL League Server"
    guild.roles = []
    guild.get_role = MagicMock(return_value=None)
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    return guild


_SENTINEL = object()


def make_mock_interaction(
    user: discord.Member | None = None,
    guild: discord.Guild | None | object = _SENTINEL,
    channel: discord.TextChannel | None = None,
) -> MagicMock:
    """Crea un mock de discord.Interaction."""
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user or make_mock_member()
    inter.guild = make_mock_guild() if guild is _SENTINEL else guild
    inter.channel = channel or make_mock_channel()
    inter.channel_id = inter.channel.id if inter.channel else 0

    response = MagicMock(spec=discord.InteractionResponse)
    response.send_message = AsyncMock()
    response.send_modal = AsyncMock()
    response.defer = AsyncMock()
    inter.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock()
    inter.followup = followup

    return inter


def make_mock_bot(
    settings: Settings | None = None,
    role_service: RoleService | None = None,
) -> MagicMock:
    """Crea un mock de LigaBot / commands.Bot."""
    bot = MagicMock(spec=LigaBot)
    bot.settings = settings or Settings(
        staff_role_id=101,
        sin_verificar_role_id=202,
        free_role_name="Libre",
    )
    bot.role_service = role_service
    bot.cogs = {}
    bot.add_cog = AsyncMock()
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    return bot


# ===========================================================================
# 1. Ciclo de Vida y Extensiones de RolesCog
# ===========================================================================


class TestRolesCogLifecycle:
    """Pruebas para inicialización, carga y registro de extensiones."""

    def test_roles_cog_init(self) -> None:
        """Verifica que RolesCog se inicialice guardando la referencia al bot."""
        bot = make_mock_bot()
        cog = RolesCog(bot)
        assert cog.bot is bot
        assert cog.settings.free_role_name == "Libre"

    @pytest.mark.asyncio
    async def test_roles_cog_load_registers_views_and_dynamic_items(self) -> None:
        """Verifica que cog_load registre PanelPedirRolView, TicketView y ConfirmarRolButton."""
        bot = make_mock_bot()
        cog = RolesCog(bot)

        await cog.cog_load()

        assert bot.add_view.call_count == 2
        registered_views = [call.args[0] for call in bot.add_view.call_args_list]
        assert any(isinstance(v, PanelPedirRolView) for v in registered_views)
        assert any(isinstance(v, TicketView) for v in registered_views)

        bot.add_dynamic_items.assert_called_once_with(ConfirmarRolButton)

    @pytest.mark.asyncio
    async def test_setup_idempotency(self) -> None:
        """Verifica que setup() añada el Cog si no existe y lo omita si ya está añadido."""
        bot = make_mock_bot()

        # 1. Cog no cargado: debe añadirse
        await roles_setup(bot)
        bot.add_cog.assert_awaited_once()
        added_cog = bot.add_cog.await_args[0][0]
        assert isinstance(added_cog, RolesCog)

        # 2. Cog ya cargado: no debe añadirse de nuevo
        bot.cogs = {"Roles": added_cog}
        bot.add_cog.reset_mock()
        await roles_setup(bot)
        bot.add_cog.assert_not_awaited()

    def test_default_extensions_contains_roles_cog(self) -> None:
        """Verifica que DEFAULT_EXTENSIONS incluya 'liga_bot.cogs.roles'."""
        assert "liga_bot.cogs.roles" in DEFAULT_EXTENSIONS


# ===========================================================================
# 2. Listener on_member_join
# ===========================================================================


class TestRolesCogListeners:
    """Pruebas para el event listener on_member_join."""

    @pytest.mark.asyncio
    async def test_on_member_join_delegates_to_role_service(self) -> None:
        """Verifica que on_member_join invoque handle_member_join si role_service existe."""
        mock_service = MagicMock(spec=RoleService)
        mock_service.handle_member_join = AsyncMock(return_value=True)

        bot = make_mock_bot(role_service=mock_service)
        cog = RolesCog(bot)
        member = make_mock_member()

        await cog.on_member_join(member)

        mock_service.handle_member_join.assert_awaited_once_with(member)

    @pytest.mark.asyncio
    async def test_on_member_join_graceful_without_service(self) -> None:
        """Verifica que on_member_join no lance excepción si role_service es None."""
        bot = make_mock_bot(role_service=None)
        cog = RolesCog(bot)
        member = make_mock_member()

        # No debe lanzar excepción
        await cog.on_member_join(member)


# ===========================================================================
# 3. Comandos Slash de RolesCog
# ===========================================================================


class TestRolesCogCommands:
    """Pruebas unitarias para /pedir-rol, /asignar-rol y /publicar-panel-rol."""

    @pytest.mark.asyncio
    async def test_pedir_rol_sends_modal(self) -> None:
        """Verifica que /pedir-rol responda enviando SolicitudRolModal."""
        bot = make_mock_bot()
        cog = RolesCog(bot)
        inter = make_mock_interaction()

        await cog.pedir_rol.callback(cog, inter)

        inter.response.send_modal.assert_awaited_once()
        modal = inter.response.send_modal.await_args[0][0]
        assert isinstance(modal, SolicitudRolModal)

    def test_asignar_rol_default_permissions(self) -> None:
        """Verifica que /asignar-rol tenga default_permissions(manage_guild=True)."""
        bot = make_mock_bot()
        cog = RolesCog(bot)
        assert cog.asignar_rol.default_permissions is not None
        assert cog.asignar_rol.default_permissions.manage_guild is True

    @pytest.mark.asyncio
    async def test_asignar_rol_denied_for_non_staff(self) -> None:
        """Verifica que usuarios sin permiso de Staff sean rechazados en /asignar-rol."""
        bot = make_mock_bot()
        cog = RolesCog(bot)
        unauth_user = make_mock_member(roles=[])
        inter = make_mock_interaction(user=unauth_user)
        target_user = make_mock_member(user_id=999)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Planar Shock Pingus",
            nombre_lol="Faker",
            riot_tag="KR1",
        )

        inter.response.send_message.assert_awaited_once_with(
            "Solo el staff puede asignar roles.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_asignar_rol_free_role_success(self) -> None:
        """Verifica que /asignar-rol con equipo 'Libre' delegue en assign_free_role."""
        mock_service = MagicMock(spec=RoleService)
        mock_service.assign_free_role = AsyncMock(
            return_value=(True, "Rol Libre asignado correctamente.")
        )

        settings = Settings(staff_role_id=101, free_role_name="Libre")
        bot = make_mock_bot(settings=settings, role_service=mock_service)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user)
        target_user = make_mock_member(user_id=999)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Libre",
            nombre_lol="Invocador",
            riot_tag="EUW",
        )

        mock_service.assign_free_role.assert_awaited_once_with(target_user, "Invocador", "EUW")
        inter.response.send_message.assert_awaited_once_with(
            "Rol Libre asignado correctamente.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_asignar_rol_free_role_service_missing(self) -> None:
        """Verifica error cuando role_service no está disponible para rol Libre."""
        settings = Settings(staff_role_id=101, free_role_name="Libre")
        bot = make_mock_bot(settings=settings, role_service=None)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user)
        target_user = make_mock_member(user_id=999)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Libre",
            nombre_lol="Invocador",
            riot_tag="EUW",
        )

        inter.response.send_message.assert_awaited_once_with(
            "El servicio de roles no está disponible.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_asignar_rol_outside_guild(self) -> None:
        """Verifica que /asignar-rol rechace invocaciones fuera de un servidor."""
        settings = Settings(staff_role_id=101)
        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=None)
        target_user = make_mock_member(user_id=999)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Fnix Esports",
            nombre_lol="Player",
            riot_tag="123",
        )

        inter.response.send_message.assert_awaited_once()
        assert "dentro de un servidor" in inter.response.send_message.await_args[0][0]

    @pytest.mark.asyncio
    async def test_asignar_rol_team_role_not_found(self) -> None:
        """Verifica mensaje de error si el rol del equipo solicitado no existe en el servidor."""
        settings = Settings(staff_role_id=101)
        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        guild = make_mock_guild()
        guild.roles = []  # Sin roles

        inter = make_mock_interaction(user=staff_user, guild=guild)
        target_user = make_mock_member(user_id=999)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Equipo Inexistente",
            nombre_lol="Player",
            riot_tag="123",
        )

        inter.response.send_message.assert_awaited_once_with(
            "El rol 'Equipo Inexistente' no existe en el servidor.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_asignar_rol_team_success_flow(self) -> None:
        """
        Verifica el flujo completo exitoso de /asignar-rol para un equipo oficial:
        - Remueve el rol 'Sin Verificar' del usuario.
        - Añade el rol del equipo.
        - Actualiza el apodo del miembro con formato 'NombreLoL #RiotTag'.
        - Registra la solicitud aprobada en BD.
        - Responde confirmando la asignación.
        """
        settings = Settings(staff_role_id=101, sin_verificar_role_id=202)
        sin_verificar_role = make_mock_role(202, "Sin Verificar")
        team_role = make_mock_role(303, "Planar Shock Pingus")

        guild = make_mock_guild()
        guild.roles = [sin_verificar_role, team_role]

        target_user = make_mock_member(user_id=555, name="TargetMember", roles=[sin_verificar_role])

        # Mock de session_factory para persistencia
        mock_session_factory = MagicMock(spec=async_sessionmaker)
        mock_session = AsyncMock(spec=AsyncSession)

        mock_role_service = MagicMock(spec=RoleService)
        mock_role_service.session_factory = mock_session_factory

        # Mock de repositorio
        mock_req = MagicMock(spec=RoleRequest)
        mock_req.id = 42

        bot = make_mock_bot(settings=settings, role_service=mock_role_service)
        cog = RolesCog(bot)

        staff_user = make_mock_member(user_id=111, roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=guild)

        @asynccontextmanager
        async def fake_tx_session(factory=None):
            yield mock_session

        with (
            patch("liga_bot.cogs.roles.transactional_session", fake_tx_session),
            patch("liga_bot.cogs.roles.RoleRequestRepository") as mock_repo_cls,
        ):
            mock_repo = MagicMock(spec=RoleRequestRepository)
            mock_repo.create_request = AsyncMock(return_value=mock_req)
            mock_repo.update_status = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            await cog.asignar_rol.callback(
                cog,
                inter,
                usuario=target_user,
                equipo="Planar Shock Pingus",
                nombre_lol="Faker",
                riot_tag="KR1",
            )

            # 1. Remoción de Sin Verificar
            target_user.remove_roles.assert_awaited_once_with(sin_verificar_role)

            # 2. Asignación del rol de equipo
            target_user.add_roles.assert_awaited_once_with(team_role)

            # 3. Actualización de apodo
            target_user.edit.assert_awaited_once_with(nick="Faker #KR1")

            # 4. Registro en BD
            mock_repo.create_request.assert_awaited_once_with(
                user_id=555,
                nombre_lol="Faker",
                riot_tag="KR1",
                equipo="Planar Shock Pingus",
                canal_id=None,
            )
            mock_repo.update_status.assert_awaited_once_with(
                request_id=42,
                estado=RoleRequestStatus.APPROVED,
                staff_id=111,
            )

            # 5. Respuesta de confirmación
            inter.response.send_message.assert_awaited_once()
            msg = inter.response.send_message.await_args[0][0]
            assert "Planar Shock Pingus" in msg
            assert "TargetMember" in msg

    @pytest.mark.asyncio
    async def test_asignar_rol_team_handles_forbidden(self) -> None:
        """Verifica que /asignar-rol capture discord.Forbidden al añadir rol."""
        settings = Settings(staff_role_id=101)
        team_role = make_mock_role(303, "Fnix Esports")
        guild = make_mock_guild()
        guild.roles = [team_role]

        target_user = make_mock_member(user_id=555)
        target_user.add_roles.side_effect = discord.Forbidden(
            MagicMock(status=403), "Missing permissions"
        )

        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=guild)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Fnix Esports",
            nombre_lol="Player",
            riot_tag="123",
        )

        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.await_args[0][0]
        assert "Permisos insuficientes" in msg

    def test_publicar_panel_rol_default_permissions(self) -> None:
        """Verifica que /publicar-panel-rol tenga default_permissions(manage_guild=True)."""
        bot = make_mock_bot()
        cog = RolesCog(bot)
        assert cog.publicar_panel_rol.default_permissions is not None
        assert cog.publicar_panel_rol.default_permissions.manage_guild is True

    @pytest.mark.asyncio
    async def test_publicar_panel_rol_denied_for_non_staff(self) -> None:
        """Verifica que usuarios sin permiso de Staff sean rechazados en /publicar-panel-rol."""
        bot = make_mock_bot()
        cog = RolesCog(bot)
        unauth_user = make_mock_member(roles=[])
        inter = make_mock_interaction(user=unauth_user)

        await cog.publicar_panel_rol.callback(cog, inter)

        inter.response.send_message.assert_awaited_once_with(
            "Solo el staff puede publicar el panel.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_publicar_panel_rol_success_to_explicit_channel(self) -> None:
        """Verifica que /publicar-panel-rol envíe embed y vista al canal indicado."""
        settings = Settings(staff_role_id=101)
        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        target_channel = make_mock_channel(channel_id=777, name="bienvenida")
        inter = make_mock_interaction(user=staff_user)

        await cog.publicar_panel_rol.callback(cog, inter, canal=target_channel)

        target_channel.send.assert_awaited_once()
        _, send_kwargs = target_channel.send.call_args
        embed = send_kwargs.get("embed")
        view = send_kwargs.get("view")

        assert isinstance(embed, discord.Embed)
        assert "Solicitud de Rol de Jugador" in (embed.title or "")
        assert isinstance(view, PanelPedirRolView)

        inter.response.send_message.assert_awaited_once_with(
            f"Panel publicado en {target_channel.mention}.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_publicar_panel_rol_success_to_current_channel(self) -> None:
        """Verifica que si no se indica canal, se publique en interaction.channel."""
        settings = Settings(staff_role_id=101)
        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        current_channel = make_mock_channel(channel_id=888, name="pedir-rol")
        inter = make_mock_interaction(user=staff_user, channel=current_channel)

        await cog.publicar_panel_rol.callback(cog, inter, canal=None)

        current_channel.send.assert_awaited_once()
        inter.response.send_message.assert_awaited_once_with(
            f"Panel publicado en {current_channel.mention}.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_asignar_rol_team_handles_http_exception(self) -> None:
        """Verifica que /asignar-rol capture discord.HTTPException al añadir rol."""
        settings = Settings(staff_role_id=101)
        team_role = make_mock_role(303, "Fnix Esports")
        guild = make_mock_guild()
        guild.roles = [team_role]

        target_user = make_mock_member(user_id=555)
        target_user.add_roles.side_effect = discord.HTTPException(
            MagicMock(status=500), "Internal Server Error"
        )

        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=guild)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Fnix Esports",
            nombre_lol="Player",
            riot_tag="123",
        )

        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.await_args[0][0]
        assert "Error al asignar el rol" in msg

    @pytest.mark.asyncio
    async def test_asignar_rol_handles_remove_roles_warning(self) -> None:
        """Verifica que si remove_roles falla, el flujo de /asignar-rol continúa."""
        settings = Settings(staff_role_id=101, sin_verificar_role_id=202)
        sin_verificar_role = make_mock_role(202, "Sin Verificar")
        team_role = make_mock_role(303, "Planar Shock Pingus")

        guild = make_mock_guild()
        guild.roles = [sin_verificar_role, team_role]

        target_user = make_mock_member(user_id=555, name="TargetMember", roles=[sin_verificar_role])
        target_user.remove_roles.side_effect = discord.Forbidden(
            MagicMock(status=403), "Cannot remove role"
        )

        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=guild)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Planar Shock Pingus",
            nombre_lol="Faker",
            riot_tag="KR1",
        )

        # Continúa con add_roles y responde
        target_user.add_roles.assert_awaited_once_with(team_role)
        inter.response.send_message.assert_awaited_once()
        assert "Planar Shock Pingus" in inter.response.send_message.await_args[0][0]

    @pytest.mark.asyncio
    async def test_asignar_rol_handles_edit_nick_warning(self) -> None:
        """Verifica que si actualizar el apodo falla, el comando responde positivamente."""
        settings = Settings(staff_role_id=101)
        team_role = make_mock_role(303, "Planar Shock Pingus")

        guild = make_mock_guild()
        guild.roles = [team_role]

        target_user = make_mock_member(user_id=555, name="TargetMember")
        target_user.edit.side_effect = discord.HTTPException(
            MagicMock(status=400), "Cannot change nick"
        )

        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=guild)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Planar Shock Pingus",
            nombre_lol="Faker",
            riot_tag="KR1",
        )

        inter.response.send_message.assert_awaited_once()
        assert "Planar Shock Pingus" in inter.response.send_message.await_args[0][0]

    @pytest.mark.asyncio
    async def test_asignar_rol_handles_db_exception_gracefully(self) -> None:
        """Verifica que si la BD lanza excepción, se captura y se confirma la asignación."""
        settings = Settings(staff_role_id=101)
        team_role = make_mock_role(303, "Planar Shock Pingus")

        guild = make_mock_guild()
        guild.roles = [team_role]

        target_user = make_mock_member(user_id=555, name="TargetMember")
        mock_role_service = MagicMock(spec=RoleService)
        mock_role_service.session_factory = MagicMock()

        bot = make_mock_bot(settings=settings, role_service=mock_role_service)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=guild)

        @asynccontextmanager
        async def failing_tx_session(factory=None):
            if False:
                yield
            raise RuntimeError("Database pool failure")

        with patch("liga_bot.cogs.roles.transactional_session", failing_tx_session):
            await cog.asignar_rol.callback(
                cog,
                inter,
                usuario=target_user,
                equipo="Planar Shock Pingus",
                nombre_lol="Faker",
                riot_tag="KR1",
            )

        inter.response.send_message.assert_awaited_once()
        assert "Planar Shock Pingus" in inter.response.send_message.await_args[0][0]

    @pytest.mark.asyncio
    async def test_publicar_panel_rol_invalid_channel(self) -> None:
        """Verifica error cuando el canal destino es None y no tiene send."""
        settings = Settings(staff_role_id=101)
        bot = make_mock_bot(settings=settings)
        cog = RolesCog(bot)

        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, channel=None)
        inter.channel = None

        await cog.publicar_panel_rol.callback(cog, inter, canal=None)

        inter.response.send_message.assert_awaited_once_with(
            "No se pudo determinar un canal válido para publicar el panel.", ephemeral=True
        )


# ===========================================================================
# 4. Verificación de Permisos Administrativos en ScheduleCog y TicketsCog
# ===========================================================================


class TestCrossCogAdminPermissions:
    """Valida los permisos default_permissions(manage_guild=True) en Schedule y Tickets."""

    def test_schedule_cog_commands_have_manage_guild_permission(self) -> None:
        """Verifica default_permissions en crear_partido, crear_jornada e importar_jornada."""
        bot = make_mock_bot()
        cog = ScheduleCog(bot)

        assert cog.crear_partido.default_permissions is not None
        assert cog.crear_partido.default_permissions.manage_guild is True

        assert cog.crear_jornada.default_permissions is not None
        assert cog.crear_jornada.default_permissions.manage_guild is True

        assert cog.importar_jornada.default_permissions is not None
        assert cog.importar_jornada.default_permissions.manage_guild is True

    def test_tickets_cog_commands_have_manage_guild_permission(self) -> None:
        """Verifica default_permissions en revisar_tickets y revisar_tickets_manual."""
        bot = make_mock_bot()
        cog = TicketsCog(bot, auto_start=False)

        assert cog.revisar_tickets.default_permissions is not None
        assert cog.revisar_tickets.default_permissions.manage_guild is True

        assert cog.revisar_tickets_manual.default_permissions is not None
        assert cog.revisar_tickets_manual.default_permissions.manage_guild is True

    @pytest.mark.asyncio
    async def test_revisar_tickets_manual_alias_executes_same_logic(self) -> None:
        """Verifica que /revisar-tickets-manual delegue en revisar_tickets."""
        bot = make_mock_bot()
        mock_ticket_service = MagicMock(spec=TicketService)
        mock_result = TicketAuditResult(
            categories_scanned=1,
            channels_scanned=5,
            alerts_sent=0,
            details=[],
        )
        mock_ticket_service.check_tickets = AsyncMock(return_value=mock_result)

        cog = TicketsCog(bot, ticket_service=mock_ticket_service, auto_start=False)
        staff_user = make_mock_member(roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user)

        await cog.revisar_tickets_manual.callback(cog, inter)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_ticket_service.check_tickets.assert_awaited_once_with(inter.guild)
        inter.followup.send.assert_awaited_once()
        embed = inter.followup.send.await_args.kwargs.get("embed")
        assert isinstance(embed, discord.Embed)
        assert "Auditoría de Inactividad de Tickets" in (embed.title or "")
