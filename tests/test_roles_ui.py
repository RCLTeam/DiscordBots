"""
Pruebas unitarias exhaustivas para los componentes UI desacoplados de Discord
en src/liga_bot/ui/roles.py.

Valida:
- SolicitudRolModal: Inicialización de campos de texto y flujo on_submit.
- EquipoSelect & EquipoSelectView: 21 opciones exactas, asignación libre y creación de tickets.
- PanelPedirRolView: Persistencia (timeout=None), custom_id y apertura de modal.
- ConfirmarRolButton: DynamicItem regex, from_custom_id, permisos de staff y confirmación.
- TicketView: Persistencia, botón de denegar rol, permisos de staff y denegación.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from liga_bot.config import TEAMS_ALL, Settings
from liga_bot.ui.roles import (
    ConfirmarRolButton,
    EquipoSelect,
    EquipoSelectView,
    PanelPedirRolView,
    SolicitudRolModal,
    TicketView,
)

# ---------------------------------------------------------------------------
# Mocks auxiliares para interacciones y entidades de Discord
# ---------------------------------------------------------------------------


def make_mock_user(user_id: int = 123456789, name: str = "TestUser") -> MagicMock:
    """Crea un mock de discord.Member/discord.User."""
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.name = name
    user.display_name = name
    user.mention = f"<@{user_id}>"
    user.roles = []
    user.guild_permissions = MagicMock()
    user.guild_permissions.administrator = False
    user.guild_permissions.manage_guild = False
    return user


def make_mock_channel(channel_id: int = 987654321, name: str = "rol-testuser") -> MagicMock:
    """Crea un mock de discord.TextChannel."""
    chan = MagicMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    chan.delete = AsyncMock()
    return chan


def make_mock_guild(guild_id: int = 1547725310508667010) -> MagicMock:
    """Crea un mock de discord.Guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "RCL League Server"
    guild.roles = []
    guild.get_role = MagicMock(return_value=None)
    return guild


def make_mock_role_service(free_role_name: str = "Libre") -> MagicMock:
    """Crea un mock completo de RoleService."""
    service = MagicMock()
    service.settings = Settings(free_role_name=free_role_name)
    service.assign_free_role = AsyncMock(return_value=(True, "Rol Libre asignado correctamente."))
    service.create_role_request_ticket = AsyncMock(
        return_value=(True, "Canal de solicitud creado correctamente.", make_mock_channel())
    )
    service.confirm_role_request = AsyncMock(
        return_value=(True, "Rol Vanguard Gaming confirmado para TestUser.")
    )
    service.deny_role_request = AsyncMock(return_value=(True, "Solicitud de rol denegada."))
    return service


def make_mock_interaction(
    user: MagicMock | None = None,
    guild: MagicMock | None = None,
    channel: MagicMock | None = None,
    role_service: MagicMock | None = None,
) -> MagicMock:
    """Crea un mock de discord.Interaction con response y followup asíncronos."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user or make_mock_user()
    interaction.guild = guild or make_mock_guild()
    interaction.channel = channel or make_mock_channel()
    interaction.channel_id = interaction.channel.id

    client = MagicMock()
    client.role_service = role_service or make_mock_role_service()
    interaction.client = client

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(return_value=False)
    response.send_message = AsyncMock()
    response.defer = AsyncMock()
    response.send_modal = AsyncMock()
    response.edit_message = AsyncMock()
    interaction.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    interaction.followup = followup

    return interaction


# ---------------------------------------------------------------------------
# 1. Pruebas de SolicitudRolModal
# ---------------------------------------------------------------------------


class TestSolicitudRolModal:
    """Pruebas para el modal de solicitud de rol."""

    def test_modal_fields_initialization(self) -> None:
        """Verifica la configuración de campos, etiquetas y restricciones de longitud."""
        modal = SolicitudRolModal()

        assert modal.title == "Solicitud de Rol de Jugador"

        # Campo nombre_lol
        assert hasattr(modal, "nombre_lol")
        nombre_label = getattr(modal.nombre_lol, "_underlying", modal.nombre_lol).label
        assert nombre_label == "Nombre en League of Legends"
        assert modal.nombre_lol.min_length == 1
        assert modal.nombre_lol.max_length == 100
        assert modal.nombre_lol.required is True

        # Campo riot_tag
        assert hasattr(modal, "riot_tag")
        riot_label = getattr(modal.riot_tag, "_underlying", modal.riot_tag).label
        assert riot_label == "Riot Tag"
        assert modal.riot_tag.min_length == 1
        assert modal.riot_tag.max_length == 20
        assert modal.riot_tag.required is True

    @pytest.mark.asyncio
    async def test_modal_on_submit_sends_equipo_select_view(self) -> None:
        """Verifica que on_submit envíe un mensaje efímero con EquipoSelectView."""
        modal = SolicitudRolModal()
        modal.nombre_lol._value = "Faker"
        modal.riot_tag._value = "KR1"

        interaction = make_mock_interaction()

        await modal.on_submit(interaction)

        interaction.response.send_message.assert_awaited_once()
        call_args, call_kwargs = interaction.response.send_message.call_args

        assert "Selecciona tu equipo" in call_args[0]
        assert call_kwargs.get("ephemeral") is True

        view = call_kwargs.get("view")
        assert isinstance(view, EquipoSelectView)
        assert view.select.nombre_lol == "Faker"
        assert view.select.riot_tag == "KR1"


# ---------------------------------------------------------------------------
# 2. Pruebas de EquipoSelect y EquipoSelectView
# ---------------------------------------------------------------------------


class TestEquipoSelect:
    """Pruebas para el selector de equipo y su vista contenedora."""

    def test_equipo_select_has_21_options(self) -> None:
        """Valida que el select contenga exactamente 21 opciones (20 equipos + Libre)."""
        select = EquipoSelect(nombre_lol="PlayerOne", riot_tag="EUW", free_role_name="Libre")

        assert len(select.options) == 21
        assert select.min_values == 1
        assert select.max_values == 1

        option_values = [opt.value for opt in select.options]

        # Validar los 20 equipos oficiales
        for team in TEAMS_ALL:
            assert team in option_values

        # Validar la opción de Agente Libre
        assert "Libre" in option_values
        free_opt = next(opt for opt in select.options if opt.value == "Libre")
        assert str(free_opt.emoji) == "🕊️"

    def test_equipo_select_custom_free_role_name(self) -> None:
        """Valida que un free_role_name personalizado se incluya correctamente en las opciones."""
        select = EquipoSelect(nombre_lol="PlayerTwo", riot_tag="LAN", free_role_name="FreeAgent")

        assert len(select.options) == 21
        option_values = [opt.value for opt in select.options]
        assert "FreeAgent" in option_values
        assert "Libre" not in option_values

    def test_equipo_select_view_initialization(self) -> None:
        """Valida que EquipoSelectView configure el timeout a 180s y contenga EquipoSelect."""
        view = EquipoSelectView(nombre_lol="Player", riot_tag="123")

        assert view.timeout == 180.0
        assert len(view.children) == 1
        assert isinstance(view.children[0], EquipoSelect)
        assert view.children[0].nombre_lol == "Player"
        assert view.children[0].riot_tag == "123"

    @pytest.mark.asyncio
    async def test_callback_free_agent_assignment_not_done(self) -> None:
        """Valida la asignación directa de Agente Libre cuando response.is_done() es False."""
        service = make_mock_role_service(free_role_name="Libre")
        service.assign_free_role = AsyncMock(
            return_value=(True, "Rol Libre asignado correctamente.")
        )
        interaction = make_mock_interaction(role_service=service)
        interaction.response.is_done.return_value = False

        select = EquipoSelect(nombre_lol="Invocador", riot_tag="EUW1", free_role_name="Libre")
        select.values = ["Libre"]

        await select.callback(interaction)

        service.assign_free_role.assert_awaited_once_with(interaction.user, "Invocador", "EUW1")
        interaction.response.send_message.assert_awaited_once_with(
            "Rol Libre asignado correctamente.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_callback_free_agent_assignment_already_done(self) -> None:
        """Valida la asignación de Agente Libre usando followup si response.is_done() es True."""
        service = make_mock_role_service(free_role_name="Libre")
        service.assign_free_role = AsyncMock(
            return_value=(True, "Rol Libre asignado correctamente.")
        )
        interaction = make_mock_interaction(role_service=service)
        interaction.response.is_done.return_value = True

        select = EquipoSelect(nombre_lol="Invocador", riot_tag="EUW1", free_role_name="Libre")
        select.values = ["Libre"]

        await select.callback(interaction)

        service.assign_free_role.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "Rol Libre asignado correctamente.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_callback_team_ticket_creation_success(self) -> None:
        """Valida la creación exitosa de ticket de equipo con ConfirmarRolButton y TicketView."""
        ticket_channel = make_mock_channel(channel_id=444, name="rol-faker")
        service = make_mock_role_service()
        service.create_role_request_ticket = AsyncMock(
            return_value=(True, "Canal creado.", ticket_channel)
        )
        interaction = make_mock_interaction(role_service=service)

        select = EquipoSelect(nombre_lol="Faker", riot_tag="KR1")
        select.values = ["Vanguard Gaming"]

        await select.callback(interaction)

        # 1. Defer efímero
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)

        # 2. Creación de ticket en RoleService
        service.create_role_request_ticket.assert_awaited_once_with(
            interaction.guild, interaction.user, "Faker", "KR1", "Vanguard Gaming"
        )

        # 3. Prompt en el canal del ticket con vista de ticket
        ticket_channel.send.assert_awaited_once()
        _, send_kwargs = ticket_channel.send.call_args
        view = send_kwargs.get("view")
        assert isinstance(view, TicketView)
        assert view.user_id == interaction.user.id
        assert view.equipo == "Vanguard Gaming"

        # Verificar que el botón de confirmación esté presente en la vista del ticket
        assert any(
            isinstance(child, ConfirmarRolButton) and child.equipo == "Vanguard Gaming"
            for child in view.children
        )

        # 4. Followup en la interacción informando al usuario
        interaction.followup.send.assert_awaited_once_with(
            f"Ticket creado en {ticket_channel.mention}.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_callback_team_ticket_creation_failure(self) -> None:
        """Valida que si falla la creación del ticket se envíe el mensaje de error por followup."""
        service = make_mock_role_service()
        service.create_role_request_ticket = AsyncMock(
            return_value=(False, "Ya tienes una solicitud de rol pendiente.", None)
        )
        interaction = make_mock_interaction(role_service=service)

        select = EquipoSelect(nombre_lol="Faker", riot_tag="KR1")
        select.values = ["Vanguard Gaming"]

        await select.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            "Ya tienes una solicitud de rol pendiente.", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_callback_without_role_service_reports_error(self) -> None:
        """Valida manejo graceful cuando el bot no tiene inyectado role_service."""
        interaction = make_mock_interaction()
        interaction.client.role_service = None

        select = EquipoSelect(nombre_lol="Faker", riot_tag="KR1")
        select.values = ["Vanguard Gaming"]

        await select.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "El servicio de roles no está disponible.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# 3. Pruebas de PanelPedirRolView
# ---------------------------------------------------------------------------


class TestPanelPedirRolView:
    """Pruebas para la vista persistente del panel de solicitud de rol."""

    def test_panel_view_persistence_and_button_properties(self) -> None:
        """Valida que la vista no tenga timeout y posea el botón con custom_id canónico."""
        view = PanelPedirRolView()

        assert view.timeout is None
        assert len(view.children) == 1

        btn = view.children[0]
        assert isinstance(btn, discord.ui.Button)
        assert btn.custom_id == "solicitud_rol:panel_pedir_rol"
        assert btn.label == "Pedir Rol"
        assert btn.style == discord.ButtonStyle.primary

    @pytest.mark.asyncio
    async def test_panel_view_button_opens_modal(self) -> None:
        """Valida que al pulsar el botón se invoque send_modal con SolicitudRolModal."""
        view = PanelPedirRolView()
        interaction = make_mock_interaction()

        btn = view.children[0]
        await btn.callback(interaction)

        interaction.response.send_modal.assert_awaited_once()
        modal_arg = interaction.response.send_modal.call_args[0][0]
        assert isinstance(modal_arg, SolicitudRolModal)


# ---------------------------------------------------------------------------
# 4. Pruebas de ConfirmarRolButton (DynamicItem)
# ---------------------------------------------------------------------------


class TestConfirmarRolButton:
    """Pruebas para el DynamicItem persistente ConfirmarRolButton."""

    def test_dynamic_item_template_and_custom_id_format(self) -> None:
        """Valida que el template regex coincida con el custom_id generado."""
        btn = ConfirmarRolButton(user_id=123456789, equipo="Nexus Esports")

        assert btn.user_id == 123456789
        assert btn.equipo == "Nexus Esports"
        assert btn.custom_id == "confirmar_rol:123456789:Nexus Esports"
        assert btn.item.label == "Confirmar Rol"
        assert btn.item.style == discord.ButtonStyle.success

        # El template debe coincidir con el custom_id
        match = btn.template.match(btn.custom_id)
        assert match is not None
        assert match.group("user_id") == "123456789"
        assert match.group("equipo") == "Nexus Esports"

    def test_dynamic_item_template_rejects_invalid_custom_ids(self) -> None:
        """Valida que custom_ids inválidos no coincidan con la plantilla."""
        template = ConfirmarRolButton.__discord_ui_compiled_template__

        assert template.match("confirmar_rol:no_digits:Nexus Esports") is None
        assert template.match("denegar_rol:123456789:Nexus Esports") is None
        assert template.match("confirmar_rol:") is None

    @pytest.mark.asyncio
    async def test_from_custom_id_deserialization(self) -> None:
        """Valida la reconstrucción del botón a partir de la expresión regular."""
        custom_id = "confirmar_rol:9876543210:Storm Legion"
        match = ConfirmarRolButton.__discord_ui_compiled_template__.match(custom_id)
        assert match is not None

        button = await ConfirmarRolButton.from_custom_id(None, None, match)

        assert isinstance(button, ConfirmarRolButton)
        assert button.user_id == 9876543210
        assert button.equipo == "Storm Legion"
        assert button.custom_id == custom_id

    @pytest.mark.asyncio
    async def test_callback_denied_for_non_staff(self) -> None:
        """Valida que un usuario sin rol de staff reciba un mensaje efímero de denegación."""
        btn = ConfirmarRolButton(user_id=123, equipo="Aegis Club")
        interaction = make_mock_interaction()

        with patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff:
            mock_is_staff.return_value = False

            await btn.callback(interaction)

            mock_is_staff.assert_awaited_once_with(interaction.user)
            interaction.response.send_message.assert_awaited_once_with(
                "Solo el staff puede confirmar solicitudes de rol.", ephemeral=True
            )
            interaction.client.role_service.confirm_role_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_confirmed_for_staff_success(self) -> None:
        """Valida que el staff confirme el rol y programe la eliminación del canal."""
        btn = ConfirmarRolButton(user_id=123, equipo="Aegis Club")
        channel = make_mock_channel(channel_id=888)
        service = make_mock_role_service()
        service.confirm_role_request = AsyncMock(
            return_value=(True, "Rol Aegis Club confirmado para Jugador.")
        )
        interaction = make_mock_interaction(channel=channel, role_service=service)

        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(btn, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await btn.callback(interaction)

            service.confirm_role_request.assert_awaited_once_with(
                interaction.guild, 888, interaction.user
            )
            interaction.response.send_message.assert_awaited_once()
            msg_content = interaction.response.send_message.call_args[0][0]
            assert "Rol Aegis Club confirmado" in msg_content
            assert "5 segundos" in msg_content

            mock_schedule.assert_called_once_with(channel)

    @pytest.mark.asyncio
    async def test_callback_confirmed_for_staff_failure(self) -> None:
        """Valida que un fallo en confirm_role_request envíe mensaje de error efímero."""
        btn = ConfirmarRolButton(user_id=123, equipo="Aegis Club")
        channel = make_mock_channel(channel_id=888)
        service = make_mock_role_service()
        service.confirm_role_request = AsyncMock(
            return_value=(False, "No hay solicitud pendiente asociada a este canal.")
        )
        interaction = make_mock_interaction(channel=channel, role_service=service)

        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(btn, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await btn.callback(interaction)

            interaction.response.send_message.assert_awaited_once_with(
                "❌ No hay solicitud pendiente asociada a este canal.", ephemeral=True
            )
            mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_channel_later_executes_delete(self) -> None:
        """Valida que _delete_channel_later espere el delay y elimine el canal."""
        btn = ConfirmarRolButton(user_id=123, equipo="Aegis Club")
        channel = make_mock_channel(channel_id=999)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await btn._delete_channel_later(channel, delay=0.01)

            mock_sleep.assert_awaited_once_with(0.01)
            channel.delete.assert_awaited_once_with(reason="Solicitud de rol confirmada")


# ---------------------------------------------------------------------------
# 5. Pruebas de TicketView
# ---------------------------------------------------------------------------


class TestTicketView:
    """Pruebas para la vista persistente del canal de ticket."""

    def test_ticket_view_empty_initialization(self) -> None:
        """Valida la inicialización por defecto (registro en cog_load sin argumentos)."""
        view = TicketView()

        assert view.timeout is None
        assert view.user_id is None
        assert view.equipo is None
        assert len(view.children) == 1

        deny_btn = view.children[0]
        assert isinstance(deny_btn, discord.ui.Button)
        assert deny_btn.custom_id == "solicitud_rol:ticket_view:denegar"
        assert deny_btn.label == "Denegar Rol"
        assert deny_btn.style == discord.ButtonStyle.danger

    def test_ticket_view_initialization_with_user_and_equipo(self) -> None:
        """Valida que al pasar user_id y equipo se añada automáticamente ConfirmarRolButton."""
        view = TicketView(user_id=12345, equipo="Titan Gaming")

        assert view.user_id == 12345
        assert view.equipo == "Titan Gaming"
        assert len(view.children) == 2

        # Comprobar ambos botones
        custom_ids = [child.custom_id for child in view.children]
        assert "solicitud_rol:ticket_view:denegar" in custom_ids
        assert "confirmar_rol:12345:Titan Gaming" in custom_ids

    @pytest.mark.asyncio
    async def test_denegar_rol_denied_for_non_staff(self) -> None:
        """Valida que un no-staff no pueda denegar la solicitud."""
        view = TicketView()
        interaction = make_mock_interaction()

        with patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff:
            mock_is_staff.return_value = False

            button = view.children[0]
            await button.callback(interaction)

            mock_is_staff.assert_awaited_once_with(interaction.user)
            interaction.response.send_message.assert_awaited_once_with(
                "Solo el staff puede denegar solicitudes de rol.", ephemeral=True
            )
            interaction.client.role_service.deny_role_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_denegar_rol_staff_success(self) -> None:
        """Valida que un miembro de staff pueda denegar la solicitud y programar el borrado."""
        view = TicketView()
        channel = make_mock_channel(channel_id=555)
        service = make_mock_role_service()
        service.deny_role_request = AsyncMock(return_value=(True, "Solicitud de rol denegada."))
        interaction = make_mock_interaction(channel=channel, role_service=service)

        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(view, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            button = view.children[0]
            await button.callback(interaction)

            service.deny_role_request.assert_awaited_once_with(
                interaction.guild, 555, interaction.user
            )
            interaction.response.send_message.assert_awaited_once()
            msg_content = interaction.response.send_message.call_args[0][0]
            assert "Solicitud de rol denegada." in msg_content
            assert "5 segundos" in msg_content

            mock_schedule.assert_called_once_with(channel)

    @pytest.mark.asyncio
    async def test_denegar_rol_staff_failure(self) -> None:
        """Valida el manejo de error si deny_role_request falla."""
        view = TicketView()
        channel = make_mock_channel(channel_id=555)
        service = make_mock_role_service()
        service.deny_role_request = AsyncMock(
            return_value=(False, "No hay solicitud pendiente asociada a este canal.")
        )
        interaction = make_mock_interaction(channel=channel, role_service=service)

        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(view, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            button = view.children[0]
            await button.callback(interaction)

            interaction.response.send_message.assert_awaited_once_with(
                "Error: No hay solicitud pendiente asociada a este canal.", ephemeral=True
            )
            mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_channel_later_executes_delete(self) -> None:
        """Valida que _delete_channel_later en TicketView espere y elimine el canal."""
        view = TicketView()
        channel = make_mock_channel(channel_id=777)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await view._delete_channel_later(channel, delay=0.02)

            mock_sleep.assert_awaited_once_with(0.02)
            channel.delete.assert_awaited_once_with(reason="Solicitud de rol denegada")
