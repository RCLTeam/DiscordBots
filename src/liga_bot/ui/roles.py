"""
Componentes de interfaz de usuario desacoplados de Discord para LigaBot.

Define modales, selectores desplegables, vistas persistentes y elementos dinámicos
(DynamicItem) para el subsistema de solicitud y verificación de roles de jugadores:
- SolicitudRolModal: Modal para recopilar nombre de invocador y Riot Tag.
- EquipoSelect & EquipoSelectView: Desplegable con 21 opciones (20 equipos oficiales + Libre).
- PanelPedirRolView: Vista persistente con botón de solicitud de rol.
- ConfirmarRolButton: Botón dinámico (DynamicItem) persistente ante reinicios del bot.
- TicketView: Vista persistente con botón para denegar solicitudes de rol.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import TYPE_CHECKING, Any

import discord

from liga_bot.config import TEAMS_ALL, get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Modal de Solicitud de Rol
# ---------------------------------------------------------------------------


class SolicitudRolModal(discord.ui.Modal, title="Solicitud de Rol de Jugador"):
    """
    Modal interactivo para solicitar rol de jugador en la liga.
    Recopila el nombre en League of Legends y el Riot Tag.
    """

    nombre_lol: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Nombre en League of Legends",
        placeholder="Ej: Invocador123",
        min_length=1,
        max_length=100,
        required=True,
    )
    riot_tag: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Riot Tag",
        placeholder="Ej: EUW o 1234",
        min_length=1,
        max_length=20,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """
        Al enviar el modal, abre el menú desplegable para que el usuario
        elija su equipo o se postule como agente libre.
        """
        await interaction.response.send_message(
            "Selecciona tu equipo:",
            view=EquipoSelectView(
                nombre_lol=self.nombre_lol.value,
                riot_tag=self.riot_tag.value,
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# 2. Desplegable de Equipos y Vista Contenedora
# ---------------------------------------------------------------------------


class EquipoSelect(discord.ui.Select[Any]):
    """
    Menú desplegable con 21 opciones:
    - 20 equipos oficiales de la liga (TEAMS_ALL).
    - 1 opción de Agente Libre (free_role_name, por defecto 'Libre').
    """

    def __init__(
        self,
        nombre_lol: str,
        riot_tag: str,
        free_role_name: str | None = None,
    ) -> None:
        resolved_free_role = free_role_name or get_settings().free_role_name
        self.nombre_lol: str = nombre_lol
        self.riot_tag: str = riot_tag
        self.free_role_name: str = resolved_free_role

        options: list[discord.SelectOption] = [
            discord.SelectOption(
                label=equipo,
                value=equipo,
                description=f"Solicitar rol para {equipo}",
            )
            for equipo in TEAMS_ALL
        ]
        options.append(
            discord.SelectOption(
                label=resolved_free_role,
                value=resolved_free_role,
                emoji="🕊️",
                description="Agente libre sin equipo asignado",
            )
        )

        super().__init__(
            placeholder="Selecciona tu equipo o agente libre...",
            min_values=1,
            max_values=1,
            options=options,
        )

    @property
    def values(self) -> list[str]:
        """Lista de valores seleccionados por el usuario."""
        return self._values

    @values.setter
    def values(self, val: list[str]) -> None:
        self._values = val

    async def callback(self, interaction: discord.Interaction) -> None:
        """
        Procesa la selección de equipo del usuario:
        - Si selecciona Agente Libre: asigna el rol 'Libre' directamente.
        - Si selecciona un equipo: crea un ticket privado de verificación.
        """
        if not self.values:
            return

        equipo = self.values[0]
        role_service = getattr(interaction.client, "role_service", None)
        if role_service is None:
            await interaction.response.send_message(
                "El servicio de roles no está disponible.",
                ephemeral=True,
            )
            return

        if equipo == role_service.settings.free_role_name:
            ok, msg = await role_service.assign_free_role(
                interaction.user, self.nombre_lol, self.riot_tag
            )
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True)
            ok, msg, channel = await role_service.create_role_request_ticket(
                interaction.guild,
                interaction.user,
                self.nombre_lol,
                self.riot_tag,
                equipo,
            )
            if ok and channel is not None:
                embed = discord.Embed(
                    title="Nueva Solicitud de Rol",
                    description=(
                        f"**Usuario:** {interaction.user.mention}\n"
                        f"**Nombre en LoL:** {self.nombre_lol}\n"
                        f"**Riot Tag:** {self.riot_tag}\n"
                        f"**Equipo solicitado:** {equipo}\n\n"
                        "Un miembro del staff revisará la solicitud y confirmará o denegará el rol."
                    ),
                    color=discord.Color.blue(),
                )
                embed.set_footer(text="Usa los botones de abajo para gestionar la solicitud.")
                ticket_view = TicketView(user_id=interaction.user.id, equipo=equipo)
                await channel.send(
                    content=f"Solicitud de rol para {interaction.user.mention}:",
                    embed=embed,
                    view=ticket_view,
                )
                await interaction.followup.send(
                    f"Ticket creado en {channel.mention}.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(msg, ephemeral=True)


class EquipoSelectView(discord.ui.View):
    """
    Vista efímera que contiene el menú desplegable EquipoSelect.
    Timeout por defecto de 180 segundos.
    """

    def __init__(
        self,
        nombre_lol: str,
        riot_tag: str,
        timeout: float | None = 180.0,
        free_role_name: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.select: EquipoSelect = EquipoSelect(
            nombre_lol=nombre_lol,
            riot_tag=riot_tag,
            free_role_name=free_role_name,
        )
        self.add_item(self.select)


# ---------------------------------------------------------------------------
# 3. Vista de Panel de Bienvenida (Persistente)
# ---------------------------------------------------------------------------


class PanelPedirRolView(discord.ui.View):
    """
    Vista persistente (timeout=None) publicada en el canal de bienvenida / pedir-rol.
    Permite abrir el modal de solicitud de rol a cualquier miembro.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Pedir Rol",
        style=discord.ButtonStyle.primary,
        emoji="🎮",
        custom_id="solicitud_rol:panel_pedir_rol",
    )
    async def pedir_rol(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        """Abre el modal de solicitud de rol."""
        await interaction.response.send_modal(SolicitudRolModal())


# ---------------------------------------------------------------------------
# 4. Botón Dinámico Confirmar Rol (DynamicItem)
# ---------------------------------------------------------------------------


class ConfirmarRolButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"confirmar_rol:(?P<user_id>\d+):(?P<equipo>.+)",
):
    """
    Botón interactivo persistente ante reinicios del bot mediante DynamicItem.
    Codifica en su custom_id el user_id del solicitante y el equipo pedido:
    confirmar_rol:{user_id}:{equipo}
    """

    def __init__(self, user_id: int, equipo: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Confirmar Rol",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"confirmar_rol:{user_id}:{equipo}",
            )
        )
        self.user_id: int = user_id
        self.equipo: str = equipo

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> ConfirmarRolButton:
        """Reconstruye el botón a partir del match regex de custom_id."""
        return cls(user_id=int(match["user_id"]), equipo=match["equipo"])

    async def callback(self, interaction: discord.Interaction) -> None:
        """
        Callback de confirmación de rol:
        - Valida que el invocador sea miembro del staff.
        - Delega la asignación del rol, actualización de apodo y persistencia a RoleService.
        - Notifica la confirmación en el canal y programa su eliminación tras 5 segundos.
        """
        from liga_bot.cogs.permissions import is_staff

        staff_check = is_staff(interaction.user)
        is_authorized = await staff_check if inspect.isawaitable(staff_check) else bool(staff_check)
        if not is_authorized:
            await interaction.response.send_message(
                "Solo el staff puede confirmar solicitudes de rol.",
                ephemeral=True,
            )
            return

        role_service = getattr(interaction.client, "role_service", None)
        if role_service is None:
            await interaction.response.send_message(
                "El servicio de roles no está disponible.",
                ephemeral=True,
            )
            return

        channel_id = getattr(interaction, "channel_id", None) or (
            interaction.channel.id if interaction.channel else 0
        )
        ok, msg = await role_service.confirm_role_request(
            interaction.guild, channel_id, interaction.user
        )
        if ok:
            await interaction.response.send_message(
                f"✅ {msg}\nEste canal se eliminará en 5 segundos."
            )
            self._schedule_deletion(interaction.channel)
        else:
            await interaction.response.send_message(
                f"❌ {msg}",
                ephemeral=True,
            )

    def _schedule_deletion(
        self, channel: discord.abc.GuildChannel | None, delay: float = 5.0
    ) -> asyncio.Task[None] | None:
        """Programa la eliminación asíncrona del canal del ticket."""
        if channel is None:
            return None
        return asyncio.create_task(self._delete_channel_later(channel, delay))

    async def _delete_channel_later(
        self, channel: discord.abc.GuildChannel | None, delay: float = 5.0
    ) -> None:
        """Elimina el canal tras el retraso configurado."""
        if channel is None:
            return
        await asyncio.sleep(delay)
        try:
            await channel.delete(reason="Solicitud de rol confirmada")
        except Exception as exc:
            logger.warning("Error al eliminar canal tras confirmar rol: %s", exc)


# ---------------------------------------------------------------------------
# 5. Vista de Ticket (Persistente)
# ---------------------------------------------------------------------------


class TicketView(discord.ui.View):
    """
    Vista persistente (timeout=None) asociada a un canal de ticket de solicitud de rol.
    Contiene el botón para denegar la solicitud y opcionalmente añade ConfirmarRolButton
    cuando se inicializa con user_id y equipo.
    """

    def __init__(self, user_id: int | None = None, equipo: str | None = None) -> None:
        super().__init__(timeout=None)
        self.user_id: int | None = user_id
        self.equipo: str | None = equipo
        if user_id is not None and equipo is not None:
            self.confirm_button: ConfirmarRolButton | None = ConfirmarRolButton(
                user_id=user_id, equipo=equipo
            )
            self.add_item(self.confirm_button)
        else:
            self.confirm_button = None

    @discord.ui.button(
        label="Denegar Rol",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="solicitud_rol:ticket_view:denegar",
    )
    async def denegar_rol(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        """
        Callback para denegar la solicitud de rol:
        - Valida que el invocador sea miembro del staff.
        - Delega la denegación y actualización transaccional a RoleService.
        - Notifica la denegación en el canal y programa su eliminación tras 5 segundos.
        """
        from liga_bot.cogs.permissions import is_staff

        staff_check = is_staff(interaction.user)
        is_authorized = await staff_check if inspect.isawaitable(staff_check) else bool(staff_check)
        if not is_authorized:
            await interaction.response.send_message(
                "Solo el staff puede denegar solicitudes de rol.",
                ephemeral=True,
            )
            return

        role_service = getattr(interaction.client, "role_service", None)
        if role_service is None:
            await interaction.response.send_message(
                "El servicio de roles no está disponible.",
                ephemeral=True,
            )
            return

        channel_id = getattr(interaction, "channel_id", None) or (
            interaction.channel.id if interaction.channel else 0
        )
        ok, msg = await role_service.deny_role_request(
            interaction.guild, channel_id, interaction.user
        )
        if ok:
            await interaction.response.send_message(
                f"❌ {msg}\nEste canal se eliminará en 5 segundos."
            )
            self._schedule_deletion(interaction.channel)
        else:
            await interaction.response.send_message(
                f"Error: {msg}",
                ephemeral=True,
            )

    def _schedule_deletion(
        self, channel: discord.abc.GuildChannel | None, delay: float = 5.0
    ) -> asyncio.Task[None] | None:
        """Programa la eliminación asíncrona del canal del ticket."""
        if channel is None:
            return None
        return asyncio.create_task(self._delete_channel_later(channel, delay))

    async def _delete_channel_later(
        self, channel: discord.abc.GuildChannel | None, delay: float = 5.0
    ) -> None:
        """Elimina el canal tras el retraso configurado."""
        if channel is None:
            return
        await asyncio.sleep(delay)
        try:
            await channel.delete(reason="Solicitud de rol denegada")
        except Exception as exc:
            logger.warning("Error al eliminar canal tras denegar rol: %s", exc)


__all__ = [
    "ConfirmarRolButton",
    "EquipoSelect",
    "EquipoSelectView",
    "PanelPedirRolView",
    "SolicitudRolModal",
    "TicketView",
]
