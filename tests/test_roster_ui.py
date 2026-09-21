"""
Pruebas unitarias para los componentes interactivos de UI en src/liga_bot/ui/roster.py.

Cubre exhaustivamente:
1. TestGestionarPosicionViewInit: Inicialización de la vista, auto-selección en equipo único,
   generación de opciones de TeamSelect y PositionSelect.
2. TestGestionarPosicionDropdownCallbacks: Actualización de selected_team_id y selected_role.
3. TestGestionarPosicionSaveButton: Flujo de guardado exitoso y validaciones de campos requeridos.
4. TestGestionarPosicionErrorHandling: Captura de CompetitivePositionConflictError,
   PlayerNotTeamMemberError, InvalidCaptainRoleError y excepciones genéricas.
5. TestGestionarPosicionSecurityAndLifecycle: interaction_check restringido al actor y
   deshabilitación de componentes en on_timeout.
6. TestGestionarPosicionCancelButton: Cancelación de la interacción y deshabilitación.
7. TestGestionarPosicionEmbedBuilders: Generación de embeds inicial, éxito y conflicto.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from liga_bot.models.enums import Division, RosterRole
from liga_bot.models.roster import Team, TeamMembership
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    InvalidCaptainRoleError,
    PlayerNotTeamMemberError,
    RosterSyncService,
)
from liga_bot.ui.roster import (
    CancelButton,
    GestionarPosicionView,
    PositionSelect,
    SaveButton,
    SavePositionButton,
    TeamSelect,
)

# ---------------------------------------------------------------------------
# Mocks y Factorías Auxiliares
# ---------------------------------------------------------------------------


def make_mock_member(
    user_id: int = 123456789,
    name: str = "TestPlayer",
    display_name: str | None = None,
) -> MagicMock:
    """Crea un mock de discord.Member con avatar y permisos simulados."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = display_name or name
    member.mention = f"<@{user_id}>"
    member.roles = []
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    member.send = AsyncMock()

    avatar = MagicMock()
    avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/avatar.png"
    member.display_avatar = avatar
    return member


def make_mock_team(
    team_id: uuid.UUID | None = None,
    name: str = "Team Alpha",
    tag: str = "ALP",
    slug: str = "team-alpha",
    division: Division = Division.PREMIER,
    discord_role_id: int = 1001,
) -> Team:
    """Crea una instancia en memoria de Team."""
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
    """Crea una instancia en memoria de TeamMembership."""
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
    service.change_player_position = AsyncMock()
    service.get_user_teams = AsyncMock(return_value=[])
    service.handle_role_added = AsyncMock()
    service.handle_role_removed = AsyncMock()
    return service


def make_mock_interaction(
    user: MagicMock | None = None,
    guild: MagicMock | None = None,
    channel: MagicMock | None = None,
) -> MagicMock:
    """Crea un mock de discord.Interaction con response y followup asíncronos."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user or make_mock_member(user_id=999000, name="staff_actor")
    interaction.guild = guild or MagicMock(spec=discord.Guild)
    interaction.channel = channel or MagicMock(spec=discord.TextChannel)
    interaction.channel_id = interaction.channel.id
    interaction.client = MagicMock()
    interaction.edit_original_response = AsyncMock()

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(return_value=False)
    response.send_message = AsyncMock()
    response.defer = AsyncMock()
    response.edit_message = AsyncMock()
    interaction.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    followup.edit_message = AsyncMock()
    interaction.followup = followup

    return interaction


def set_select_values(select: discord.ui.Select[Any], values: list[str]) -> None:
    """Asigna valores simulados a un componente Select."""
    select.values = list(values)


# ---------------------------------------------------------------------------
# 1. Pruebas de Inicialización y Población de Dropdowns
# ---------------------------------------------------------------------------


class TestGestionarPosicionViewInit:
    """Pruebas para la inicialización y componentes de GestionarPosicionView."""

    def test_init_with_multiple_teams(self) -> None:
        """Verifica que con múltiples equipos se listen todos sin auto-seleccionar."""
        target_member = make_mock_member(user_id=111, name="PlayerOne")
        actor_member = make_mock_member(user_id=999, name="StaffAdmin")
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Alpha", tag="ALP")
        t2 = make_mock_team(name="Beta", tag="BET")
        user_teams = [
            (t1, make_mock_membership(t1, str(target_member.id), RosterRole.STAFF)),
            (t2, make_mock_membership(t2, str(target_member.id), RosterRole.TOP)),
        ]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        assert view.timeout == 180.0
        assert view.member == target_member
        assert view.actor == actor_member
        assert view.selected_team_id is None
        assert view.selected_role is None

        # Verificar team_select
        assert len(view.team_select.options) == 2
        option_values = [opt.value for opt in view.team_select.options]
        assert str(t1.id) in option_values
        assert str(t2.id) in option_values
        assert not any(opt.default for opt in view.team_select.options)

    def test_init_with_single_team_auto_selects(self) -> None:
        """Verifica que con un único equipo se auto-seleccione inmediatamente."""
        target_member = make_mock_member(user_id=222, name="SoloPlayer")
        actor_member = make_mock_member(user_id=999, name="StaffAdmin")
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Solo Team", tag="SOLO")
        user_teams = [
            (t1, make_mock_membership(t1, str(target_member.id), RosterRole.ADC)),
        ]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        assert len(view.team_select.options) == 1
        assert view.selected_team_id == t1.id
        assert view.team_select.options[0].default is True

    def test_position_select_contains_all_roster_roles(self) -> None:
        """Verifica que PositionSelect contenga los 9 roles de RosterRole."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()
        view = GestionarPosicionView(
            member=target_member,
            user_teams=[],
            roster_sync_service=service,
            actor=actor_member,
        )

        assert len(view.position_select.options) == 9
        option_values = [opt.value for opt in view.position_select.options]
        for role in RosterRole:
            assert role.value in option_values

    def test_init_with_no_teams_disables_save(self) -> None:
        """Verifica que si el usuario no tiene equipos, el botón de guardado esté deshabilitado."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(
            member=target_member,
            user_teams=[],
            roster_sync_service=service,
            actor=actor_member,
        )

        assert len(view.team_select.options) == 0 or view.team_select.disabled
        assert view.save_button.disabled is True

    def test_view_default_attributes(self) -> None:
        """Verifica atributos por defecto y componentes de GestionarPosicionView."""
        target_member = make_mock_member(user_id=111)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(
            member=target_member,
            user_teams=[],
            roster_sync_service=service,
            actor=actor_member,
        )

        assert view.timeout == 180.0
        assert view.member == target_member
        assert view.actor == actor_member
        assert view.selected_team_id is None
        assert view.selected_role is None
        assert view.is_captain is False
        assert isinstance(view.team_select, TeamSelect)
        assert isinstance(view.position_select, PositionSelect)
        assert isinstance(view.save_button, SaveButton)
        assert isinstance(view.cancel_button, CancelButton)
        assert view.team_select in view.children
        assert view.position_select in view.children
        assert view.save_button in view.children
        assert view.cancel_button in view.children

    def test_save_position_button_alias(self) -> None:
        """Verifica que SavePositionButton sea un alias válido de SaveButton."""
        assert SavePositionButton is SaveButton


# ---------------------------------------------------------------------------
# 2. Pruebas de Callbacks de Menús Desplegables
# ---------------------------------------------------------------------------


class TestGestionarPosicionDropdownCallbacks:
    """Pruebas para los eventos de selección de equipo y posición."""

    @pytest.mark.asyncio
    async def test_team_select_callback_updates_state(self) -> None:
        """Verifica que seleccionar un equipo actualice selected_team_id."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Alpha", tag="ALP")
        t2 = make_mock_team(name="Beta", tag="BET")
        user_teams = [
            (t1, make_mock_membership(t1, str(target_member.id))),
            (t2, make_mock_membership(t2, str(target_member.id))),
        ]

        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        interaction = make_mock_interaction(user=actor_member)

        set_select_values(view.team_select, [str(t2.id)])
        await view.team_select.callback(interaction)

        assert view.selected_team_id == t2.id
        assert interaction.response.defer.awaited or interaction.response.edit_message.awaited

    @pytest.mark.asyncio
    async def test_team_select_empty_values_noop(self) -> None:
        """Verifica que el callback con lista vacía o 'none' no altere el estado."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Alpha")
        view = GestionarPosicionView(
            target_member, [(t1, make_mock_membership(t1))], service, actor_member
        )
        view.selected_team_id = t1.id

        set_select_values(view.team_select, [])
        interaction = make_mock_interaction(user=actor_member)
        await view.team_select.callback(interaction)

        assert view.selected_team_id == t1.id

        set_select_values(view.team_select, ["none"])
        await view.team_select.callback(interaction)
        assert view.selected_team_id == t1.id

    @pytest.mark.asyncio
    async def test_position_select_callback_updates_state(self) -> None:
        """Verifica que seleccionar una posición actualice selected_role."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team()
        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]

        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        interaction = make_mock_interaction(user=actor_member)

        set_select_values(view.position_select, ["mid"])
        await view.position_select.callback(interaction)

        assert view.selected_role == RosterRole.MID
        assert interaction.response.defer.awaited or interaction.response.edit_message.awaited

    @pytest.mark.asyncio
    async def test_position_select_non_competitive_resets_captaincy(self) -> None:
        """Verifica que seleccionar un rol no titular resetee is_captain a False."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team()
        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]

        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        view.is_captain = True

        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["coach"])
        await view.position_select.callback(interaction)

        assert view.selected_role == RosterRole.COACH
        assert view.is_captain is False

    @pytest.mark.asyncio
    async def test_sequential_dropdown_selections(self) -> None:
        """Selección secuencial: equipo A -> rol MID -> cambio a equipo B preservando rol."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Alpha")
        t2 = make_mock_team(name="Beta")
        user_teams = [
            (t1, make_mock_membership(t1, str(target_member.id))),
            (t2, make_mock_membership(t2, str(target_member.id))),
        ]

        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        interaction = make_mock_interaction(user=actor_member)

        # 1. Seleccionar equipo 1
        set_select_values(view.team_select, [str(t1.id)])
        await view.team_select.callback(interaction)
        assert view.selected_team_id == t1.id

        # 2. Seleccionar rol MID
        set_select_values(view.position_select, ["mid"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.MID

        # 3. Cambiar a equipo 2 manteniendo rol MID
        set_select_values(view.team_select, [str(t2.id)])
        await view.team_select.callback(interaction)
        assert view.selected_team_id == t2.id
        assert view.selected_role == RosterRole.MID


# ---------------------------------------------------------------------------
# 3. Pruebas de Guardado y Delegación al Servicio
# ---------------------------------------------------------------------------


class TestGestionarPosicionSaveButton:
    """Pruebas para el botón de guardado y comunicación con RosterSyncService."""

    @pytest.mark.asyncio
    async def test_save_button_success_calls_service(self) -> None:
        """Verifica que al pulsar guardar con datos válidos se invoque el servicio correctamente."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Alpha")
        updated_membership = make_mock_membership(t1, str(target_member.id), RosterRole.TOP)
        service.change_player_position.return_value = updated_membership

        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)

        view.selected_team_id = t1.id
        view.selected_role = RosterRole.TOP

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        service.change_player_position.assert_awaited_once_with(
            discord_user_id=str(target_member.id),
            team_id=t1.id,
            new_role=RosterRole.TOP,
            actor_id=str(actor_member.id),
            is_captain=False,
        )

        # Los componentes deben quedar deshabilitados
        assert all(child.disabled for child in view.children)
        assert (
            interaction.response.edit_message.awaited
            or interaction.edit_original_response.awaited
            or interaction.followup.send.awaited
        )

    @pytest.mark.asyncio
    async def test_save_without_team_warns_user(self) -> None:
        """Verifica que intentar guardar sin equipo muestre advertencia efímera."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(target_member, [], service, actor_member)
        view.selected_team_id = None
        view.selected_role = RosterRole.MID

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        service.change_player_position.assert_not_called()
        assert interaction.response.send_message.awaited or interaction.followup.send.awaited
        if interaction.response.send_message.awaited:
            call_kwargs = interaction.response.send_message.call_args.kwargs
            assert call_kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_save_without_role_warns_user(self) -> None:
        """Verifica que intentar guardar sin rol muestre advertencia efímera."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team()
        view = GestionarPosicionView(
            target_member, [(t1, make_mock_membership(t1))], service, actor_member
        )
        view.selected_team_id = t1.id
        view.selected_role = None

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        service.change_player_position.assert_not_called()
        assert interaction.response.send_message.awaited or interaction.followup.send.awaited
        if interaction.response.send_message.awaited:
            call_kwargs = interaction.response.send_message.call_args.kwargs
            assert call_kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_save_button_missing_both_validation(self) -> None:
        """Verifica que intentar guardar sin equipo ni rol muestre advertencia efímera."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(target_member, [], service, actor_member)
        view.selected_team_id = None
        view.selected_role = None

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        service.change_player_position.assert_not_called()
        assert interaction.response.send_message.awaited or interaction.followup.send.awaited

    @pytest.mark.asyncio
    async def test_save_button_with_captain_flag(self) -> None:
        """Verifica que si la vista tiene is_captain=True se envíe al servicio."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Alpha")
        updated_membership = make_mock_membership(
            t1, str(target_member.id), RosterRole.SUPPORT, is_captain=True
        )
        service.change_player_position.return_value = updated_membership

        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)

        view.selected_team_id = t1.id
        view.selected_role = RosterRole.SUPPORT
        view.is_captain = True

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        service.change_player_position.assert_awaited_once_with(
            discord_user_id=str(target_member.id),
            team_id=t1.id,
            new_role=RosterRole.SUPPORT,
            actor_id=str(actor_member.id),
            is_captain=True,
        )


# ---------------------------------------------------------------------------
# 4. Pruebas de Captura y Manejo de Errores de Dominio
# ---------------------------------------------------------------------------


class TestGestionarPosicionErrorHandling:
    """Pruebas para el manejo de excepciones de dominio."""

    @pytest.mark.asyncio
    async def test_save_competitive_position_conflict_renders_cleanly(self) -> None:
        """Verifica captura de CompetitivePositionConflictError sin excepción no controlada."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team(name="Alpha")
        t2_id = uuid.uuid4()
        conflict_exc = CompetitivePositionConflictError(
            discord_user_id=str(target_member.id),
            existing_team_id=t2_id,
            new_team_id=t1.id,
            existing_role=RosterRole.MID,
            attempted_role=RosterRole.TOP,
        )
        service.change_player_position.side_effect = conflict_exc

        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        view.selected_team_id = t1.id
        view.selected_role = RosterRole.TOP

        interaction = make_mock_interaction(user=actor_member)

        # No debe levantar excepción
        await view.save_button.callback(interaction)

        # Debe responder con mensaje de error / advertencia
        assert (
            interaction.response.send_message.awaited
            or interaction.response.edit_message.awaited
            or interaction.followup.send.awaited
        )
        # La vista no debe bloquearse permanentemente para permitir al usuario cambiar la posición
        assert not view.save_button.disabled

    @pytest.mark.asyncio
    async def test_save_player_not_team_member_renders_cleanly(self) -> None:
        """Verifica captura de PlayerNotTeamMemberError."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team()
        service.change_player_position.side_effect = PlayerNotTeamMemberError(
            discord_user_id=str(target_member.id),
            team_id=t1.id,
        )

        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        view.selected_team_id = t1.id
        view.selected_role = RosterRole.ADC

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        assert (
            interaction.response.send_message.awaited
            or interaction.response.edit_message.awaited
            or interaction.followup.send.awaited
        )
        assert not view.save_button.disabled

    @pytest.mark.asyncio
    async def test_save_invalid_captain_role_renders_cleanly(self) -> None:
        """Verifica captura de InvalidCaptainRoleError."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        service.change_player_position.side_effect = InvalidCaptainRoleError(role=RosterRole.COACH)

        t1 = make_mock_team()
        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        view.selected_team_id = t1.id
        view.selected_role = RosterRole.COACH

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        assert (
            interaction.response.send_message.awaited
            or interaction.response.edit_message.awaited
            or interaction.followup.send.awaited
        )
        assert not view.save_button.disabled

    @pytest.mark.asyncio
    async def test_save_generic_exception_graceful_catch(self) -> None:
        """Verifica captura de Exception genérica sin propagación incontrolada."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        service.change_player_position.side_effect = RuntimeError("Database connection timeout")

        t1 = make_mock_team()
        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        view.selected_team_id = t1.id
        view.selected_role = RosterRole.ADC

        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        assert (
            interaction.response.send_message.awaited
            or interaction.response.edit_message.awaited
            or interaction.followup.send.awaited
        )
        assert not view.save_button.disabled


# ---------------------------------------------------------------------------
# 5. Pruebas de Seguridad y Ciclo de Vida
# ---------------------------------------------------------------------------


class TestGestionarPosicionSecurityAndLifecycle:
    """Pruebas para interaction_check y on_timeout."""

    @pytest.mark.asyncio
    async def test_interaction_check_allows_actor(self) -> None:
        """Verifica que el actor del comando pueda interactuar."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(target_member, [], service, actor_member)
        interaction = make_mock_interaction(user=actor_member)

        check_res = view.interaction_check(interaction)
        is_allowed = await check_res if inspect.isawaitable(check_res) else check_res

        assert is_allowed is True
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_interaction_check_rejects_non_actor(self) -> None:
        """Verifica que un usuario ajeno reciba rechazo efímero."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        intruder = make_mock_member(user_id=777)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(target_member, [], service, actor_member)
        interaction = make_mock_interaction(user=intruder)

        check_res = view.interaction_check(interaction)
        is_allowed = await check_res if inspect.isawaitable(check_res) else check_res

        assert is_allowed is False
        assert interaction.response.send_message.awaited
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_interaction_check_rejects_target_member_if_not_actor(self) -> None:
        """Verifica que el jugador objetivo sea rechazado si no es el actor del comando."""
        target_member = make_mock_member(user_id=123)
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(target_member, [], service, actor_member)
        interaction = make_mock_interaction(user=target_member)

        check_res = view.interaction_check(interaction)
        is_allowed = await check_res if inspect.isawaitable(check_res) else check_res

        assert is_allowed is False
        assert interaction.response.send_message.awaited
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_on_timeout_disables_all_components(self) -> None:
        """Verifica que on_timeout deshabilite todos los componentes de la vista."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team()
        user_teams = [(t1, make_mock_membership(t1))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)

        # Pre-condición: componentes activos
        assert any(not child.disabled for child in view.children)

        mock_msg = AsyncMock(spec=discord.Message)
        view.message = mock_msg

        await view.on_timeout()

        assert all(child.disabled for child in view.children)
        mock_msg.edit.assert_awaited_once_with(view=view)

    @pytest.mark.asyncio
    async def test_on_timeout_edits_message_when_present(self) -> None:
        """Verifica que on_timeout edite el mensaje cuando está asignado."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(target_member, [], service, actor_member)
        mock_msg = AsyncMock(spec=discord.Message)
        view.message = mock_msg

        await view.on_timeout()
        mock_msg.edit.assert_awaited_once_with(view=view)

    @pytest.mark.asyncio
    async def test_on_timeout_without_message_does_not_fail(self) -> None:
        """Verifica que on_timeout no falle si view.message es None."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        view = GestionarPosicionView(target_member, [], service, actor_member)
        view.message = None

        await view.on_timeout()
        assert all(child.disabled for child in view.children)


# ---------------------------------------------------------------------------
# 6. Pruebas de CancelButton y Embeds
# ---------------------------------------------------------------------------


class TestGestionarPosicionCancelButton:
    """Pruebas para el botón de cancelación."""

    @pytest.mark.asyncio
    async def test_cancel_button_disables_view_and_stops(self) -> None:
        """Verifica que CancelButton deshabilite la vista, la detenga y edite el mensaje."""
        target_member = make_mock_member()
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()

        t1 = make_mock_team()
        view = GestionarPosicionView(
            target_member, [(t1, make_mock_membership(t1))], service, actor_member
        )
        interaction = make_mock_interaction(user=actor_member)

        await view.cancel_button.callback(interaction)

        assert all(child.disabled for child in view.children)
        assert view.is_finished()
        assert (
            interaction.response.edit_message.awaited
            or interaction.edit_original_response.awaited
            or interaction.followup.send.awaited
        )


class TestGestionarPosicionEmbedBuilders:
    """Pruebas para los métodos generadores de embeds."""

    def test_build_initial_embed(self) -> None:
        """Verifica contenido y campos del embed inicial."""
        target_member = make_mock_member(user_id=123, name="CoolPlayer")
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()
        t1 = make_mock_team()

        view = GestionarPosicionView(
            target_member, [(t1, make_mock_membership(t1))], service, actor_member
        )
        embed = view.build_initial_embed()

        assert "Gestión de Posición" in embed.title
        assert target_member.mention in embed.fields[0].value

    def test_build_success_embed(self) -> None:
        """Verifica contenido y campos del recibo de éxito."""
        target_member = make_mock_member(user_id=123, name="CoolPlayer")
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()
        t1 = make_mock_team(name="Alpha", tag="ALP")
        membership = make_mock_membership(t1, str(target_member.id), role=RosterRole.MID)

        view = GestionarPosicionView(target_member, [(t1, membership)], service, actor_member)
        view.selected_team_id = t1.id

        updated = make_mock_membership(
            t1, str(target_member.id), role=RosterRole.TOP, is_captain=True
        )
        embed = view.build_success_embed(updated)

        assert "Actualizada" in embed.title
        assert "Alpha [ALP]" in embed.fields[1].value
        assert "top" in embed.fields[3].value
        assert "Sí (Capitán oficial)" in embed.fields[5].value

    def test_build_conflict_embed(self) -> None:
        """Verifica contenido y detalles del embed de conflicto deportivo."""
        target_member = make_mock_member(user_id=123, name="CoolPlayer")
        actor_member = make_mock_member(user_id=999)
        service = make_mock_roster_sync_service()
        t1 = make_mock_team()

        view = GestionarPosicionView(
            target_member, [(t1, make_mock_membership(t1))], service, actor_member
        )
        exc = CompetitivePositionConflictError(
            discord_user_id=str(target_member.id),
            existing_team_id=uuid.uuid4(),
            new_team_id=t1.id,
            existing_role=RosterRole.MID,
            attempted_role=RosterRole.TOP,
        )
        embed = view.build_conflict_embed(exc)

        assert "Conflicto de Posición Competitiva" in embed.title
        assert target_member.mention in embed.description
        assert "top" in embed.description
        assert "mid" in embed.description
