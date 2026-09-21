"""
Módulo de presentación de Discord para el subsistema de solicitud y asignación de roles.

Define RolesCog con:
- Registro de vistas persistentes y dynamic items en cog_load().
- Listener on_member_join para bienvenida y asignación de rol Sin Verificar.
- Slash command /pedir-rol abierto a los miembros para solicitar rol de jugador.
- Slash command /asignar-rol restringido a Staff para asignación directa oficial o Libre.
- Slash command /publicar-panel-rol restringido a Staff para publicar el panel interactivo.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from liga_bot.cogs.permissions import is_staff
from liga_bot.config import Settings, get_settings
from liga_bot.database import transactional_session
from liga_bot.models.enums import RoleRequestStatus
from liga_bot.repositories.role_request_repo import RoleRequestRepository
from liga_bot.ui.roles import (
    ConfirmarRolButton,
    PanelPedirRolView,
    SolicitudRolModal,
    TicketView,
)

if TYPE_CHECKING:
    from liga_bot.bot import LigaBot

logger = logging.getLogger(__name__)


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
        description="Asigna un rol oficial o Libre a un usuario directamente (Solo Staff)",
    )
    @app_commands.describe(
        usuario="Miembro al que asignar el rol",
        equipo="Nombre del equipo oficial o Libre",
        nombre_lol="Nombre del jugador en League of Legends",
        riot_tag="Riot Tag del jugador",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def asignar_rol(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        equipo: str,
        nombre_lol: str,
        riot_tag: str,
    ) -> None:
        """Asigna un rol oficial o Libre a un usuario directamente (Solo Staff)."""
        staff_check = is_staff(interaction.user, self.settings)
        is_authorized = await staff_check if inspect.isawaitable(staff_check) else bool(staff_check)
        if not is_authorized:
            await interaction.response.send_message(
                "Solo el staff puede asignar roles.",
                ephemeral=True,
            )
            return

        role_service = getattr(self.bot, "role_service", None)

        if equipo == self.settings.free_role_name:
            if role_service is None:
                await interaction.response.send_message(
                    "El servicio de roles no está disponible.",
                    ephemeral=True,
                )
                return
            ok, msg = await role_service.assign_free_role(usuario, nombre_lol, riot_tag)
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        team_role = discord.utils.get(interaction.guild.roles, name=equipo)
        if team_role is None:
            await interaction.response.send_message(
                f"El rol '{equipo}' no existe en el servidor.",
                ephemeral=True,
            )
            return

        # Remover rol 'Sin Verificar' si está configurado y el usuario lo tiene
        if self.settings.sin_verificar_role_id > 0:
            sin_verificar = discord.utils.get(
                interaction.guild.roles, id=self.settings.sin_verificar_role_id
            )
            if sin_verificar is None:
                sin_verificar = interaction.guild.get_role(self.settings.sin_verificar_role_id)
            if sin_verificar is not None and sin_verificar in usuario.roles:
                try:
                    await usuario.remove_roles(sin_verificar)
                except Exception as exc:
                    logger.warning(
                        "No se pudo remover el rol sin verificar de %s: %s",
                        usuario.display_name,
                        exc,
                    )

        # Asignar rol del equipo
        try:
            await usuario.add_roles(team_role)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"Permisos insuficientes para asignar el rol '{equipo}'.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Error al asignar el rol '{equipo}': {exc}",
                ephemeral=True,
            )
            return

        # Actualizar apodo (hasta 32 caracteres)
        nick = f"{nombre_lol} #{riot_tag}"[:32]
        try:
            await usuario.edit(nick=nick)
        except Exception as exc:
            logger.warning(
                "No se pudo actualizar el apodo de %s a '%s': %s",
                usuario.display_name,
                nick,
                exc,
            )

        # Registrar en base de datos si role_service tiene session_factory
        if role_service is not None and getattr(role_service, "session_factory", None) is not None:
            try:
                async with transactional_session(role_service.session_factory) as session:
                    repo = RoleRequestRepository(session)
                    req = await repo.create_request(
                        user_id=usuario.id,
                        nombre_lol=nombre_lol,
                        riot_tag=riot_tag,
                        equipo=equipo,
                        canal_id=None,
                    )
                    staff_id = getattr(interaction.user, "id", None)
                    await repo.update_status(
                        request_id=req.id,
                        estado=RoleRequestStatus.APPROVED,
                        staff_id=staff_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Error al registrar solicitud de rol en base de datos para %s: %s",
                    usuario.display_name,
                    exc,
                )

        await interaction.response.send_message(
            f"Rol {equipo} asignado a {usuario.display_name} correctamente.",
            ephemeral=True,
        )

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
        staff_check = is_staff(interaction.user, self.settings)
        is_authorized = await staff_check if inspect.isawaitable(staff_check) else bool(staff_check)
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

        embed = discord.Embed(
            title="Solicitud de Rol de Jugador",
            description="Haz clic en el botón inferior para solicitar tu rol en la liga...",
            color=discord.Color.blue(),
        )
        await target_channel.send(embed=embed, view=PanelPedirRolView())
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
