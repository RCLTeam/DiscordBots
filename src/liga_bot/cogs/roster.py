"""
Módulo de presentación de Discord para la sincronización de plantillas de equipos
y gestión interactiva de posiciones de plantilla con RCL-Next.

Define RosterCog con:
- Listener on_member_update para sincronización automática de altas y bajas
  de membresía en BD ante adición o remoción de roles de equipo en Discord.
- Slash command /gestionar-posicion para desplegar la interfaz interactiva
  GestionarPosicionView y modificar rol de plantilla y capitanía.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from liga_bot.cogs.permissions import is_staff, resolve_member
from liga_bot.config import Settings, get_settings
from liga_bot.ui.roster import GestionarPosicionView

if TYPE_CHECKING:
    from liga_bot.bot import LigaBot
    from liga_bot.services.roster_sync_service import RosterSyncService

logger = logging.getLogger(__name__)


class RosterCog(commands.Cog, name="Roster"):
    """
    Cog responsable de la sincronización de roles de equipo y la gestión
    de posiciones de plantilla.
    """

    def __init__(
        self,
        bot: LigaBot | commands.Bot,
        settings: Settings | None = None,
    ) -> None:
        self.bot: LigaBot | commands.Bot = bot
        self._settings = settings

    @property
    def settings(self) -> Settings:
        """Resuelve de forma resiliente la configuración de la aplicación."""
        if self._settings is not None:
            return self._settings
        bot_settings = getattr(self.bot, "settings", None)
        return bot_settings if bot_settings is not None else get_settings()

    @property
    def roster_sync_service(self) -> RosterSyncService | None:
        """Resuelve el servicio de sincronización de plantillas desde el bot."""
        return getattr(self.bot, "roster_sync_service", None)

    # ---------------------------------------------------------------------------
    # Event Listener: Sincronización Automática de Roles
    # ---------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """
        Detecta cambios en los roles de un miembro de Discord y delega la sincronización
        en RosterSyncService de forma secuencial y resiliente ante fallos parciales:
        - Roles añadidos: invoca handle_role_added (crea membresía si es rol de club).
        - Roles eliminados: invoca handle_role_removed (da de baja la membresía si correspondía).
        - Aislamiento de excepciones: un fallo en un rol no interrumpe el resto.
        """
        before_roles = getattr(before, "roles", [])
        after_roles = getattr(after, "roles", [])

        added_roles = [r for r in after_roles if r not in before_roles]
        removed_roles = [r for r in before_roles if r not in after_roles]

        if not added_roles and not removed_roles:
            return

        service = self.roster_sync_service
        if service is None:
            logger.warning(
                "RosterSyncService no disponible en el bot; omitiendo sync para %s (%s).",
                getattr(after, "display_name", str(after)),
                getattr(after, "id", "desconocido"),
            )
            return

        # 1. Procesar roles añadidos secuencialmente
        for role in added_roles:
            try:
                membership = await service.handle_role_added(member=after, role=role)
                if membership is not None:
                    logger.info(
                        "Membresía sincronizada (alta): usuario '%s' (%s), rol '%s' (%s).",
                        after.display_name,
                        after.id,
                        role.name,
                        role.id,
                    )
            except Exception as exc:
                logger.error(
                    "Error al procesar alta de rol '%s' (%s) para usuario '%s' (%s): %s",
                    getattr(role, "name", "desconocido"),
                    getattr(role, "id", "desconocido"),
                    after.display_name,
                    after.id,
                    exc,
                    exc_info=True,
                )

        # 2. Procesar roles eliminados secuencialmente
        for role in removed_roles:
            try:
                removed = await service.handle_role_removed(member=after, role=role)
                if removed:
                    logger.info(
                        "Membresía sincronizada (baja): usuario '%s' (%s), rol '%s' (%s).",
                        after.display_name,
                        after.id,
                        role.name,
                        role.id,
                    )
            except Exception as exc:
                logger.error(
                    "Error al procesar retirada de rol '%s' (%s) para usuario '%s' (%s): %s",
                    getattr(role, "name", "desconocido"),
                    getattr(role, "id", "desconocido"),
                    after.display_name,
                    after.id,
                    exc,
                    exc_info=True,
                )

    # ---------------------------------------------------------------------------
    # Slash Command: /gestionar-posicion
    # ---------------------------------------------------------------------------

    @app_commands.command(
        name="gestionar-posicion",
        description="Gestiona la posición y rol de plantilla de un jugador en sus equipos",
    )
    @app_commands.describe(
        member="Miembro del servidor cuya posición de plantilla se desea gestionar",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gestionar_posicion(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        """Abre el panel interactivo para gestionar la posición y rol de plantilla."""
        # 1. Validación de contexto de servidor
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        # 2. Verificación de autorización de Staff / Administrador
        staff_check = is_staff(interaction.user, self.settings)
        is_authorized = await staff_check if inspect.isawaitable(staff_check) else bool(staff_check)
        if not is_authorized:
            await interaction.response.send_message(
                "❌ Solo el personal de staff tiene autorización para gestionar posiciones.",
                ephemeral=True,
            )
            return

        # 3. Verificación previa de disponibilidad del servicio
        service = self.roster_sync_service
        if service is None:
            await interaction.response.send_message(
                "❌ El servicio de sincronización de plantillas no está disponible.",
                ephemeral=True,
            )
            return

        # 4. Respuesta diferida efímera
        await interaction.response.defer(ephemeral=True)

        target_member = await resolve_member(member) or member

        # 5. Consulta de equipos pertenecientes
        try:
            user_teams = await service.get_user_teams(str(target_member.id))
        except Exception as exc:
            logger.error(
                "Error al consultar equipos para usuario %s (%s): %s",
                getattr(target_member, "display_name", str(target_member)),
                target_member.id,
                exc,
                exc_info=True,
            )
            await interaction.followup.send(
                "❌ Ocurrió un error inesperado al consultar los equipos del usuario en la base.",
                ephemeral=True,
            )
            return

        # 6. Caso sin equipos: enviar advertencia informativa sin vista interactiva
        if not user_teams:
            desc = (
                f"El usuario {target_member.mention} (`{target_member.display_name}`) "
                "no pertenece a la plantilla de ningún equipo registrado en la liga.\n\n"
                "Para gestionar su posición competitiva o rol en plantilla, primero "
                "debe tener asignado al menos un rol de equipo oficial en Discord."
            )
            empty_embed = discord.Embed(
                title="🛡️ Sin Equipos Registrados",
                description=desc,
                color=discord.Color.orange(),
            )
            avatar_url = getattr(getattr(target_member, "display_avatar", None), "url", None)
            if avatar_url:
                empty_embed.set_thumbnail(url=avatar_url)
            empty_embed.set_footer(text="RCL League • Sincronización de Plantillas")
            await interaction.followup.send(embed=empty_embed, ephemeral=True)
            return

        # 7. Caso con equipos: instanciar vista interactiva y enviar panel
        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=interaction.user,
        )
        msg = await interaction.followup.send(
            embed=view.build_initial_embed(),
            view=view,
            ephemeral=True,
        )
        view.message = msg


async def setup(bot: LigaBot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "Roster" not in bot.cogs:
        await bot.add_cog(RosterCog(bot))  # type: ignore[arg-type]


__all__ = [
    "RosterCog",
    "setup",
]
