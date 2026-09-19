"""
Tickets cog for LigaBot managing ticket inactivity audits and background monitoring.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from liga_bot.cogs.permissions import is_authorized_scheduler
from liga_bot.config import Settings, get_settings
from liga_bot.services.ticket_service import (
    ChannelAuditStatus,
    TicketAuditResult,
    TicketService,
)

if TYPE_CHECKING:
    from discord.ext.commands import Bot

logger = logging.getLogger(__name__)


class TicketsCog(commands.Cog, name="Tickets"):
    """
    Cog responsable de la auditoría periódica y bajo demanda de tickets de soporte.
    """

    def __init__(
        self,
        bot: Bot | commands.Bot,
        ticket_service: TicketService | None = None,
        auto_start: bool = True,
    ) -> None:
        self.bot = bot
        self._ticket_service = ticket_service
        self._auto_start = auto_start

        if auto_start:
            try:
                asyncio.get_running_loop()
                self.check_tickets_loop.start()
            except RuntimeError:
                pass

    @property
    def ticket_service(self) -> TicketService:
        """Resuelve la instancia de TicketService desde el bot o fallback local."""
        if self._ticket_service is not None:
            return self._ticket_service
        bot_service = getattr(self.bot, "ticket_service", None)
        if bot_service is not None:
            return bot_service
        self._ticket_service = TicketService(bot=self.bot)
        return self._ticket_service

    @property
    def settings(self) -> Settings:
        """Resuelve los settings de configuración."""
        bot_settings = getattr(self.bot, "settings", None)
        return bot_settings if bot_settings is not None else get_settings()

    @tasks.loop(hours=24)
    async def check_tickets_loop(self) -> None:
        """Tarea periódica que audita las categorías de tickets cada 24 horas."""
        guild_id = self.settings.guild_id
        if not guild_id:
            logger.warning("TicketsCog: guild_id no configurado. Omitiendo revisión automática.")
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(guild_id)
            except Exception as exc:
                logger.warning(
                    "TicketsCog: No se pudo resolver el guild %s para revisión de tickets: %s",
                    guild_id,
                    exc,
                )
                return

        try:
            logger.info(
                "TicketsCog: Iniciando revisión automática de tickets en %s (%s)...",
                getattr(guild, "name", "Servidor"),
                getattr(guild, "id", guild_id),
            )
            result: TicketAuditResult = await self.ticket_service.check_tickets(guild)
            logger.info(
                "TicketsCog: Revisión automática finalizada. Canales: %d, Avisos: %d",
                result.channels_scanned,
                result.alerts_sent,
            )
        except Exception as exc:
            logger.exception("TicketsCog: Excepción capturada durante el bucle de tickets: %s", exc)

    @check_tickets_loop.before_loop
    async def before_check_tickets_loop(self) -> None:
        """Espera a que el bot esté conectado y con cachés sincronizadas antes de iniciar."""
        await self.bot.wait_until_ready()

    @check_tickets_loop.error
    async def on_check_tickets_error(self, error: Exception) -> None:
        """Manejador de error para evitar la muerte definitiva del loop."""
        logger.error("TicketsCog: Error crítico en la tarea periódica: %s", error, exc_info=True)
        if not self.check_tickets_loop.is_running():
            logger.warning("TicketsCog: Reiniciando bucle de tickets tras fallo no controlado...")
            self.check_tickets_loop.restart()

    def _build_audit_embed(
        self,
        result: TicketAuditResult,
        user: discord.Member | discord.User,
    ) -> discord.Embed:
        """Construye un Embed detallado con el informe de auditoría."""
        color = discord.Color.green() if result.alerts_sent == 0 else discord.Color.gold()
        embed = discord.Embed(
            title="🔍 Auditoría de Inactividad de Tickets",
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="📊 Resumen General",
            value=(
                f"• **Canales auditados**: `{result.channels_scanned}`\n"
                f"• **Categorías revisadas**: `{result.categories_scanned}`\n"
                f"• **Avisos enviados**: `{result.alerts_sent}`"
            ),
            inline=False,
        )

        embed.add_field(
            name="⏭️ Canales Descartados / En Regla",
            value=(
                f"• **Activos (<24h)**: {result.skipped_recent}\n"
                f"• **Respondidos por Staff**: {result.skipped_staff}\n"
                f"• **Ya notificados (<24h)**: {result.skipped_already_alerted}\n"
                f"• **Canales vacíos**: {result.skipped_empty}\n"
                f"• **Sin permisos / Error**: {result.skipped_forbidden + result.skipped_error}"
            ),
            inline=False,
        )

        if result.alerts_sent > 0:
            alerted_mentions = [
                f"• <#{d.channel_id}> (`{d.channel_name}`)"
                for d in result.details
                if d.status == ChannelAuditStatus.ALERT_SENT
            ]
            shown = alerted_mentions[:15]
            more = len(alerted_mentions) - len(shown)
            val = "\n".join(shown)
            if more > 0:
                val += f"\n*... y {more} ticket(s) más.*"
            if len(val) > 1020:
                suffix = "\n*... (truncado por límite de Discord)*"
                val = val[: 1024 - len(suffix)] + suffix
            if len(val) > 1024:
                val = val[:1024]
            embed.add_field(name="🚨 Tickets Notificados", value=val, inline=False)

        author_name = getattr(user, "display_name", str(user))
        embed.set_footer(text=f"Auditoría manual por {author_name} • LigaBot")
        return embed

    @app_commands.command(
        name="revisar-tickets",
        description="Fuerza la revisión inmediata de tickets inactivos sin esperar a las 24h",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def revisar_tickets(self, interaction: discord.Interaction) -> None:
        """Comando slash para ejecutar la auditoría manual de tickets."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        if not await is_authorized_scheduler(interaction, self.settings):
            await interaction.response.send_message(
                "No tienes permiso para usar este comando.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            result = await self.ticket_service.check_tickets(interaction.guild)
            embed = self._build_audit_embed(result, interaction.user)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("TicketsCog: Error ejecutando /revisar-tickets: %s", exc)
            await interaction.followup.send(
                f"❌ Ocurrió un error inesperado al revisar los tickets: `{exc}`",
                ephemeral=True,
            )

    @app_commands.command(
        name="revisar-tickets-manual",
        description="Alias manual para forzar la revisión inmediata de tickets inactivos",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def revisar_tickets_manual(self, interaction: discord.Interaction) -> None:
        """Alias de /revisar-tickets para forzar la revisión manual."""
        await self.revisar_tickets.callback(self, interaction)

    async def cog_load(self) -> None:
        """Inicia el bucle si no estaba activo."""
        if self._auto_start and not self.check_tickets_loop.is_running():
            self.check_tickets_loop.start()

    def cog_unload(self) -> None:
        """Detiene limpiamente la tarea en segundo plano al descargar el Cog."""
        if self.check_tickets_loop.is_running():
            self.check_tickets_loop.cancel()

    def stop_loops(self) -> None:
        """Método explícito para detener loops en shutdown."""
        self.cog_unload()


async def setup(bot: Bot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "Tickets" not in bot.cogs:
        await bot.add_cog(TicketsCog(bot))
