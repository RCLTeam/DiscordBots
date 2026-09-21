"""
Pruebas unitarias para RosterCog, listener on_member_update, slash command /gestionar-posicion
y registro del ciclo de vida de extensiones en LigaBot.

Cubre:
1. TestRosterCogLifecycle:
   - Inicialización con dependencias y propiedades resilientes.
   - Idempotencia y registro mediante setup(bot).
   - Presencia en DEFAULT_EXTENSIONS (bot.py).
   - Re-exportación mediante alias roster_cog.py.
2. TestRosterCogListeners:
   - Detección precisa de roles añadidos y delegación en handle_role_added.
   - Detección precisa de roles retirados y delegación en handle_role_removed.
   - No-op ante actualizaciones sin alteración de roles (apodo, avatar).
   - Secuencialidad ante múltiples roles añadidos y eliminados simultáneamente.
   - Resiliencia ante excepciones por rol y ausencia de roster_sync_service.
3. TestRosterCogCommands:
   - Metadatos de seguridad: default_permissions(manage_guild=True).
   - Doble cerrojo: rechazo estricto a no-staff y admisión a staff/admin/ceo.
   - Flujo exitoso con equipos: deferral efímero, instanciación de GestionarPosicionView.
   - Flujo de aviso sin equipos: embed informativo de aviso sin vista interactiva.
   - Manejo elegante de servicio no disponible, excepciones en base de datos y DM.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from liga_bot.bot import DEFAULT_EXTENSIONS, LigaBot
from liga_bot.cogs.roster import RosterCog, setup
from liga_bot.cogs.roster_cog import RosterCog as AliasRosterCog
from liga_bot.cogs.roster_cog import setup as alias_setup
from liga_bot.config import Settings
from liga_bot.models.enums import Division, RosterRole
from liga_bot.models.roster import Team, TeamMembership
from liga_bot.services.roster_sync_service import RosterSyncService
from liga_bot.ui.roster import GestionarPosicionView

# ---------------------------------------------------------------------------
# Mocks Auxiliares
# ---------------------------------------------------------------------------


def make_mock_role(role_id: int, name: str = "Rol") -> MagicMock:
    """Crea un mock estricto de discord.Role."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_mock_member(
    user_id: int = 123456789,
    name: str = "TestUser",
    roles: list[discord.Role] | None = None,
    is_admin: bool = False,
    can_manage_guild: bool = False,
) -> MagicMock:
    """Crea un mock estricto de discord.Member con soporte para permisos y avatar."""
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


def make_mock_guild(
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


def make_mock_interaction(
    user: discord.Member | None = None,
    guild: discord.Guild | None | object = ...,
) -> MagicMock:
    """Crea un mock de discord.Interaction con response y followup asíncronos."""
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user or make_mock_member()
    inter.guild = make_mock_guild() if guild is ... else guild
    inter.channel = MagicMock(spec=discord.TextChannel)

    response = MagicMock(spec=discord.InteractionResponse)
    response.send_message = AsyncMock()
    response.defer = AsyncMock()
    inter.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    inter.followup = followup

    return inter


def make_mock_team(
    team_id: uuid.UUID | None = None,
    name: str = "Team Alpha",
    tag: str = "ALP",
    slug: str = "team-alpha",
    division: Division = Division.PREMIER,
    discord_role_id: int = 1001,
) -> Team:
    """Crea una instancia de modelo Team en memoria."""
    return Team(
        id=team_id or uuid.uuid4(),
        name=name,
        tag=tag,
        slug=slug,
        division=division,
        discord_role_id=discord_role_id,
    )


def make_mock_membership(
    team: Team,
    discord_user_id: str = "123456789",
    role: RosterRole = RosterRole.STAFF,
    is_captain: bool = False,
) -> TeamMembership:
    """Crea una instancia de modelo TeamMembership en memoria."""
    membership = TeamMembership(
        team_id=team.id,
        discord_user_id=discord_user_id,
        role=role,
        is_captain=is_captain,
    )
    membership.team = team
    return membership


def make_mock_roster_sync_service() -> MagicMock:
    """Crea un mock asíncrono para RosterSyncService."""
    service = MagicMock(spec=RosterSyncService)
    service.handle_role_added = AsyncMock(return_value=None)
    service.handle_role_removed = AsyncMock(return_value=True)
    service.get_user_teams = AsyncMock(return_value=[])
    service.change_player_position = AsyncMock()
    return service


def make_mock_bot(
    settings: Settings | None = None,
    roster_sync_service: RosterSyncService | None = None,
) -> MagicMock:
    """Crea un mock de LigaBot."""
    bot = MagicMock(spec=LigaBot)
    bot.settings = settings or Settings(
        staff_role_id=101,
        ceo_role_id=102,
        sin_verificar_role_id=202,
    )
    bot.roster_sync_service = roster_sync_service
    bot.cogs = {}
    bot.add_cog = AsyncMock()
    return bot


# ===========================================================================
# 1. Ciclo de Vida y Extensiones de RosterCog
# ===========================================================================


class TestRosterCogLifecycle:
    """Pruebas unitarias para inicialización, carga idempotente y extensiones."""

    def test_roster_cog_init(self) -> None:
        """Verifica que RosterCog se inicialice guardando la referencia al bot y propiedades."""
        mock_service = make_mock_roster_sync_service()
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        assert cog.bot is bot
        assert cog.settings is bot.settings
        assert cog.roster_sync_service is mock_service

    def test_roster_cog_init_with_explicit_settings(self) -> None:
        """Verifica que RosterCog priorice settings explícitos si se proporcionan."""
        bot = make_mock_bot()
        custom_settings = Settings(staff_role_id=999)
        cog = RosterCog(bot, settings=custom_settings)

        assert cog.settings is custom_settings

    @pytest.mark.asyncio
    async def test_setup_adds_cog(self) -> None:
        """Verifica que setup() añada el Cog Roster si no está previamente cargado."""
        bot = make_mock_bot()
        bot.cogs = {}

        await setup(bot)

        bot.add_cog.assert_awaited_once()
        added_cog = bot.add_cog.await_args[0][0]
        assert isinstance(added_cog, RosterCog)
        assert added_cog.bot is bot

    @pytest.mark.asyncio
    async def test_setup_idempotency(self) -> None:
        """Verifica que setup() sea idempotente y no duplique el Cog si ya existe."""
        bot = make_mock_bot()
        existing_cog = RosterCog(bot)
        bot.cogs = {"Roster": existing_cog}

        await setup(bot)

        bot.add_cog.assert_not_awaited()

    def test_bot_default_extensions_contains_roster_cog(self) -> None:
        """Verifica que DEFAULT_EXTENSIONS en bot.py incluya 'liga_bot.cogs.roster'."""
        assert "liga_bot.cogs.roster" in DEFAULT_EXTENSIONS

    def test_roster_cog_alias_export(self) -> None:
        """Verifica que el alias roster_cog.py re-exporte RosterCog y setup correctamente."""
        assert AliasRosterCog is RosterCog
        assert alias_setup is setup


# ===========================================================================
# 2. Event Listener: on_member_update
# ===========================================================================


class TestRosterCogListeners:
    """Pruebas unitarias para la sincronización automática de roles en on_member_update."""

    @pytest.mark.asyncio
    async def test_on_member_update_role_added(self) -> None:
        """Verifica que la incorporación de un rol de equipo invoque handle_role_added."""
        mock_service = make_mock_roster_sync_service()
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = make_mock_role(100, "Miembro")
        team_role = make_mock_role(1001, "Planar Shock Pingus")

        before = make_mock_member(user_id=123, roles=[base_role])
        after = make_mock_member(user_id=123, roles=[base_role, team_role])

        await cog.on_member_update(before, after)

        mock_service.handle_role_added.assert_awaited_once()
        args, kwargs = mock_service.handle_role_added.await_args
        called_member = kwargs.get("member") or (args[0] if args else None)
        called_role = kwargs.get("role") or (args[1] if len(args) > 1 else None)
        assert called_member == after
        assert called_role == team_role
        mock_service.handle_role_removed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_member_update_role_removed(self) -> None:
        """Verifica que la retirada de un rol de equipo invoque handle_role_removed."""
        mock_service = make_mock_roster_sync_service()
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = make_mock_role(100, "Miembro")
        team_role = make_mock_role(1001, "Planar Shock Pingus")

        before = make_mock_member(user_id=123, roles=[base_role, team_role])
        after = make_mock_member(user_id=123, roles=[base_role])

        await cog.on_member_update(before, after)

        mock_service.handle_role_removed.assert_awaited_once()
        args, kwargs = mock_service.handle_role_removed.await_args
        called_member = kwargs.get("member") or (args[0] if args else None)
        called_role = kwargs.get("role") or (args[1] if len(args) > 1 else None)
        assert called_member == after
        assert called_role == team_role
        mock_service.handle_role_added.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_member_update_no_roles_changed(self) -> None:
        """Verifica que modificaciones sin alteración de roles no invoquen al servicio."""
        mock_service = make_mock_roster_sync_service()
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        role1 = make_mock_role(100, "Miembro")
        role2 = make_mock_role(200, "Verificado")

        before = make_mock_member(user_id=123, name="NickAntiguo", roles=[role1, role2])
        after = make_mock_member(user_id=123, name="NickNuevo", roles=[role1, role2])

        await cog.on_member_update(before, after)

        mock_service.handle_role_added.assert_not_awaited()
        mock_service.handle_role_removed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_member_update_multiple_roles_added_and_removed(self) -> None:
        """Verifica el procesamiento secuencial ante altas y bajas múltiples simultáneas."""
        mock_service = make_mock_roster_sync_service()
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        role_keep = make_mock_role(100, "Base")
        role_rem1 = make_mock_role(201, "OldTeam1")
        role_rem2 = make_mock_role(202, "OldTeam2")
        role_add1 = make_mock_role(301, "NewTeam1")
        role_add2 = make_mock_role(302, "NewTeam2")

        before = make_mock_member(user_id=123, roles=[role_keep, role_rem1, role_rem2])
        after = make_mock_member(user_id=123, roles=[role_keep, role_add1, role_add2])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == 2
        assert mock_service.handle_role_removed.await_count == 2

    @pytest.mark.asyncio
    async def test_on_member_update_service_missing_graceful(self) -> None:
        """Verifica que on_member_update no lance excepción si roster_sync_service es None."""
        bot = make_mock_bot(roster_sync_service=None)
        cog = RosterCog(bot)

        role1 = make_mock_role(100, "Rol1")
        role2 = make_mock_role(200, "Rol2")

        before = make_mock_member(user_id=123, roles=[role1])
        after = make_mock_member(user_id=123, roles=[role1, role2])

        # No debe lanzar ninguna excepción
        await cog.on_member_update(before, after)

    @pytest.mark.asyncio
    async def test_on_member_update_exception_resilience_per_role(self) -> None:
        """Verifica que un fallo al sincronizar un rol no impida procesar los roles restantes."""
        mock_service = make_mock_roster_sync_service()
        mock_service.handle_role_added.side_effect = [
            RuntimeError("Fallo de conexión en DB temporal"),
            None,
        ]

        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = make_mock_role(100, "Base")
        role_fail = make_mock_role(201, "TeamFail")
        role_ok = make_mock_role(202, "TeamOk")

        before = make_mock_member(user_id=123, roles=[base_role])
        after = make_mock_member(user_id=123, roles=[base_role, role_fail, role_ok])

        await cog.on_member_update(before, after)

        # Se debe haber intentado procesar ambos roles a pesar del fallo en el primero
        assert mock_service.handle_role_added.await_count == 2

    @pytest.mark.asyncio
    async def test_on_member_update_exception_resilience_removed_role(self) -> None:
        """Verifica que un fallo al retirar un rol no impida procesar otros roles retirados."""
        mock_service = make_mock_roster_sync_service()
        mock_service.handle_role_removed.side_effect = [
            RuntimeError("Fallo en BD al retirar"),
            True,
        ]

        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = make_mock_role(100, "Base")
        role_rem1 = make_mock_role(201, "Team1")
        role_rem2 = make_mock_role(202, "Team2")

        before = make_mock_member(user_id=123, roles=[base_role, role_rem1, role_rem2])
        after = make_mock_member(user_id=123, roles=[base_role])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_removed.await_count == 2


# ===========================================================================
# 3. Slash Command: /gestionar-posicion
# ===========================================================================


class TestRosterCogCommands:
    """Pruebas unitarias para el comando /gestionar-posicion y permisos."""

    def test_gestionar_posicion_default_permissions(self) -> None:
        """Verifica que /gestionar-posicion declare default_permissions(manage_guild=True)."""
        bot = make_mock_bot()
        cog = RosterCog(bot)

        cmd = getattr(cog, "gestionar_posicion", None)
        assert cmd is not None
        assert cmd.default_permissions is not None
        assert cmd.default_permissions.manage_guild is True

    @pytest.mark.asyncio
    async def test_gestionar_posicion_denied_for_non_staff(self) -> None:
        """Verifica rechazo inmediato a usuarios sin rol de Staff ni permisos administrativos."""
        mock_service = make_mock_roster_sync_service()
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        unauth_user = make_mock_member(
            user_id=999,
            roles=[],
            is_admin=False,
            can_manage_guild=False,
        )
        target_member = make_mock_member(user_id=555, name="Target")
        inter = make_mock_interaction(user=unauth_user)

        # Invocación posicional agnóstica al nombre del argumento (member / miembro)
        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.await_args
        msg = args[0] if args else kwargs.get("content", "")
        assert kwargs.get("ephemeral") is True
        assert any(term in str(msg).lower() for term in ("permiso", "staff", "autorización"))

        # El servicio de base de datos nunca debe ser consultado por usuarios no autorizados
        inter.response.defer.assert_not_awaited()
        mock_service.get_user_teams.assert_not_awaited()
        inter.followup.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gestionar_posicion_allowed_for_staff_role(self) -> None:
        """Verifica que un usuario con rol configurado de Staff sea admitido al comando."""
        mock_service = make_mock_roster_sync_service()
        mock_service.get_user_teams.return_value = []
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        staff_role = make_mock_role(101, "Staff")
        staff_user = make_mock_member(user_id=111, roles=[staff_role])
        target_member = make_mock_member(user_id=555)
        inter = make_mock_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_service.get_user_teams.assert_awaited_once_with(str(target_member.id))

    @pytest.mark.asyncio
    async def test_gestionar_posicion_allowed_for_ceo_role(self) -> None:
        """Verifica que un usuario con rol de CEO sea admitido al comando."""
        mock_service = make_mock_roster_sync_service()
        mock_service.get_user_teams.return_value = []
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        ceo_role = make_mock_role(102, "CEO")
        ceo_user = make_mock_member(user_id=112, roles=[ceo_role])
        target_member = make_mock_member(user_id=555)
        inter = make_mock_interaction(user=ceo_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_service.get_user_teams.assert_awaited_once_with(str(target_member.id))

    @pytest.mark.asyncio
    async def test_gestionar_posicion_allowed_for_admin_or_manage_guild(self) -> None:
        """Verifica que usuarios con admin o manage_guild sean admitidos sin rol explícito."""
        mock_service = make_mock_roster_sync_service()
        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        admin_user = make_mock_member(user_id=222, roles=[], is_admin=True)
        target_member = make_mock_member(user_id=555)
        inter = make_mock_interaction(user=admin_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_gestionar_posicion_success(self) -> None:
        """
        Verifica el flujo exitoso cuando el miembro pertenece a uno o más equipos:
        - Difiere efímeramente.
        - Consulta equipos en RosterSyncService.
        - Instancia GestionarPosicionView.
        - Envía embed inicial informativo y view interactiva.
        - Asigna el mensaje resultante a view.message.
        """
        team1 = make_mock_team(name="Planar Shock Pingus", tag="PSP")
        membership1 = make_mock_membership(team=team1, role=RosterRole.MID)
        user_teams = [(team1, membership1)]

        mock_service = make_mock_roster_sync_service()
        mock_service.get_user_teams.return_value = user_teams

        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        staff_user = make_mock_member(user_id=101, roles=[make_mock_role(101, "Staff")])
        target_member = make_mock_member(user_id=555, name="Faker")
        inter = make_mock_interaction(user=staff_user)

        mock_sent_msg = MagicMock(spec=discord.Message)
        inter.followup.send = AsyncMock(return_value=mock_sent_msg)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_service.get_user_teams.assert_awaited_once_with(str(target_member.id))
        inter.followup.send.assert_awaited_once()

        kwargs = inter.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        assert "view" in kwargs
        view = kwargs["view"]
        assert isinstance(view, GestionarPosicionView)
        assert view.member == target_member
        assert view.user_teams == user_teams
        assert view.actor == staff_user
        assert view.roster_sync_service == mock_service

        assert "embed" in kwargs
        assert isinstance(kwargs["embed"], discord.Embed)
        assert view.message == mock_sent_msg

    @pytest.mark.asyncio
    async def test_gestionar_posicion_no_teams(self) -> None:
        """
        Verifica el flujo cuando el miembro no posee equipos en base de datos:
        - Difiere efímeramente.
        - Consulta equipos (retorna []).
        - Envía embed informativo de aviso.
        - NO envía vista interactiva (view es None o no se pasa).
        """
        mock_service = make_mock_roster_sync_service()
        mock_service.get_user_teams.return_value = []

        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        staff_user = make_mock_member(user_id=101, roles=[make_mock_role(101, "Staff")])
        target_member = make_mock_member(user_id=555, name="SinEquipo")
        inter = make_mock_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_service.get_user_teams.assert_awaited_once_with(str(target_member.id))
        inter.followup.send.assert_awaited_once()

        kwargs = inter.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        # Condición obligatoria: sin vista interactiva
        assert kwargs.get("view") is None or "view" not in kwargs

        assert "embed" in kwargs
        embed = kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        title_desc = f"{embed.title} {embed.description}".lower()
        assert any(w in title_desc for w in ("equipo", "plantilla", "registrado"))

    @pytest.mark.asyncio
    async def test_gestionar_posicion_service_unavailable(self) -> None:
        """Verifica respuesta controlada cuando roster_sync_service es None."""
        bot = make_mock_bot(roster_sync_service=None)
        cog = RosterCog(bot)

        staff_user = make_mock_member(user_id=101, roles=[make_mock_role(101, "Staff")])
        target_member = make_mock_member(user_id=555)
        inter = make_mock_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        # Debe responder con mensaje efímero notificando indisponibilidad
        if inter.response.send_message.await_count > 0:
            kwargs = inter.response.send_message.await_args.kwargs
            assert kwargs.get("ephemeral") is True
        else:
            kwargs = inter.followup.send.await_args.kwargs
            assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_gestionar_posicion_outside_guild(self) -> None:
        """Verifica rechazo cuando el comando se intenta ejecutar fuera de un servidor (DM)."""
        bot = make_mock_bot()
        cog = RosterCog(bot)

        staff_user = make_mock_member(user_id=101, roles=[make_mock_role(101, "Staff")])
        target_member = make_mock_member(user_id=555)
        inter = make_mock_interaction(user=staff_user, guild=None)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.await_args
        msg = args[0] if args else kwargs.get("content", "")
        assert kwargs.get("ephemeral") is True
        assert "servidor" in str(msg).lower()

    @pytest.mark.asyncio
    async def test_gestionar_posicion_db_exception_handled(self) -> None:
        """Verifica que una excepción en get_user_teams se maneje enviando error amigable."""
        mock_service = make_mock_roster_sync_service()
        mock_service.get_user_teams.side_effect = RuntimeError("Conexión perdida con la BD")

        bot = make_mock_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        staff_user = make_mock_member(user_id=101, roles=[make_mock_role(101, "Staff")])
        target_member = make_mock_member(user_id=555)
        inter = make_mock_interaction(user=staff_user)

        await cog.gestionar_posicion.callback(cog, inter, target_member)

        inter.followup.send.assert_awaited_once()
        args, kwargs = inter.followup.send.await_args
        msg = args[0] if args else kwargs.get("content", "")
        assert kwargs.get("ephemeral") is True
        assert "error" in str(msg).lower()
