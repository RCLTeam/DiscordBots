"""
Módulo de presentación de Discord para el subsistema de solicitud y asignación de roles.

Define RolesCog con:
- Registro de vistas persistentes y dynamic items en cog_load().
- Listener on_member_join para bienvenida y asignación de rol Sin Verificar.
- Slash command /pedir-rol abierto a los miembros para solicitar rol de jugador.
- Slash command /asignar-rol restringido a Staff para asignar un equipo registrado o Libre
  con el mismo resultado que confirmar un ticket.
- Slash command /publicar-panel-rol restringido a Staff para publicar el panel interactivo.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from liga_bot.cogs.permissions import is_staff
from liga_bot.config import Settings, get_settings
from liga_bot.ui.roles import (
    POSICION_DESCRIPCIONES,
    ConfirmarRolButton,
    PanelPedirRolView,
    SolicitudRolModal,
    TicketView,
    build_panel_rol_embed,
)
from liga_bot.utils.formatting import normalize_riot_tag

if TYPE_CHECKING:
    from liga_bot.bot import LigaBot

logger = logging.getLogger(__name__)

# Límites de Discord para las opciones de un comando slash.
MAX_AUTOCOMPLETE_CHOICES = 25
MAX_CHOICE_LENGTH = 100

# Mismas posiciones que el desplegable del ticket.
POSICION_CHOICES = [
    app_commands.Choice(name=posicion.value.capitalize(), value=posicion.value)
    for posicion in POSICION_DESCRIPCIONES
]


class RolesCog(commands.Cog, name="Roles"):
    """
    Cog responsable del flujo interactivo de solicitud, verificación
    y asignación administrativa de roles en la liga.
    """

    def __init__(self, bot: LigaBot | commands.Bot) -> None:
        self.bot: LigaBot | commands.Bot = bot

    @property
    def settings(self) -> Settings:
        """Resuelve de forma resiliente la configuración del bot."""
        bot_settings = getattr(self.bot, "settings", None)
        return bot_settings if bot_settings is not None else get_settings()

    async def cog_load(self) -> None:
        """
        Registra vistas persistentes y elementos dinámicos en el bot
        para garantizar resiliencia ante reinicios.
        """
        self.bot.add_view(PanelPedirRolView())
        self.bot.add_view(TicketView())
        self.bot.add_dynamic_items(ConfirmarRolButton)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Gestiona la incorporación de nuevos miembros asignando el rol sin verificar."""
        role_service = getattr(self.bot, "role_service", None)
        if role_service is not None:
            await role_service.handle_member_join(member)

    @app_commands.command(
        name="pedir-rol",
        description="Abre el modal para solicitar rol de jugador",
    )
    async def pedir_rol(self, interaction: discord.Interaction) -> None:
        """Abre el modal para solicitar rol de jugador."""
        await interaction.response.send_modal(SolicitudRolModal())

    @app_commands.command(
        name="asignar-rol",
        description="Asigna un equipo registrado o Libre a un usuario directamente (Solo Staff)",
    )
    @app_commands.describe(
        usuario="Miembro al que asignar el rol",
        equipo="Equipo registrado o Libre",
        nombre_lol="Nombre del jugador en League of Legends",
        riot_tag="Riot Tag del jugador",
        posicion="Posición en el equipo (obligatoria salvo para Libre)",
    )
    @app_commands.choices(posicion=POSICION_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def asignar_rol(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        equipo: str,
        nombre_lol: str,
        riot_tag: str,
        posicion: str | None = None,
    ) -> None:
        """Asigna un equipo registrado o Libre a un usuario directamente (Solo Staff)."""
        is_authorized = await is_staff(interaction.user, self.settings)
        if not is_authorized:
            await interaction.response.send_message(
                "Solo el staff puede asignar roles.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        if getattr(interaction.user, "id", None) == usuario.id:
            await interaction.response.send_message(
                "No puedes asignarte un rol a ti mismo.",
                ephemeral=True,
            )
            return

        role_service = getattr(self.bot, "role_service", None)
        if role_service is None:
            await interaction.response.send_message(
                "El servicio de roles no está disponible.",
                ephemeral=True,
            )
            return

        raw_tag = (riot_tag or "").replace("#", "").strip()
        clean_tag = normalize_riot_tag(riot_tag)
        if len(raw_tag) > 5 or len(clean_tag) < 3 or len(clean_tag) > 5:
            await interaction.response.send_message(
                "❌ El Riot Tag debe tener entre 3 y 5 caracteres alfanuméricos (ej: EUW o 12345).",
                ephemeral=True,
            )
            return

        # Las llamadas a Discord y a la base de datos pueden superar los 3 segundos
        # que Discord concede para la primera respuesta.
        await interaction.response.defer(ephemeral=True)

        if equipo.strip().casefold() == self.settings.free_role_name.casefold():
            _, msg = await role_service.assign_free_role(usuario, nombre_lol, clean_tag)
        else:
            _, msg = await role_service.assign_team_role(
                guild=interaction.guild,
                member=usuario,
                staff_member=interaction.user,
                equipo=equipo,
                nombre_lol=nombre_lol,
                riot_tag=clean_tag,
                posicion=posicion,
            )
        await interaction.followup.send(msg, ephemeral=True)

    @asignar_rol.autocomplete("equipo")
    async def asignar_rol_equipo_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Sugiere los equipos registrados y Libre."""
        names: list[str] = []
        role_service = getattr(self.bot, "role_service", None)
        if role_service is not None:
            try:
                names = await role_service.list_team_names()
            except Exception as exc:
                logger.warning("No se pudieron cargar los equipos para autocompletar: %s", exc)
        names = [*names, self.settings.free_role_name]
        needle = current.strip().casefold()
        return [
            app_commands.Choice(name=name, value=name)
            for name in names
            if len(name) <= MAX_CHOICE_LENGTH and needle in name.casefold()
        ][:MAX_AUTOCOMPLETE_CHOICES]

    @app_commands.command(
        name="publicar-panel-rol",
        description="Publica el panel interactivo persistente para pedir rol (Solo Staff)",
    )
    @app_commands.describe(
        canal="Canal donde publicar el panel (opcional, por defecto el canal actual)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def publicar_panel_rol(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel | None = None,
    ) -> None:
        """Publica el panel interactivo persistente para pedir rol (Solo Staff)."""
        is_authorized = await is_staff(interaction.user, self.settings)
        if not is_authorized:
            await interaction.response.send_message(
                "Solo el staff puede publicar el panel.",
                ephemeral=True,
            )
            return

        target_channel = canal or interaction.channel
        if target_channel is None or not hasattr(target_channel, "send"):
            await interaction.response.send_message(
                "No se pudo determinar un canal válido para publicar el panel.",
                ephemeral=True,
            )
            return

        await target_channel.send(embed=build_panel_rol_embed(), view=PanelPedirRolView())
        await interaction.response.send_message(
            f"Panel publicado en {target_channel.mention}.",
            ephemeral=True,
        )


async def setup(bot: LigaBot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "Roles" not in bot.cogs:
        await bot.add_cog(RolesCog(bot))  # type: ignore[arg-type]


__all__ = [
    "RolesCog",
    "setup",
]
