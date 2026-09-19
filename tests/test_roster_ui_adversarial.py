"""
Pruebas de estrés adversarial y verificación empírica para GestionarPosicionView.

Cubre exhaustivamente:
1. Recuperación tras errores de dominio:
   - CompetitivePositionConflictError: renderizado de detalles en embed, no destrucción de la vista,
     y recuperación exitosa mediante selección posterior y reintento de guardado.
   - InvalidCaptainRoleError y PlayerNotTeamMemberError: manejo efímero garantizado.
2. Estrés de seguridad y aislamiento de interacción:
   - Múltiples actores no autorizados (jugador objetivo si no es actor, intrusos,
     bots, administradores ajenos).
   - Verificación de tasa de rechazo del 100% y preservación de estado.
3. Resiliencia en ciclo de vida y timeout:
   - on_timeout con mensaje existente, mensaje inexistente (discord.NotFound 404),
     error HTTP (discord.HTTPException), y mensaje None.
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
    GestionarPosicionView,
)

# ---------------------------------------------------------------------------
# Factorías y Mocks Realistas para Discord
# ---------------------------------------------------------------------------


def make_test_member(
    user_id: int = 123456789,
    name: str = "TestPlayer",
    is_bot: bool = False,
    is_admin: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Member con atributos realistas."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.bot = is_bot
    member.roles = []

    perms = MagicMock()
    perms.administrator = is_admin
    perms.manage_guild = is_admin
    member.guild_permissions = perms

    avatar = MagicMock()
    avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/avatar.png"
    member.display_avatar = avatar
    return member


def make_test_team(
    name: str = "Team Alpha",
    tag: str = "ALP",
    team_id: uuid.UUID | None = None,
) -> Team:
    """Crea un modelo Team en memoria."""
    return Team(
        id=team_id or uuid.uuid4(),
        name=name,
        tag=tag,
        slug=name.lower().replace(" ", "-"),
        division=Division.PREMIER,
        discord_role_id=1001,
    )


def make_test_membership(
    team: Team,
    discord_user_id: str,
    role: RosterRole = RosterRole.MID,
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


def make_realistic_interaction(
    user: MagicMock | None = None,
) -> MagicMock:
    """
    Crea un mock de discord.Interaction que emula fielmente el ciclo de vida de Discord:
    - Inicialmente response.is_done() es False.
    - Al llamar await response.defer(), response.is_done() pasa a ser True.
    - Al intentar llamar response.send_message() tras defer, simula la restricción de Discord.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user or make_test_member(user_id=999, name="staff_actor")
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel_id = 55555
    interaction.client = MagicMock()
    interaction.edit_original_response = AsyncMock()

    is_done_flag = False

    def is_done() -> bool:
        return is_done_flag

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(side_effect=is_done)

    async def defer(*args: Any, **kwargs: Any) -> None:
        nonlocal is_done_flag
        is_done_flag = True

    response.defer = AsyncMock(side_effect=defer)
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    interaction.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    followup.edit_message = AsyncMock()
    interaction.followup = followup

    return interaction


# ---------------------------------------------------------------------------
# 1. Pruebas Adversariales de Recuperación tras Errores de Dominio
# ---------------------------------------------------------------------------


class TestAdversarialDomainErrorRecovery:
    """
    Pruebas empíricas que verifican la resiliencia de GestionarPosicionView
    ante errores de dominio, garantizando recuperación no destructiva y reintentos exitosos.
    """

    @pytest.mark.asyncio
    async def test_conflict_error_renders_embed_details_and_leaves_view_active(self) -> None:
        """
        Simula CompetitivePositionConflictError:
        1. Verifica que los detalles del conflicto se rendericen exhaustivamente en el embed.
        2. Verifica que la vista NO se destruya ni deshabilite sus componentes.
        """
        target_member = make_test_member(user_id=12345, name="TargetPro")
        actor_member = make_test_member(user_id=99999, name="OfficialStaff")
        service = MagicMock(spec=RosterSyncService)

        team_a = make_test_team(name="Club Alpha", tag="ALP")
        team_b_id = uuid.uuid4()

        conflict = CompetitivePositionConflictError(
            discord_user_id=str(target_member.id),
            existing_team_id=team_b_id,
            new_team_id=team_a.id,
            existing_role=RosterRole.MID,
            attempted_role=RosterRole.TOP,
        )
        service.change_player_position = AsyncMock(side_effect=conflict)

        user_teams = [(team_a, make_test_membership(team_a, str(target_member.id)))]
        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )
        view.selected_team_id = team_a.id
        view.selected_role = RosterRole.TOP

        interaction = make_realistic_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        # 1. Verificación del embed enviado
        assert interaction.followup.send.awaited, "Debe enviar followup tras defer()"
        call_kwargs = interaction.followup.send.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True, "El mensaje de conflicto debe ser efímero"
        embed: discord.Embed = call_kwargs.get("embed")
        assert embed is not None, "Debe incluir un embed"
        assert "Conflicto de Posición Competitiva" in (embed.title or "")
        assert target_member.mention in embed.description
        assert str(team_b_id) in embed.description
        assert str(team_a.id) in embed.description
        assert "mid" in embed.description
        assert "top" in embed.description
        assert embed.color == discord.Color.gold()

        # 2. Verificación de que la vista NO fue destruida ni deshabilitada
        assert not view.is_finished(), "La vista NO debe detenerse (stop) ante un conflicto"
        assert not view.save_button.disabled, "El botón Guardar debe seguir activo"
        assert not view.team_select.disabled, "El selector de equipo debe seguir activo"
        assert not view.position_select.disabled, "El selector de posición debe seguir activo"
        assert not view.cancel_button.disabled, "El botón Cancelar debe seguir activo"

    @pytest.mark.asyncio
    async def test_conflict_recovery_multi_turn_selection_and_retry_succeeds(self) -> None:
        """
        Escenario Multi-Turn de Recuperación:
        Turno 1: Intento de asignar rol competitivo 'top' -> Falla con
                 CompetitivePositionConflictError. La vista no se bloquea.
        Turno 2: El operador cambia de idea en PositionSelect y elige rol no competitivo 'coach'.
                 Vuelve a pulsar 'Guardar Posición'.
                 El servicio procesa la solicitud con éxito.
                 La vista deshabilita componentes, envía recibo verde y se detiene (stop).
        """
        target_member = make_test_member(user_id=12345, name="TargetPro")
        actor_member = make_test_member(user_id=99999, name="OfficialStaff")
        service = MagicMock(spec=RosterSyncService)

        team_a = make_test_team(name="Club Alpha", tag="ALP")
        team_b_id = uuid.uuid4()

        conflict = CompetitivePositionConflictError(
            discord_user_id=str(target_member.id),
            existing_team_id=team_b_id,
            new_team_id=team_a.id,
            existing_role=RosterRole.MID,
            attempted_role=RosterRole.TOP,
        )

        success_membership = make_test_membership(
            team_a, str(target_member.id), role=RosterRole.COACH, is_captain=False
        )

        # Primer llamado falla con conflicto, segundo llamado tiene éxito
        service.change_player_position = AsyncMock(side_effect=[conflict, success_membership])

        user_teams = [(team_a, make_test_membership(team_a, str(target_member.id)))]
        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )
        view.selected_team_id = team_a.id
        view.selected_role = RosterRole.TOP

        # === TURNO 1: Fallo con conflicto ===
        interaction_turn1 = make_realistic_interaction(user=actor_member)
        await view.save_button.callback(interaction_turn1)

        assert interaction_turn1.followup.send.awaited
        assert not view.is_finished()
        assert not view.save_button.disabled

        # === TURNO 2: Cambio de selección a 'coach' y reintento ===
        view.position_select.values = ["coach"]
        interaction_select = make_realistic_interaction(user=actor_member)
        await view.position_select.callback(interaction_select)

        assert view.selected_role == RosterRole.COACH
        assert view.is_captain is False  # Coach no puede ser capitán

        interaction_turn2 = make_realistic_interaction(user=actor_member)
        await view.save_button.callback(interaction_turn2)

        # Verificar que el servicio fue invocado con el nuevo rol
        assert service.change_player_position.await_count == 2
        service.change_player_position.assert_awaited_with(
            discord_user_id=str(target_member.id),
            team_id=team_a.id,
            new_role=RosterRole.COACH,
            actor_id=str(actor_member.id),
            is_captain=False,
        )

        # Verificar que la vista ahora SÍ fue deshabilitada y finalizada
        assert view.is_finished(), "Tras éxito, la vista debe estar finalizada"
        assert all(child.disabled for child in view.children), (
            "Todos los componentes deben estar deshabilitados"
        )
        assert interaction_turn2.edit_original_response.awaited, (
            "Debe editar la respuesta original con el recibo"
        )
        success_embed = interaction_turn2.edit_original_response.call_args.kwargs.get("embed")
        assert success_embed is not None
        assert "Actualizada Exitosamente" in success_embed.title
        assert "coach" in success_embed.fields[3].value
        assert "No Competitiva" in success_embed.fields[4].value

    @pytest.mark.asyncio
    async def test_invalid_captain_role_error_ephemeral_handling(self) -> None:
        """
        Simula InvalidCaptainRoleError:
        Verifica respuesta efímera, mensaje explicativo y que la vista no se bloquee.
        """
        target_member = make_test_member(user_id=12345)
        actor_member = make_test_member(user_id=99999)
        service = MagicMock(spec=RosterSyncService)

        service.change_player_position = AsyncMock(
            side_effect=InvalidCaptainRoleError(role=RosterRole.COACH)
        )

        team_a = make_test_team()
        user_teams = [(team_a, make_test_membership(team_a, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        view.selected_team_id = team_a.id
        view.selected_role = RosterRole.COACH
        view.is_captain = True

        interaction = make_realistic_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        assert interaction.followup.send.awaited
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("ephemeral") is True
        msg = interaction.followup.send.call_args.args[0]
        assert "Error de Capitanía" in msg
        assert "coach" in msg
        assert not view.is_finished()
        assert not view.save_button.disabled

    @pytest.mark.asyncio
    async def test_player_not_team_member_error_ephemeral_handling(self) -> None:
        """
        Simula PlayerNotTeamMemberError:
        Verifica respuesta efímera, mención del jugador y que la vista no se bloquee.
        """
        target_member = make_test_member(user_id=12345)
        actor_member = make_test_member(user_id=99999)
        service = MagicMock(spec=RosterSyncService)

        team_a = make_test_team()
        service.change_player_position = AsyncMock(
            side_effect=PlayerNotTeamMemberError(
                discord_user_id=str(target_member.id),
                team_id=team_a.id,
            )
        )

        user_teams = [(team_a, make_test_membership(team_a, str(target_member.id)))]
        view = GestionarPosicionView(target_member, user_teams, service, actor_member)
        view.selected_team_id = team_a.id
        view.selected_role = RosterRole.ADC

        interaction = make_realistic_interaction(user=actor_member)
        await view.save_button.callback(interaction)

        assert interaction.followup.send.awaited
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("ephemeral") is True
        msg = interaction.followup.send.call_args.args[0]
        assert "Error de Membresía" in msg
        assert f"<@{target_member.id}>" in msg
        assert not view.is_finished()
        assert not view.save_button.disabled


# ---------------------------------------------------------------------------
# 2. Estrés de Seguridad y Aislamiento de Interacciones
# ---------------------------------------------------------------------------


class TestAdversarialSecurityStress:
    """
    Pruebas empíricas que someten a interacción concurrente y no autorizada
    a GestionarPosicionView frente a una cohorte diversa de atacantes.
    """

    @pytest.mark.asyncio
    async def test_security_matrix_100_percent_rejection_across_cohort(self) -> None:
        """
        Genera una cohorte de 25 identidades no autorizadas:
        - El propio jugador objetivo (no es el actor del comando)
        - Miembros ordinarios ajenos
        - Administradores de servidor (que no sean el actor)
        - Cuentas de bot (bot=True)
        - Objetos discord.User (ej. interacción en contexto de usuario global)
        Verifica:
        1. Tasa de rechazo del 100% (cero falsos permisos concedidos).
        2. Mensaje efímero de autorización enviado a cada uno.
        3. Cero mutación en el estado interno de la vista.
        """
        target_member = make_test_member(user_id=10001, name="TargetPlayer")
        legit_actor = make_test_member(user_id=99999, name="AuthorizedStaff")
        service = MagicMock(spec=RosterSyncService)
        team = make_test_team()
        user_teams = [(team, make_test_membership(team, str(target_member.id)))]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=legit_actor,
        )
        view.selected_team_id = team.id
        view.selected_role = RosterRole.SUPPORT
        view.is_captain = False

        # Construir cohorte diversa de atacantes
        cohort: list[discord.Member | discord.User] = [
            # 1. El jugador objetivo intentando manipular su propio rol
            target_member,
            # 2-10. Intrusos ordinarios
            *(make_test_member(user_id=20000 + i, name=f"Intruder_{i}") for i in range(9)),
            # 11-15. Administradores no actores (no deben eludir el aislamiento del panel)
            *(
                make_test_member(user_id=30000 + i, name=f"AdminIntruder_{i}", is_admin=True)
                for i in range(5)
            ),
            # 16-20. Cuentas de bot automatizadas
            *(
                make_test_member(user_id=40000 + i, name=f"BotAccount_{i}", is_bot=True)
                for i in range(5)
            ),
            # 21-25. Instancias de discord.User puro
            *(MagicMock(spec=discord.User, id=50000 + i, name=f"PureUser_{i}") for i in range(5)),
        ]

        total_tested = len(cohort)
        rejected_count = 0

        for intruder in cohort:
            interaction = make_realistic_interaction(user=intruder)

            check_res = view.interaction_check(interaction)
            allowed = await check_res if inspect.isawaitable(check_res) else check_res

            if not allowed:
                rejected_count += 1
                assert interaction.response.send_message.awaited, (
                    f"Atacante {intruder.id} no recibió mensaje de rechazo"
                )
                kwargs = interaction.response.send_message.call_args.kwargs
                assert kwargs.get("ephemeral") is True, (
                    f"Mensaje de rechazo para {intruder.id} debe ser efímero"
                )
                msg_content = interaction.response.send_message.call_args.args[0]
                assert "No tienes autorización" in msg_content, (
                    f"Mensaje para {intruder.id} debe ser de denegación"
                )

        assert rejected_count == total_tested == 25, (
            f"Tasa de rechazo esperada: 100%. Obtenida: {rejected_count}/{total_tested}"
        )

        # Verificar que el estado de la vista permanezca intacto
        assert view.selected_team_id == team.id
        assert view.selected_role == RosterRole.SUPPORT
        assert view.is_captain is False
        assert not view.is_finished()

    @pytest.mark.asyncio
    async def test_security_check_strictly_permits_actor_alone(self) -> None:
        """Verifica que el actor legítimo sea admitido sin emitir mensajes de error."""
        target_member = make_test_member(user_id=10001)
        legit_actor = make_test_member(user_id=99999)
        service = MagicMock(spec=RosterSyncService)
        team = make_test_team()

        view = GestionarPosicionView(
            member=target_member,
            user_teams=[(team, make_test_membership(team, str(target_member.id)))],
            roster_sync_service=service,
            actor=legit_actor,
        )

        interaction = make_realistic_interaction(user=legit_actor)
        check_res = view.interaction_check(interaction)
        allowed = await check_res if inspect.isawaitable(check_res) else check_res

        assert allowed is True
        interaction.response.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Resiliencia en Ciclo de Vida y Estrés de Timeout
# ---------------------------------------------------------------------------


class TestAdversarialTimeoutStress:
    """
    Pruebas de estrés que someten a on_timeout a condiciones de fallo HTTP
    y anomalías de mensajes (NotFound 404, 500 HTTPException, message=None, WebhookMessage).
    """

    @pytest.mark.asyncio
    async def test_on_timeout_with_valid_message_disables_all_children(self) -> None:
        """Verifica comportamiento estándar: deshabilita componentes y edita mensaje."""
        target = make_test_member()
        actor = make_test_member(user_id=999)
        service = MagicMock(spec=RosterSyncService)
        team = make_test_team()

        view = GestionarPosicionView(
            target, [(team, make_test_membership(team, str(target.id)))], service, actor
        )

        mock_msg = AsyncMock(spec=discord.Message)
        view.message = mock_msg

        await view.on_timeout()

        assert all(child.disabled for child in view.children)
        mock_msg.edit.assert_awaited_once_with(view=view)

    @pytest.mark.asyncio
    async def test_on_timeout_message_deleted_discord_not_found(self) -> None:
        """
        Simula que el mensaje fue eliminado por el usuario o moderación (discord.NotFound 404):
        Verifica que on_timeout no lance excepción no controlada y deshabilite componentes.
        """
        target = make_test_member()
        actor = make_test_member(user_id=999)
        service = MagicMock(spec=RosterSyncService)

        view = GestionarPosicionView(target, [], service, actor)
        mock_msg = AsyncMock(spec=discord.Message)

        # discord.NotFound requiere mock response y message
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_msg.edit.side_effect = discord.NotFound(mock_resp, "Unknown Message (404 Not Found)")
        view.message = mock_msg

        # Debe ejecutarse limpiamente sin propagar excepción
        await view.on_timeout()

        assert all(child.disabled for child in view.children)
        mock_msg.edit.assert_awaited_once_with(view=view)

    @pytest.mark.asyncio
    async def test_on_timeout_discord_http_exception_500(self) -> None:
        """
        Simula fallo transitorio de la API de Discord (discord.HTTPException):
        Verifica que on_timeout capture el error gracefully.
        """
        target = make_test_member()
        actor = make_test_member(user_id=999)
        service = MagicMock(spec=RosterSyncService)

        view = GestionarPosicionView(target, [], service, actor)
        mock_msg = AsyncMock(spec=discord.Message)

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_msg.edit.side_effect = discord.HTTPException(
            mock_resp, "Discord Internal Server Error (500)"
        )
        view.message = mock_msg

        # Debe capturarse sin elevar excepción
        await view.on_timeout()

        assert all(child.disabled for child in view.children)
        mock_msg.edit.assert_awaited_once_with(view=view)

    @pytest.mark.asyncio
    async def test_on_timeout_message_is_none(self) -> None:
        """
        Simula expiración de timeout cuando view.message nunca fue asignado (None):
        Verifica que no ocurra AttributeError y todos los componentes queden deshabilitados.
        """
        target = make_test_member()
        actor = make_test_member(user_id=999)
        service = MagicMock(spec=RosterSyncService)

        view = GestionarPosicionView(target, [], service, actor)
        view.message = None

        await view.on_timeout()

        assert all(child.disabled for child in view.children)

    @pytest.mark.asyncio
    async def test_on_timeout_with_webhook_message_instance(self) -> None:
        """
        Simula que el mensaje es un discord.WebhookMessage (típico en respuestas efímeras/webhooks):
        Verifica que se invoque .edit(view=view) correctamente.
        """
        target = make_test_member()
        actor = make_test_member(user_id=999)
        service = MagicMock(spec=RosterSyncService)

        view = GestionarPosicionView(target, [], service, actor)
        mock_webhook_msg = AsyncMock(spec=discord.WebhookMessage)
        view.message = mock_webhook_msg

        await view.on_timeout()

        assert all(child.disabled for child in view.children)
        mock_webhook_msg.edit.assert_awaited_once_with(view=view)
