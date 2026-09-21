"""
Adversarial Empirical Challenge Suite for Milestone 5:
Empirical stress testing of RosterCog.gestionar_posicion slash command,
permission enforcement, and team query edge cases.

Vectors challenged:
1. Permission Stress:
   - Normal user (no admin, no manage_guild, no staff role): verify rejection with
     ephemeral message, defer never called, service never called.
   - User with only staff_role_id: authorized.
   - User with only administrator: authorized.
   - User with only manage_guild: authorized.
   - User with only ceo_role_id: authorized.
   - DM invocation (interaction.guild is None): rejected with guild-only message.
   - User resolution edge cases (discord.User fallback via get_member / fetch_member).
2. Roster Team Query Edge Cases:
   - Target member has 0 teams: verify informational embed sent, NO interactive view
     attached (view is None or omitted).
   - Target member has 1 team: verify GestionarPosicionView sent, auto-selected team
     (selected_team_id set and option default is True).
   - Target member has 4 teams: verify GestionarPosicionView sent with all 4 teams in
     dropdown, none auto-selected.
   - Database error during get_user_teams: verify graceful ephemeral error message to
     user, no unhandled exception escaping, no bot crash.
   - Missing roster_sync_service on bot: verify immediate graceful rejection before defer.
3. Real Database Integration:
   - End-to-end verification against real PGlite database executing actual SQL queries
     for 0, 1, and 4 teams.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.bot import LigaBot
from liga_bot.cogs.roster import RosterCog
from liga_bot.config import Settings
from liga_bot.models.enums import AppRole, Division, RosterRole
from liga_bot.models.roster import DiscordUser, Team, TeamMembership
from liga_bot.services.roster_sync_service import RosterSyncService
from liga_bot.ui.roster import GestionarPosicionView

# ---------------------------------------------------------------------------
# Test Helpers & Mocks
# ---------------------------------------------------------------------------


def make_role(role_id: int, name: str = "TestRole") -> MagicMock:
    """Crea un mock de discord.Role."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_member(
    user_id: int = 123456789,
    name: str = "TestUser",
    roles: list[discord.Role] | None = None,
    is_admin: bool = False,
    can_manage_guild: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Member con soporte para permisos y avatar."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])

    perms = MagicMock(spec=discord.Permissions)
    perms.administrator = is_admin
    perms.manage_guild = can_manage_guild
    member.guild_permissions = perms

    avatar = MagicMock()
    avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/avatar.png"
    member.display_avatar = avatar

    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.edit = AsyncMock()
    return member


def make_guild(
    guild_id: int = 1547725310508667010,
    name: str = "RCL League Server",
) -> MagicMock:
    """Crea un mock de discord.Guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    guild.roles = []
    guild.get_role = MagicMock(return_value=None)
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    return guild


def make_interaction(
    user: discord.Member | discord.User | None = None,
    guild: discord.Guild | None | object = ...,
) -> MagicMock:
    """Crea un mock de discord.Interaction."""
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user if user is not None else make_member()
    inter.guild = make_guild() if guild is ... else guild
    inter.channel = MagicMock(spec=discord.TextChannel)

    response = MagicMock(spec=discord.InteractionResponse)
    response.send_message = AsyncMock()
    response.defer = AsyncMock()
    inter.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    inter.followup = followup

    return inter


def make_team(
    name: str = "Team Alpha",
    tag: str = "ALP",
    slug: str = "team-alpha",
    division: Division = Division.PREMIER,
    discord_role_id: int = 1001,
) -> Team:
    """Crea un modelo Team en memoria."""
    return Team(
        id=uuid.uuid4(),
        name=name,
        tag=tag,
        slug=slug,
        division=division,
        discord_role_id=discord_role_id,
    )


def make_membership(
    team: Team,
    discord_user_id: str = "123456789",
    role: RosterRole = RosterRole.STAFF,
    is_captain: bool = False,
) -> TeamMembership:
    """Crea un modelo TeamMembership en memoria."""
    membership = TeamMembership(
        team_id=team.id,
        discord_user_id=discord_user_id,
        role=role,
        is_captain=is_captain,
    )
    membership.team = team
    return membership


def make_roster_sync_service() -> MagicMock:
    """Crea un mock de RosterSyncService."""
    service = MagicMock(spec=RosterSyncService)
    service.handle_role_added = AsyncMock(return_value=None)
    service.handle_role_removed = AsyncMock(return_value=True)
    service.get_user_teams = AsyncMock(return_value=[])
    service.change_player_position = AsyncMock()
    return service


def make_bot(
    settings: Settings | None = None,
    roster_sync_service: RosterSyncService | None = None,
) -> MagicMock:
    """Crea un mock de LigaBot."""
    bot = MagicMock(spec=LigaBot)
    bot.settings = settings or Settings(
        staff_role_id=901,
        ceo_role_id=902,
        sin_verificar_role_id=903,
    )
    bot.roster_sync_service = roster_sync_service
    bot.cogs = {}
    bot.add_cog = AsyncMock()
    return bot


# ===========================================================================
# Vector 1: Empirical Permission Stress Tests
# ===========================================================================


class TestEmpiricalPermissionStress:
    """Pruebas empíricas adversariales de seguridad y control de acceso a /gestionar-posicion."""

    @pytest.mark.asyncio
    async def test_permission_rejected_normal_user_no_privileges(self) -> None:
        """
        Usuario estándar sin roles de staff ni permisos administrativos:
        - Reclamación: El bot debe rechazar de inmediato con mensaje efímero.
        - Verificación: interaction.response.defer NUNCA llamado.
        - Verificación: service.get_user_teams NUNCA llamado.
        - Verificación: interaction.followup.send NUNCA llamado.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        unprivileged_user = make_member(
            user_id=10001,
            name="NormalGuy",
            roles=[],
            is_admin=False,
            can_manage_guild=False,
        )
        target_member = make_member(user_id=20001, name="TargetGuy")
        inter = make_interaction(user=unprivileged_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        # 1. Mensaje efímero de rechazo
        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.await_args
        msg = args[0] if args else kwargs.get("content", "")
        assert kwargs.get("ephemeral") is True
        assert any(term in str(msg).lower() for term in ("staff", "autorización", "permiso"))

        # 2. Defensas perimetrales estrictas: no defer, no DB service, no followup
        inter.response.defer.assert_not_awaited()
        service.get_user_teams.assert_not_awaited()
        inter.followup.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_permission_rejected_user_with_arbitrary_unauthorized_roles(self) -> None:
        """
        Usuario con roles genéricos del servidor (ej. Jugador, Capitán, Viewer), pero sin
        staff_role_id, ceo_role_id, administrator ni manage_guild:
        - Debe ser rechazado categóricamente.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        role_viewer = make_role(501, "Viewer")
        role_player = make_role(502, "Player")
        role_captain = make_role(503, "Captain")

        member_with_roles = make_member(
            user_id=10002,
            name="GenericPlayer",
            roles=[role_viewer, role_player, role_captain],
            is_admin=False,
            can_manage_guild=False,
        )
        target_member = make_member(user_id=20002)
        inter = make_interaction(user=member_with_roles)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.send_message.assert_awaited_once()
        kwargs = inter.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        inter.response.defer.assert_not_awaited()
        service.get_user_teams.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_permission_authorized_with_only_staff_role(self) -> None:
        """
        Usuario que posee ÚNICAMENTE el staff_role_id configurado:
        - No tiene administrator ni manage_guild.
        - Debe ser autorizado exitosamente.
        - interaction.response.defer(ephemeral=True) llamado.
        - service.get_user_teams llamado con el target_member id.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        staff_role = make_role(bot.settings.staff_role_id, "Official Staff")
        staff_user = make_member(
            user_id=10003,
            roles=[staff_role],
            is_admin=False,
            can_manage_guild=False,
        )
        target_member = make_member(user_id=20003)
        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        service.get_user_teams.assert_awaited_once_with(str(target_member.id))

    @pytest.mark.asyncio
    async def test_permission_authorized_with_only_administrator_flag(self) -> None:
        """
        Usuario que posee ÚNICAMENTE el permiso nativo administrator:
        - Sin roles en Discord (roles=[]).
        - can_manage_guild=False.
        - Debe ser autorizado exitosamente.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        admin_user = make_member(
            user_id=10004,
            roles=[],
            is_admin=True,
            can_manage_guild=False,
        )
        target_member = make_member(user_id=20004)
        inter = make_interaction(user=admin_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        service.get_user_teams.assert_awaited_once_with(str(target_member.id))

    @pytest.mark.asyncio
    async def test_permission_authorized_with_only_manage_guild_flag(self) -> None:
        """
        Usuario que posee ÚNICAMENTE el permiso nativo manage_guild:
        - Sin roles en Discord (roles=[]).
        - is_admin=False.
        - Debe ser autorizado exitosamente (cumple default_permissions y is_staff).
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        mod_user = make_member(
            user_id=10005,
            roles=[],
            is_admin=False,
            can_manage_guild=True,
        )
        target_member = make_member(user_id=20005)
        inter = make_interaction(user=mod_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        service.get_user_teams.assert_awaited_once_with(str(target_member.id))

    @pytest.mark.asyncio
    async def test_permission_authorized_with_only_ceo_role(self) -> None:
        """
        Usuario que posee ÚNICAMENTE el ceo_role_id configurado:
        - Sin permisos nativos de admin ni manage_guild.
        - Debe ser admitido conforme a las reglas de gobernanza de la liga.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        ceo_role = make_role(bot.settings.ceo_role_id, "League CEO")
        ceo_user = make_member(
            user_id=10006,
            roles=[ceo_role],
            is_admin=False,
            can_manage_guild=False,
        )
        target_member = make_member(user_id=20006)
        inter = make_interaction(user=ceo_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        service.get_user_teams.assert_awaited_once_with(str(target_member.id))

    @pytest.mark.asyncio
    async def test_permission_rejected_in_direct_message_context(self) -> None:
        """
        Invocación por Mensaje Directo (interaction.guild is None):
        - Aunque el usuario sea un administrador global o posea rol de staff.
        - Debe ser rechazado inmediatamente con mensaje efímero de servidor requerido.
        - interaction.response.defer NUNCA llamado.
        - service.get_user_teams NUNCA llamado.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        admin_in_dm = make_member(user_id=10007, is_admin=True)
        target_member = make_member(user_id=20007)
        inter = make_interaction(user=admin_in_dm, guild=None)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.await_args
        msg = args[0] if args else kwargs.get("content", "")
        assert kwargs.get("ephemeral") is True
        assert "servidor" in str(msg).lower()

        inter.response.defer.assert_not_awaited()
        service.get_user_teams.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_permission_raw_discord_user_fails_safe_and_rejects(self) -> None:
        """
        Prueba de frontera: interaction.user es una instancia de discord.User pura
        (sin roles ni guild_permissions, caso típico si se perdiera el contexto de miembro):
        - is_staff(interaction.user) evalúa de forma fail-safe a False.
        - Se rechaza inmediatamente con mensaje efímero.
        - Defer y service.get_user_teams NUNCA son llamados.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        raw_user = MagicMock(spec=discord.User)
        raw_user.id = 998877
        raw_user.name = "RawUser"

        inter = make_interaction(user=raw_user)
        target_member = make_member(user_id=20008)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.send_message.assert_awaited_once()
        kwargs = inter.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        inter.response.defer.assert_not_awaited()
        service.get_user_teams.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_target_member_resolution_fallback_for_raw_user(self) -> None:
        """
        Prueba de frontera para el parámetro target_member:
        - Si member es un discord.User puro (ej. mención no resuelta en caché de guild),
          resolve_member(member) retorna None y se activa el fallback 'or member'.
        - La extracción str(target_member.id) debe funcionar correctamente.
        - service.get_user_teams se invoca con el ID del target_member.
        """
        service = make_roster_sync_service()
        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        staff_role = make_role(bot.settings.staff_role_id, "Staff")
        staff_user = make_member(user_id=10008, roles=[staff_role])

        raw_target = MagicMock(spec=discord.User)
        raw_target.id = 20009
        raw_target.name = "RawTargetUser"
        raw_target.display_name = "RawTargetUser"
        raw_target.mention = f"<@{raw_target.id}>"

        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, raw_target)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        service.get_user_teams.assert_awaited_once_with(str(raw_target.id))


# ===========================================================================
# Vector 2: Empirical Roster Team Query Edge Cases
# ===========================================================================


class TestEmpiricalRosterTeamQueryEdgeCases:
    """Pruebas empíricas adversariales de las respuestas de consulta de equipos."""

    @pytest.mark.asyncio
    async def test_team_query_zero_teams_sends_informational_embed_without_view(self) -> None:
        """
        Caso de borde 1: Miembro objetivo con 0 equipos en la base de datos.
        - Debe diferir efímeramente.
        - Debe consultar get_user_teams(target_member.id).
        - Debe enviar un embed de aviso informativo.
        - CONDICIÓN CRÍTICA DE VERIFICACIÓN: NO se debe adjuntar ninguna vista interactiva
          (view debe ser None o no estar presente en kwargs).
        - Los botones o selectores nunca deben estar expuestos para un usuario sin equipos.
        """
        service = make_roster_sync_service()
        service.get_user_teams.return_value = []

        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        staff_user = make_member(
            user_id=10010,
            roles=[make_role(bot.settings.staff_role_id)],
        )
        target_member = make_member(user_id=30001, name="LonePlayer")
        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        service.get_user_teams.assert_awaited_once_with(str(target_member.id))
        inter.followup.send.assert_awaited_once()

        kwargs = inter.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True

        # Verificación adversarial: vista AUSENTE
        attached_view = kwargs.get("view")
        assert attached_view is None, (
            f"Violación de especificación: Se adjuntó una vista interactiva {attached_view} "
            "cuando el usuario no tiene equipos registrados."
        )

        # Verificación del embed
        assert "embed" in kwargs
        embed = kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert embed.title == "🛡️ Sin Equipos Registrados"
        assert target_member.mention in embed.description
        assert target_member.display_name in embed.description
        assert embed.color == discord.Color.orange()
        assert embed.footer.text == "RCL League • Sincronización de Plantillas"

    @pytest.mark.asyncio
    async def test_team_query_single_team_auto_selects_team_in_view(self) -> None:
        """
        Caso de borde 2: Miembro objetivo pertenece exactamente a 1 equipo.
        - Debe generar GestionarPosicionView.
        - CONDICIÓN CRÍTICA DE VERIFICACIÓN: view.selected_team_id DEBE estar auto-seleccionado
          con el ID de ese único equipo (no debe ser None).
        - El selector de equipos (team_select) debe contener exactamente 1 opción y default=True.
        - Initial embed debe mostrar "Equipos Pertenecientes: 1".
        """
        team1 = make_team(name="Planar Shock Pingus", tag="PSP")
        membership1 = make_membership(team=team1, role=RosterRole.MID)
        user_teams = [(team1, membership1)]

        service = make_roster_sync_service()
        service.get_user_teams.return_value = user_teams

        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        staff_user = make_member(
            user_id=10011,
            roles=[make_role(bot.settings.staff_role_id)],
        )
        target_member = make_member(user_id=30002, name="Faker")
        inter = make_interaction(user=staff_user)

        sent_msg_mock = MagicMock(spec=discord.Message)
        inter.followup.send.return_value = sent_msg_mock

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True

        # Verificación de la vista
        assert "view" in kwargs
        view = kwargs["view"]
        assert isinstance(view, GestionarPosicionView)
        assert view.message == sent_msg_mock
        assert view.member == target_member
        assert view.actor == staff_user

        # Auto-selección obligatoria para 1 equipo
        assert view.selected_team_id == team1.id, (
            f"Fallo de auto-selección: view.selected_team_id ({view.selected_team_id}) "
            f"no coincide con el ID del único equipo ({team1.id})."
        )

        # Verificación del selector de equipo subordinado
        assert len(view.team_select.options) == 1
        opt = view.team_select.options[0]
        assert opt.value == str(team1.id)
        assert opt.default is True
        assert "PSP" in opt.label

        # Embed inicial
        embed = kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        field_equipos = next(
            (f for f in embed.fields if f.name == "🛡️ Equipos Pertenecientes"), None
        )
        assert field_equipos is not None
        assert field_equipos.value == "1"

    @pytest.mark.asyncio
    async def test_team_query_four_teams_all_options_present_none_auto_selected(self) -> None:
        """
        Caso de borde 3: Miembro objetivo pertenece a 4 equipos (multi-club).
        - Debe generar GestionarPosicionView.
        - CONDICIÓN CRÍTICA DE VERIFICACIÓN: view.selected_team_id DEBE ser None
          (exige al usuario seleccionar explícitamente en el menú desplegable).
        - El selector de equipos (team_select) debe contener exactamente 4 opciones.
        - Ninguna opción debe tener default=True inicialmente.
        - Cada opción debe tener el valor str(team.id) correspondiente.
        - Initial embed debe mostrar "Equipos Pertenecientes: 4".
        """
        t1 = make_team(name="Team Alpha", tag="ALP", discord_role_id=2001)
        t2 = make_team(name="Team Beta", tag="BET", discord_role_id=2002)
        t3 = make_team(name="Team Gamma", tag="GAM", discord_role_id=2003)
        t4 = make_team(name="Team Delta", tag="DEL", discord_role_id=2004)

        m1 = make_membership(team=t1, role=RosterRole.MID, is_captain=True)
        m2 = make_membership(team=t2, role=RosterRole.COACH)
        m3 = make_membership(team=t3, role=RosterRole.STAFF)
        m4 = make_membership(team=t4, role=RosterRole.PARTNERS)

        user_teams = [(t1, m1), (t2, m2), (t3, m3), (t4, m4)]

        service = make_roster_sync_service()
        service.get_user_teams.return_value = user_teams

        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        staff_user = make_member(
            user_id=10012,
            roles=[make_role(bot.settings.staff_role_id)],
        )
        target_member = make_member(user_id=30003, name="MultiTeamGuy")
        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.await_args.kwargs
        view = kwargs["view"]
        assert isinstance(view, GestionarPosicionView)

        # Para > 1 equipo, selected_team_id debe ser None
        assert view.selected_team_id is None, (
            "Violación de UX: En un usuario con 4 equipos, selected_team_id "
            f"fue precargado indebidamente como {view.selected_team_id} en vez de None."
        )

        # Verificación exhaustiva de las 4 opciones en el dropdown
        assert len(view.team_select.options) == 4
        option_values = [opt.value for opt in view.team_select.options]
        expected_values = [str(t1.id), str(t2.id), str(t3.id), str(t4.id)]
        assert option_values == expected_values

        for opt in view.team_select.options:
            assert opt.default is False

        # Verificación de que la descripción refleja la membresía (ej. Capitán en m1)
        assert "Capitán" in view.team_select.options[0].description
        assert "coach" in view.team_select.options[1].description.lower()
        assert "staff" in view.team_select.options[2].description.lower()
        assert "partners" in view.team_select.options[3].description.lower()

        # Embed inicial
        embed = kwargs["embed"]
        field_equipos = next(
            (f for f in embed.fields if f.name == "🛡️ Equipos Pertenecientes"), None
        )
        assert field_equipos is not None
        assert field_equipos.value == "4"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "simulated_exception",
        [
            RuntimeError("PostgreSQL connection terminated unexpectedly"),
            TimeoutError("Query execution timed out after 5000ms"),
            OSError("Network unreachable / socket reset by peer"),
            Exception("Generic database driver crash"),
        ],
    )
    async def test_team_query_database_error_gracefully_handled(
        self,
        simulated_exception: Exception,
    ) -> None:
        """
        Caso de borde 4: get_user_teams lanza excepciones diversas en la capa de datos.
        - El comando DEBE capturar la excepción.
        - NUNCA debe propagar la excepción hacia el despachador de discord.py (evita bot crash).
        - DEBE notificar al usuario con un mensaje de error amigable y efímero vía followup.send.
        - NO debe dejar la interacción colgada sin respuesta.
        """
        service = make_roster_sync_service()
        service.get_user_teams.side_effect = simulated_exception

        bot = make_bot(roster_sync_service=service)
        cog = RosterCog(bot)

        staff_user = make_member(
            user_id=10013,
            roles=[make_role(bot.settings.staff_role_id)],
        )
        target_member = make_member(user_id=30004)
        inter = make_interaction(user=staff_user)

        # Invocación segura sin que se escape la excepción
        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        inter.followup.send.assert_awaited_once()

        args, kwargs = inter.followup.send.await_args
        msg = args[0] if args else kwargs.get("content", "")
        assert kwargs.get("ephemeral") is True
        assert "error" in str(msg).lower()

    @pytest.mark.asyncio
    async def test_team_query_missing_service_fails_gracefully_before_defer(self) -> None:
        """
        Caso de borde 5: bot.roster_sync_service es None (servicio no registrado / caída).
        - Debe rechazar inmediatamente con interaction.response.send_message.
        - NUNCA debe intentar diferir ni invocar un servicio nulo.
        """
        bot = make_bot(roster_sync_service=None)
        cog = RosterCog(bot)

        staff_user = make_member(
            user_id=10014,
            roles=[make_role(bot.settings.staff_role_id)],
        )
        target_member = make_member(user_id=30005)
        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.await_args
        msg = args[0] if args else kwargs.get("content", "")
        assert kwargs.get("ephemeral") is True
        assert any(term in str(msg).lower() for term in ("servicio", "disponible"))

        inter.response.defer.assert_not_awaited()
        inter.followup.send.assert_not_awaited()


# ===========================================================================
# Vector 3: Real Database Integration (PGlite Empirical Validation)
# ===========================================================================


class TestEmpiricalDatabaseRosterCommandIntegration:
    """Pruebas empíricas de integración con base de datos real (PGlite en memoria)."""

    @pytest.fixture
    def session_factory(self, migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(bind=migrated_db, expire_on_commit=False)

    @pytest_asyncio.fixture(autouse=True)
    async def clean_database(self, migrated_db: AsyncEngine) -> AsyncGenerator[None, None]:
        async with migrated_db.connect() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                    "players, teams, discord_users CASCADE;"
                )
            )
            await conn.commit()
        yield
        async with migrated_db.connect() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                    "players, teams, discord_users CASCADE;"
                )
            )
            await conn.commit()

    @pytest.mark.asyncio
    async def test_e2e_gestionar_posicion_with_real_database_zero_teams(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        Integración real:
        - Usuario registrado en discord_users sin membresías en team_memberships.
        - RosterSyncService real ejecutando consulta SQL en PGlite.
        - RosterCog debe procesar el resultado real y emitir embed informativo sin vista.
        """
        real_settings = Settings(staff_role_id=8881)
        real_service = RosterSyncService(session_factory=session_factory, settings=real_settings)

        bot = make_bot(settings=real_settings, roster_sync_service=real_service)
        cog = RosterCog(bot)

        # Sembrar usuario en BD
        user_id = 40001
        async with session_factory() as session:
            db_user = DiscordUser(
                discord_id=str(user_id),
                username="ZeroTeamDbUser",
                role=AppRole.VIEWER,
            )
            session.add(db_user)
            await session.commit()

        staff_user = make_member(user_id=88810, roles=[make_role(real_settings.staff_role_id)])
        target_member = make_member(user_id=user_id, name="ZeroTeamDbUser")
        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        assert kwargs.get("view") is None
        assert "embed" in kwargs
        assert kwargs["embed"].title == "🛡️ Sin Equipos Registrados"

    @pytest.mark.asyncio
    async def test_e2e_gestionar_posicion_with_real_database_one_team(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        Integración real:
        - Usuario sembrado con 1 membresía real en PGlite.
        - RosterSyncService realiza join y carga eager de Team.
        - RosterCog crea GestionarPosicionView con selected_team_id auto-seleccionado.
        """
        real_settings = Settings(staff_role_id=8882)
        real_service = RosterSyncService(session_factory=session_factory, settings=real_settings)

        bot = make_bot(settings=real_settings, roster_sync_service=real_service)
        cog = RosterCog(bot)

        user_id = 40002
        async with session_factory() as session:
            db_user = DiscordUser(discord_id=str(user_id), username="SoloPlayer")
            t = Team(
                name="Real PGlite Team",
                tag="RPT",
                slug="real-pglite-team",
                division=Division.PREMIER,
                discord_role_id=5501,
            )
            session.add_all([db_user, t])
            await session.flush()

            mem = TeamMembership(
                team_id=t.id,
                discord_user_id=str(user_id),
                role=RosterRole.MID,
                is_captain=True,
            )
            session.add(mem)
            await session.commit()
            team_id = t.id

        staff_user = make_member(user_id=88820, roles=[make_role(real_settings.staff_role_id)])
        target_member = make_member(user_id=user_id, name="SoloPlayer")
        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.await_args.kwargs
        view = kwargs.get("view")
        assert isinstance(view, GestionarPosicionView)
        assert view.selected_team_id == team_id
        assert len(view.team_select.options) == 1
        assert view.team_select.options[0].default is True
        assert "RPT" in view.team_select.options[0].label

    @pytest.mark.asyncio
    async def test_e2e_gestionar_posicion_with_real_database_four_teams(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        Integración real:
        - Usuario sembrado con 4 membresías reales en 4 equipos diferentes en PGlite.
        - RosterSyncService ejecuta la consulta con selectinload(TeamMembership.team).
        - RosterCog crea GestionarPosicionView con 4 opciones y selected_team_id=None.
        """
        real_settings = Settings(staff_role_id=8883)
        real_service = RosterSyncService(session_factory=session_factory, settings=real_settings)

        bot = make_bot(settings=real_settings, roster_sync_service=real_service)
        cog = RosterCog(bot)

        user_id = 40003
        expected_team_ids: set[uuid.UUID] = set()

        async with session_factory() as session:
            db_user = DiscordUser(discord_id=str(user_id), username="QuadPlayer")
            session.add(db_user)

            teams = [
                Team(
                    name=f"Club {i}",
                    tag=f"C{i}",
                    slug=f"club-{i}",
                    division=Division.PREMIER if i % 2 == 0 else Division.ASCEND,
                    discord_role_id=6000 + i,
                )
                for i in range(1, 5)
            ]
            session.add_all(teams)
            await session.flush()

            roles = [RosterRole.ADC, RosterRole.COACH, RosterRole.STAFF, RosterRole.PARTNERS]
            for team, role in zip(teams, roles, strict=True):
                mem = TeamMembership(
                    team_id=team.id,
                    discord_user_id=str(user_id),
                    role=role,
                    is_captain=(role == RosterRole.ADC),
                )
                session.add(mem)
                expected_team_ids.add(team.id)

            await session.commit()

        staff_user = make_member(user_id=88830, roles=[make_role(real_settings.staff_role_id)])
        target_member = make_member(user_id=user_id, name="QuadPlayer")
        inter = make_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.await_args.kwargs
        view = kwargs.get("view")
        assert isinstance(view, GestionarPosicionView)
        assert view.selected_team_id is None
        assert len(view.team_select.options) == 4

        actual_option_ids = {uuid.UUID(opt.value) for opt in view.team_select.options}
        assert actual_option_ids == expected_team_ids
