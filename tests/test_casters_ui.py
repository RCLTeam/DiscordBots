"""
Unit and integration tests for casters Discord UI components:
- build_match_caster_embed
- MatchCasterView
- CasterActionButton (DynamicItem)
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from liga_bot.config import Settings
from liga_bot.models.caster import CasterRole, MatchCaster
from liga_bot.models.enums import Division
from liga_bot.models.match import Match
from liga_bot.models.team import Team
from liga_bot.services.caster_service import CasterAssignmentResult, MatchCastersData
from liga_bot.ui.casters import (
    CasterActionButton,
    MatchCasterView,
    build_match_caster_embed,
)

# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


def make_test_match(
    match_id: uuid.UUID | None = None,
    jornada: int = 1,
    team1_name: str = "Team Alpha",
    team2_name: str = "Team Beta",
    scheduled_at: datetime | None = None,
    division: Division = Division.PREMIER,
) -> Match:
    """Crea una entidad Match para pruebas sin persistencia en base de datos."""
    m_id = match_id or uuid.uuid4()
    t1 = Team(id=uuid.uuid4(), name=team1_name, tag="ALP")
    t2 = Team(id=uuid.uuid4(), name=team2_name, tag="BET")
    match = Match(
        id=m_id,
        jornada=jornada,
        team1_id=t1.id,
        team2_id=t2.id,
        scheduled_at=scheduled_at,
    )
    match.team1 = t1
    match.team2 = t2
    match.division = division
    return match


def make_mock_user(
    user_id: int = 123456789,
    name: str = "TestCaster",
    role_ids: list[int] | None = None,
    is_admin: bool = False,
    is_manage_guild: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Member."""
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.name = name
    user.display_name = name
    user.mention = f"<@{user_id}>"

    roles = []
    for rid in role_ids or []:
        role = MagicMock(spec=discord.Role)
        role.id = rid
        roles.append(role)
    user.roles = roles

    user.guild_permissions = MagicMock()
    user.guild_permissions.administrator = is_admin
    user.guild_permissions.manage_guild = is_manage_guild
    return user


def make_mock_interaction(
    user: MagicMock | None = None,
    settings: Settings | None = None,
    caster_service: MagicMock | None = None,
    is_done: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Interaction con response y followup asíncronos."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user or make_mock_user()
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = 1547725310508667010
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel.id = 987654321

    client = MagicMock()
    client.settings = settings or Settings()
    client.caster_service = caster_service
    interaction.client = client

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(return_value=is_done)
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    interaction.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    interaction.followup = followup

    interaction.edit_original_response = AsyncMock()
    return interaction


# ---------------------------------------------------------------------------
# 1. Tests para build_match_caster_embed
# ---------------------------------------------------------------------------


class TestBuildMatchCasterEmbed:
    """Pruebas del constructor visual de embeds de partido."""

    def test_embed_title_and_footer(self) -> None:
        """Verifica que el título incluye división, jornada y equipos,
        y el pie de página es estándar.
        """
        match = make_test_match(jornada=5, team1_name="Origen", team2_name="Fnatic")
        data = MatchCastersData(has_streamer=False, streamer=None, casters=[])

        embed = build_match_caster_embed(match, data)

        assert "PREMIER" in embed.title
        assert "Jornada 5" in embed.title
        assert "Origen vs Fnatic" in embed.title
        assert embed.footer.text == "RCL · Rebel Crown Legacy"

    def test_embed_horario_with_scheduled_at(self) -> None:
        """Verifica el formateo con timestamp de Discord cuando el partido está programado."""
        dt = datetime(2026, 11, 20, 18, 30, tzinfo=timezone.utc)
        match = make_test_match(scheduled_at=dt)
        data = MatchCastersData(has_streamer=False, streamer=None, casters=[])

        embed = build_match_caster_embed(match, data)

        ts = int(dt.timestamp())
        horario_field = next(f for f in embed.fields if f.name == "Horario")
        assert horario_field.value == f"<t:{ts}:F> (<t:{ts}:R>)"

    def test_embed_horario_without_scheduled_at(self) -> None:
        """Verifica que un partido sin fecha muestra 'Por determinar'."""
        match = make_test_match(scheduled_at=None)
        data = MatchCastersData(has_streamer=False, streamer=None, casters=[])

        embed = build_match_caster_embed(match, data)

        horario_field = next(f for f in embed.fields if f.name == "Horario")
        assert horario_field.value == "*Por determinar*"

    def test_embed_streamer_solo_pc_and_partial_coverage(self) -> None:
        """Verifica la etiqueta '(Solo PC)', estado amarillo y falta de caster."""
        match = make_test_match()
        streamer = MatchCaster(
            id=uuid.uuid4(),
            match_id=match.id,
            discord_user_id=111222333,
            caster_role=CasterRole.STREAMER,
        )
        data = MatchCastersData(has_streamer=True, streamer=streamer, casters=[])

        embed = build_match_caster_embed(match, data)

        stream_field = next(f for f in embed.fields if f.name == "📺 Retransmisión")
        assert stream_field.value == "<@111222333> (Solo PC)"

        casters_field = next(f for f in embed.fields if f.name == "🎙️ Casters")
        assert casters_field.value == "*Sin casters asignados*"

        status_field = next(f for f in embed.fields if f.name == "Estado")
        assert "🟡 Falta caster (solo retransmisión)" in status_field.value
        assert embed.color == discord.Color.gold()

    def test_embed_streamer_both_counts_as_full_coverage(self) -> None:
        """Verifica que el rol BOTH muestra '(Caster + PC)' y cobertura verde completa."""
        match = make_test_match()
        user_id = 999888777
        streamer = MatchCaster(
            id=uuid.uuid4(),
            match_id=match.id,
            discord_user_id=user_id,
            caster_role=CasterRole.BOTH,
        )
        # En CasterService, un usuario con BOTH forma parte de streamer y de casters
        data = MatchCastersData(has_streamer=True, streamer=streamer, casters=[streamer])

        embed = build_match_caster_embed(match, data)

        stream_field = next(f for f in embed.fields if f.name == "📺 Retransmisión")
        assert stream_field.value == f"<@{user_id}> (Caster + PC)"

        casters_field = next(f for f in embed.fields if f.name == "🎙️ Casters")
        assert f"<@{user_id}>" in casters_field.value

        status_field = next(f for f in embed.fields if f.name == "Estado")
        assert "🟢 Cobertura lista (Streamer + Casters)" in status_field.value
        assert embed.color == discord.Color.green()

    def test_embed_full_coverage_separate_streamer_and_casters(self) -> None:
        """Verifica cobertura lista cuando hay un streamer independiente y varios casters."""
        match = make_test_match()
        streamer = MatchCaster(
            id=uuid.uuid4(),
            match_id=match.id,
            discord_user_id=100,
            caster_role=CasterRole.STREAMER,
        )
        caster1 = MatchCaster(
            id=uuid.uuid4(),
            match_id=match.id,
            discord_user_id=200,
            caster_role=CasterRole.CASTER,
        )
        caster2 = MatchCaster(
            id=uuid.uuid4(),
            match_id=match.id,
            discord_user_id=300,
            caster_role=CasterRole.CASTER,
        )
        data = MatchCastersData(has_streamer=True, streamer=streamer, casters=[caster1, caster2])

        embed = build_match_caster_embed(match, data)

        casters_field = next(f for f in embed.fields if f.name == "🎙️ Casters")
        assert "<@200>, <@300>" in casters_field.value

        status_field = next(f for f in embed.fields if f.name == "Estado")
        assert "🟢 Cobertura lista (Streamer + Casters)" in status_field.value
        assert embed.color == discord.Color.green()

    def test_embed_only_casters_partial_coverage(self) -> None:
        """Verifica estado amarillo cuando hay audio/casters pero falta retransmisión."""
        match = make_test_match()
        caster = MatchCaster(
            id=uuid.uuid4(),
            match_id=match.id,
            discord_user_id=400,
            caster_role=CasterRole.CASTER,
        )
        data = MatchCastersData(has_streamer=False, streamer=None, casters=[caster])

        embed = build_match_caster_embed(match, data)

        stream_field = next(f for f in embed.fields if f.name == "📺 Retransmisión")
        assert stream_field.value == "*Vacante (disponible)*"

        status_field = next(f for f in embed.fields if f.name == "Estado")
        assert "🟡 Falta retransmisión (solo audio)" in status_field.value
        assert embed.color == discord.Color.gold()

    def test_embed_empty_vacancy(self) -> None:
        """Verifica estado vacante blanco cuando el partido no tiene cobertura asignada."""
        match = make_test_match()
        data = MatchCastersData(has_streamer=False, streamer=None, casters=[])

        embed = build_match_caster_embed(match, data)

        status_field = next(f for f in embed.fields if f.name == "Estado")
        assert "⚪ Vacante (sin cubrir)" in status_field.value
        assert embed.color == discord.Color.blurple()

    def test_embed_division_name_from_season_division(self) -> None:
        """Verifica la resolución de nombre de división desde season_division anidada."""
        match = make_test_match()
        sd_mock = MagicMock()
        sd_mock.division = MagicMock()
        sd_mock.division.name = "ASCEND"
        match.season_division = sd_mock

        embed = build_match_caster_embed(
            match, MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )
        assert "ASCEND" in embed.title


# ---------------------------------------------------------------------------
# 2. Tests para MatchCasterView
# ---------------------------------------------------------------------------


class TestMatchCasterView:
    """Pruebas de la vista persistente y botones de acción."""

    def test_view_persistence_and_children_count(self) -> None:
        """Verifica que la vista es persistente (timeout=None) y contiene 4 botones."""
        match_id = uuid.uuid4()
        view = MatchCasterView(match_id=match_id)

        assert view.timeout is None
        assert len(view.children) == 4

        actions = [btn.action for btn in view.children]
        assert actions == ["cast", "stream", "both", "leave"]

    def test_view_buttons_enabled_when_no_streamer(self) -> None:
        """Verifica que los botones de retransmisión están habilitados si no hay streamer."""
        match_id = uuid.uuid4()
        data = MatchCastersData(has_streamer=False, streamer=None, casters=[])
        view = MatchCasterView(match_id=match_id, casters_data=data)

        assert view.btn_cast.disabled is False
        assert view.btn_stream.disabled is False
        assert view.btn_both.disabled is False
        assert view.btn_leave.disabled is False

    def test_view_buttons_disabled_when_has_streamer(self) -> None:
        """Verifica que 'stream' y 'both' se deshabilitan cuando ya hay streamer activo."""
        match_id = uuid.uuid4()
        streamer = MatchCaster(
            id=uuid.uuid4(),
            match_id=match_id,
            discord_user_id=123,
            caster_role=CasterRole.STREAMER,
        )
        data = MatchCastersData(has_streamer=True, streamer=streamer, casters=[])
        view = MatchCasterView(match_id=match_id, casters_data=data)

        assert view.btn_cast.disabled is False
        assert view.btn_stream.disabled is True
        assert view.btn_both.disabled is True
        assert view.btn_leave.disabled is False

    def test_view_custom_ids_match_specification(self) -> None:
        """Verifica que los custom_ids se construyen según el estándar
        caster:{action}:{match_id}.
        """
        match_id = uuid.uuid4()
        view = MatchCasterView(match_id=match_id)

        assert view.btn_cast.custom_id == f"caster:cast:{match_id}"
        assert view.btn_stream.custom_id == f"caster:stream:{match_id}"
        assert view.btn_both.custom_id == f"caster:both:{match_id}"
        assert view.btn_leave.custom_id == f"caster:leave:{match_id}"

    def test_view_serialization_to_components(self) -> None:
        """Verifica que view.to_components() serializa correctamente para Discord."""
        match_id = uuid.uuid4()
        view = MatchCasterView(match_id=match_id)

        components = view.to_components()
        assert len(components) == 1  # 1 ActionRow
        row_buttons = components[0]["components"]
        assert len(row_buttons) == 4
        assert row_buttons[0]["custom_id"] == f"caster:cast:{match_id}"
        assert row_buttons[0]["label"] == "Castear"
        assert row_buttons[1]["custom_id"] == f"caster:stream:{match_id}"
        assert row_buttons[1]["label"] == "Retransmitir"

    def test_view_accepts_uuid_or_str(self) -> None:
        """Verifica compatibilidad con UUID o string como argumento match_id."""
        raw_uuid = uuid.uuid4()
        view_from_uuid = MatchCasterView(match_id=raw_uuid)
        view_from_str = MatchCasterView(match_id=str(raw_uuid))

        assert view_from_uuid.match_id == raw_uuid
        assert view_from_str.match_id == raw_uuid


# ---------------------------------------------------------------------------
# 3. Tests para CasterActionButton (Regex, Deserialización y Propiedades)
# ---------------------------------------------------------------------------


class TestCasterActionButtonRegexAndProperties:
    """Pruebas del DynamicItem template y reconstrucción desde custom_id."""

    @pytest.mark.parametrize("action", ["cast", "stream", "both", "leave"])
    def test_regex_matching_valid_custom_ids(self, action: str) -> None:
        """Verifica que el patrón regex compila y extrae acción y UUID."""
        m_id = uuid.uuid4()
        custom_id = f"caster:{action}:{m_id}"

        match = re.match(
            r"^caster:(?P<action>cast|stream|both|leave):(?P<match_id>[0-9a-fA-F-]+)$",
            custom_id,
        )
        assert match is not None
        assert match["action"] == action
        assert uuid.UUID(match["match_id"]) == m_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["cast", "stream", "both", "leave"])
    async def test_from_custom_id_reconstruction(self, action: str) -> None:
        """Verifica la reconstrucción del botón dinámico desde from_custom_id."""
        m_id = uuid.uuid4()
        custom_id = f"caster:{action}:{m_id}"
        pattern = CasterActionButton.__discord_ui_compiled_template__
        regex_match = pattern.match(custom_id)
        assert regex_match is not None

        interaction = make_mock_interaction()
        mock_item = MagicMock(spec=discord.ui.Button)
        mock_item.disabled = True

        reconstructed = await CasterActionButton.from_custom_id(interaction, mock_item, regex_match)

        assert reconstructed.action == action
        assert reconstructed.match_id == m_id
        assert reconstructed.disabled is True

    def test_regex_rejects_invalid_custom_ids(self) -> None:
        """Verifica que custom_ids con acciones no permitidas
        o formatos inválidos son rechazados.
        """
        pattern = CasterActionButton.__discord_ui_compiled_template__
        m_id = uuid.uuid4()

        assert pattern.match(f"caster:unknown:{m_id}") is None
        assert pattern.match(f"other:cast:{m_id}") is None
        assert pattern.match("caster:cast:not-a-valid-uuid") is None

    def test_init_with_unknown_action_raises_value_error(self) -> None:
        """Verifica que instanciar CasterActionButton con acción desconocida genera ValueError."""
        with pytest.raises(ValueError, match="Acción de casteo desconocida"):
            CasterActionButton(action="invalid_action", match_id=uuid.uuid4())

    def test_property_delegation_to_internal_button(self) -> None:
        """Verifica que disabled, style, label y emoji se delegan a self.item."""
        btn = CasterActionButton(action="cast", match_id=uuid.uuid4(), disabled=False)

        assert btn.disabled is False
        btn.disabled = True
        assert btn.disabled is True
        assert btn.item.disabled is True

        assert btn.label == "Castear"
        btn.label = "Nuevo Label"
        assert btn.label == "Nuevo Label"
        assert btn.item.label == "Nuevo Label"

        assert btn.style == discord.ButtonStyle.primary
        btn.style = discord.ButtonStyle.danger
        assert btn.style == discord.ButtonStyle.danger
        assert btn.item.style == discord.ButtonStyle.danger

        assert str(btn.emoji) == "🎙️"
        btn.emoji = "🎧"
        assert str(btn.emoji) == "🎧"
        assert str(btn.item.emoji) == "🎧"


# ---------------------------------------------------------------------------
# 4. Tests de Integración Mock para CasterActionButton.callback
# ---------------------------------------------------------------------------


class TestCasterActionButtonCallback:
    """Pruebas del flujo de ejecución del callback en respuestas a interacciones."""

    @pytest.mark.asyncio
    async def test_callback_assign_caster_success_edits_message(self) -> None:
        """Verifica que una asignación de caster exitosa actualiza el mensaje
        in-place y envía confirmación.
        """
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)

        new_data = MatchCastersData(
            has_streamer=False,
            streamer=None,
            casters=[
                MatchCaster(
                    id=uuid.uuid4(),
                    match_id=match_id,
                    discord_user_id=123456789,
                    caster_role=CasterRole.CASTER,
                )
            ],
        )

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(success=True, action="assign", data=new_data)
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(caster_service=mock_service)
        btn = CasterActionButton(action="cast", match_id=match_id)

        await btn.callback(interaction)

        # 1. Verifica llamada al servicio con rol CASTER
        mock_service.assign_caster.assert_awaited_once_with(
            match_id, interaction.user.id, CasterRole.CASTER
        )
        # 2. Verifica edición in-place del mensaje
        interaction.response.edit_message.assert_awaited_once()
        call_kwargs = interaction.response.edit_message.call_args.kwargs
        assert isinstance(call_kwargs["embed"], discord.Embed)
        assert isinstance(call_kwargs["view"], MatchCasterView)
        # 3. Verifica confirmación efímera vía followup
        interaction.followup.send.assert_awaited_once_with(
            "✅ Tu asignación ha sido actualizada.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_callback_assign_streamer_success_disables_stream_buttons(self) -> None:
        """Verifica que asignarse como streamer edita el mensaje deshabilitando
        botones de retransmisión.
        """
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)

        streamer = MatchCaster(
            id=uuid.uuid4(),
            match_id=match_id,
            discord_user_id=123456789,
            caster_role=CasterRole.STREAMER,
        )
        new_data = MatchCastersData(has_streamer=True, streamer=streamer, casters=[])

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(success=True, action="assign", data=new_data)
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(caster_service=mock_service)
        btn = CasterActionButton(action="stream", match_id=match_id)

        await btn.callback(interaction)

        mock_service.assign_caster.assert_awaited_once_with(
            match_id, interaction.user.id, CasterRole.STREAMER
        )

        interaction.response.edit_message.assert_awaited_once()
        updated_view: MatchCasterView = interaction.response.edit_message.call_args.kwargs["view"]
        assert updated_view.btn_stream.disabled is True
        assert updated_view.btn_both.disabled is True
        assert updated_view.btn_cast.disabled is False

    @pytest.mark.asyncio
    async def test_callback_assign_both_maps_to_both_role(self) -> None:
        """Verifica que la acción 'both' delega con el enum CasterRole.BOTH."""
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)

        streamer = MatchCaster(
            id=uuid.uuid4(),
            match_id=match_id,
            discord_user_id=123456789,
            caster_role=CasterRole.BOTH,
        )
        new_data = MatchCastersData(has_streamer=True, streamer=streamer, casters=[streamer])

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(success=True, action="assign", data=new_data)
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(caster_service=mock_service)
        btn = CasterActionButton(action="both", match_id=match_id)

        await btn.callback(interaction)

        mock_service.assign_caster.assert_awaited_once_with(
            match_id, interaction.user.id, CasterRole.BOTH
        )
        interaction.response.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_callback_streamer_collision_returns_ephemeral_error(self) -> None:
        """Verifica que la colisión de retransmisión responde efímeramente
        sin alterar el mensaje.
        """
        match_id = uuid.uuid4()

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(
                success=False,
                action="assign",
                data=None,
                error="Ya hay una persona asignada a la retransmisión de este partido.",
            )
        )

        interaction = make_mock_interaction(caster_service=mock_service)
        btn = CasterActionButton(action="stream", match_id=match_id)

        await btn.callback(interaction)

        # No debe editar el mensaje en el canal
        interaction.response.edit_message.assert_not_called()
        # Debe enviar mensaje de error efímero al invocador
        interaction.response.send_message.assert_awaited_once_with(
            "❌ Ya hay una persona asignada a la retransmisión de este partido.",
            ephemeral=True,
        )

    @pytest.mark.asyncio
    async def test_callback_leave_action_restores_streamer_buttons(self) -> None:
        """Verifica que al desapuntarse un streamer, los botones de retransmisión se rehabilitan."""
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)

        empty_data = MatchCastersData(has_streamer=False, streamer=None, casters=[])

        mock_service = MagicMock()
        mock_service.remove_caster = AsyncMock(
            return_value=CasterAssignmentResult(success=True, action="remove", data=empty_data)
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(caster_service=mock_service)
        btn = CasterActionButton(action="leave", match_id=match_id)

        await btn.callback(interaction)

        mock_service.remove_caster.assert_awaited_once_with(match_id, interaction.user.id)
        interaction.response.edit_message.assert_awaited_once()

        call_view = interaction.response.edit_message.call_args.kwargs["view"]
        updated_view: MatchCasterView = call_view
        assert updated_view.btn_stream.disabled is False
        assert updated_view.btn_both.disabled is False

    @pytest.mark.asyncio
    async def test_callback_role_check_rejection_when_configured(self) -> None:
        """Verifica el rechazo efímero si se exige caster_role_id y el usuario
        no lo posee ni es staff.
        """
        settings = Settings(caster_role_id=999888)
        user = make_mock_user(role_ids=[111], is_admin=False, is_manage_guild=False)

        mock_service = MagicMock()
        interaction = make_mock_interaction(
            user=user, settings=settings, caster_service=mock_service
        )
        btn = CasterActionButton(action="cast", match_id=uuid.uuid4())

        await btn.callback(interaction)

        # No debe haber llamado al servicio
        mock_service.assign_caster.assert_not_called()
        # Debe haber rechazado con mensaje efímero
        interaction.response.send_message.assert_awaited_once_with(
            "❌ No tienes el rol necesario para apuntarte como caster.",
            ephemeral=True,
        )

    @pytest.mark.asyncio
    async def test_callback_role_check_passes_when_user_has_role(self) -> None:
        """Verifica que el usuario con el rol configurado puede apuntarse."""
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)
        settings = Settings(caster_role_id=999888)
        user = make_mock_user(role_ids=[999888], is_admin=False, is_manage_guild=False)

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(
                success=True,
                action="assign",
                data=MatchCastersData(has_streamer=False, streamer=None, casters=[]),
            )
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(
            user=user, settings=settings, caster_service=mock_service
        )
        btn = CasterActionButton(action="cast", match_id=match_id)

        await btn.callback(interaction)

        mock_service.assign_caster.assert_awaited_once()
        interaction.response.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_callback_role_check_passes_for_staff_without_role(self) -> None:
        """Verifica que miembros del staff pueden apuntarse aunque
        no tengan el rol de caster explícito.
        """
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)
        settings = Settings(caster_role_id=999888)
        user = make_mock_user(role_ids=[], is_admin=True, is_manage_guild=False)

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(
                success=True,
                action="assign",
                data=MatchCastersData(has_streamer=False, streamer=None, casters=[]),
            )
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(
            user=user, settings=settings, caster_service=mock_service
        )
        btn = CasterActionButton(action="cast", match_id=match_id)

        await btn.callback(interaction)

        mock_service.assign_caster.assert_awaited_once()
        interaction.response.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_callback_when_match_not_found(self) -> None:
        """Verifica manejo de error si el partido no se encuentra en el repositorio."""
        match_id = uuid.uuid4()
        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(
                success=True,
                action="assign",
                data=MatchCastersData(has_streamer=False, streamer=None, casters=[]),
            )
        )
        mock_service.get_match = AsyncMock(return_value=None)

        interaction = make_mock_interaction(caster_service=mock_service)
        btn = CasterActionButton(action="cast", match_id=match_id)

        await btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "❌ No se encontró la información del partido.",
            ephemeral=True,
        )

    @pytest.mark.asyncio
    async def test_callback_deferred_interaction_edits_original_response(self) -> None:
        """Verifica que si la interacción ya fue respondida (is_done=True),
        edita vía edit_original_response.
        """
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(
                success=True,
                action="assign",
                data=MatchCastersData(has_streamer=False, streamer=None, casters=[]),
            )
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(caster_service=mock_service, is_done=True)
        btn = CasterActionButton(action="cast", match_id=match_id)

        await btn.callback(interaction)

        interaction.response.edit_message.assert_not_called()
        interaction.edit_original_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_callback_internal_button_routes_to_action_button(self) -> None:
        """Verifica que llamar al callback del Button interno ejecuta el flujo completo."""
        match_id = uuid.uuid4()
        match = make_test_match(match_id=match_id)

        mock_service = MagicMock()
        mock_service.assign_caster = AsyncMock(
            return_value=CasterAssignmentResult(
                success=True,
                action="assign",
                data=MatchCastersData(has_streamer=False, streamer=None, casters=[]),
            )
        )
        mock_service.get_match = AsyncMock(return_value=match)

        interaction = make_mock_interaction(caster_service=mock_service)
        btn = CasterActionButton(action="cast", match_id=match_id)

        # Invoca el callback del item envuelto (discord.ui.Button)
        await btn.item.callback(interaction)

        mock_service.assign_caster.assert_awaited_once()
        interaction.response.edit_message.assert_awaited_once()
