"""
Pruebas unitarias para RolesCog, su ciclo de vida, comandos slash y verificación de
permisos administrativos (@app_commands.default_permissions) en ScheduleCog y TicketsCog.

Cubre:
1. RolesCog:
   - Registro de vistas persistentes y dynamic items en cog_load().
   - Listener on_member_join y delegación a RoleService.
   - /pedir-rol: apertura de SolicitudRolModal.
   - /asignar-rol: comprobación de staff, regla de no autoasignación, defer previo y
     delegación en RoleService (assign_free_role / assign_team_role).
   - /publicar-panel-rol: comprobación de staff, publicación en canal destino y embed.
   - Idempotencia de la función setup().
2. Permisos administrativos (@app_commands.default_permissions(manage_guild=True)):
   - ScheduleCog: crear_partido, crear_jornada, importar_jornada.
   - TicketsCog: revisar_tickets, revisar_tickets_manual.
   - Alias /revisar-tickets-manual delegando en revisar_tickets.
3. DEFAULT_EXTENSIONS en bot.py incluye 'liga_bot.cogs.roles'.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from liga_bot.bot import DEFAULT_EXTENSIONS, LigaBot
from liga_bot.cogs.roles import RolesCog
from liga_bot.cogs.roles import setup as roles_setup
from liga_bot.cogs.schedule import ScheduleCog
from liga_bot.cogs.tickets import TicketsCog
from liga_bot.config import Settings
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
        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        inter.followup.send.assert_awaited_once_with(
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
    async def test_asignar_rol_rechaza_asignarse_a_si_mismo(self) -> None:
        """El staff no puede usar /asignar-rol sobre sí mismo, ni con equipo ni con Libre."""
        mock_service = MagicMock(spec=RoleService)
        mock_service.assign_team_role = AsyncMock()
        mock_service.assign_free_role = AsyncMock()
        settings = Settings(staff_role_id=101, free_role_name="Libre")
        bot = make_mock_bot(settings=settings, role_service=mock_service)
        cog = RolesCog(bot)

        staff_user = make_mock_member(user_id=111, roles=[make_mock_role(101, "Staff")])
        for equipo in ("Planar Shock Pingus", "Libre"):
            inter = make_mock_interaction(user=staff_user)
            await cog.asignar_rol.callback(
                cog,
                inter,
                usuario=staff_user,
                equipo=equipo,
                nombre_lol="Yo",
                riot_tag="EUW",
                posicion="mid",
            )
            inter.response.send_message.assert_awaited_once_with(
                "No puedes asignarte un rol a ti mismo.", ephemeral=True
            )
            inter.response.defer.assert_not_called()

        mock_service.assign_team_role.assert_not_called()
        mock_service.assign_free_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_asignar_rol_equipo_difiere_antes_de_delegar_en_el_servicio(self) -> None:
        """La interacción se difiere antes de que el servicio toque Discord o la base de datos."""
        orden: list[str] = []
        mock_service = MagicMock(spec=RoleService)

        async def fake_assign(**_kwargs: object) -> tuple[bool, str]:
            orden.append("servicio")
            return True, "Rol Planar Shock Pingus asignado a TargetMember (mid)."

        mock_service.assign_team_role = AsyncMock(side_effect=fake_assign)
        settings = Settings(staff_role_id=101, free_role_name="Libre")
        bot = make_mock_bot(settings=settings, role_service=mock_service)
        cog = RolesCog(bot)

        staff_user = make_mock_member(user_id=111, roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user)
        inter.response.defer.side_effect = lambda **_kw: orden.append("defer")
        target_user = make_mock_member(user_id=555, name="TargetMember")

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Planar Shock Pingus",
            nombre_lol="Faker",
            riot_tag="KR1",
            posicion="mid",
        )

        assert orden == ["defer", "servicio"]
        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_service.assign_team_role.assert_awaited_once_with(
            guild=inter.guild,
            member=target_user,
            staff_member=staff_user,
            equipo="Planar Shock Pingus",
            nombre_lol="Faker",
            riot_tag="KR1",
            posicion="mid",
        )
        inter.followup.send.assert_awaited_once_with(
            "Rol Planar Shock Pingus asignado a TargetMember (mid).", ephemeral=True
        )
        inter.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_asignar_rol_equipo_no_busca_roles_por_nombre(self) -> None:
        """Un rol del servidor que no es de un equipo registrado no se asigna desde el cog."""
        mock_service = MagicMock(spec=RoleService)
        mock_service.assign_team_role = AsyncMock(
            return_value=(False, "El equipo 'Administración' no está registrado.")
        )
        settings = Settings(staff_role_id=101, free_role_name="Libre")
        bot = make_mock_bot(settings=settings, role_service=mock_service)
        cog = RolesCog(bot)

        admin_role = make_mock_role(999, "Administración")
        guild = make_mock_guild()
        guild.roles = [admin_role]
        staff_user = make_mock_member(user_id=111, roles=[make_mock_role(101, "Staff")])
        inter = make_mock_interaction(user=staff_user, guild=guild)
        target_user = make_mock_member(user_id=555)

        await cog.asignar_rol.callback(
            cog,
            inter,
            usuario=target_user,
            equipo="Administración",
            nombre_lol="Faker",
            riot_tag="KR1",
            posicion="mid",
        )

        target_user.add_roles.assert_not_called()
        inter.followup.send.assert_awaited_once_with(
            "El equipo 'Administración' no está registrado.", ephemeral=True
        )

    def test_asignar_rol_posicion_ofrece_las_opciones_del_ticket(self) -> None:
        """El parámetro posicion tiene las mismas opciones que el desplegable del ticket."""
        from liga_bot.ui.roles import POSICION_DESCRIPCIONES

        bot = make_mock_bot()
        cog = RolesCog(bot)
        param = next(p for p in cog.asignar_rol.parameters if p.name == "posicion")
        assert [c.value for c in param.choices] == [p.value for p in POSICION_DESCRIPCIONES]

    @pytest.mark.asyncio
    async def test_asignar_rol_autocompleta_equipos_registrados_y_libre(self) -> None:
        """El autocompletado sugiere equipos registrados y Libre, filtrando por el texto."""
        mock_service = MagicMock(spec=RoleService)
        mock_service.list_team_names = AsyncMock(
            return_value=["Planar Shock Pingus", "Storm Legion", "X" * 101]
        )
        settings = Settings(staff_role_id=101, free_role_name="Libre")
        bot = make_mock_bot(settings=settings, role_service=mock_service)
        cog = RolesCog(bot)
        inter = make_mock_interaction()

        todos = await cog.asignar_rol_equipo_autocomplete(inter, "")
        filtrados = await cog.asignar_rol_equipo_autocomplete(inter, "storm")

        assert [c.value for c in todos] == ["Planar Shock Pingus", "Storm Legion", "Libre"]
        assert [c.value for c in filtrados] == ["Storm Legion"]

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
        assert "Rebel Crown Legacy" in (embed.title or "")
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
