"""
Schedule cog for LigaBot managing match provisioning and CSV imports.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.cogs.permissions import is_authorized_scheduler
from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory
from liga_bot.services.schedule_service import JornadaResult, MatchResult, ScheduleService

if TYPE_CHECKING:
    from discord.ext.commands import Bot

logger = logging.getLogger(__name__)


class ScheduleCog(commands.Cog, name="Schedule"):
    """Comandos de programación de calendario, alta de partidos e importación CSV."""

    def __init__(
        self,
        bot: Bot | commands.Bot,
        schedule_service: ScheduleService | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings or getattr(bot, "settings", None) or get_settings()
        self._session_factory = session_factory or getattr(bot, "session_factory", None)
        self._schedule_service = schedule_service

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Obtiene o inicializa de forma resiliente la factoría de sesiones."""
        if self._session_factory is not None:
            return self._session_factory
        bot_factory = getattr(self.bot, "session_factory", None)
        if bot_factory is not None:
            return bot_factory
        return get_session_factory()

    @property
    def schedule_service(self) -> ScheduleService:
        """Obtiene o inicializa de forma resiliente el ScheduleService."""
        if self._schedule_service is not None:
            return self._schedule_service
        service = getattr(self.bot, "schedule_service", None)
        if service is not None:
            return service
        self._schedule_service = ScheduleService(
            session_factory=self.session_factory,
            settings=self.settings,
            bot=self.bot,
        )
        return self._schedule_service

    @app_commands.command(
        name="crear-partido",
        description="Crea el canal de un partido individual y postea los mensajes oficiales",
    )
    @app_commands.describe(
        jornada="Número de la jornada (ej: 1, 2, ...)",
        equipo1="Nombre o tag del equipo local",
        equipo2="Nombre o tag del equipo visitante",
        fecha="Fecha del partido en formato DD/MM/YYYY (opcional)",
        hora="Hora del partido en formato HH:MM (opcional)",
    )
    async def crear_partido(
        self,
        interaction: discord.Interaction,
        jornada: int,
        equipo1: str,
        equipo2: str,
        fecha: str | None = None,
        hora: str | None = None,
    ) -> None:
        """Crea un canal de partido individual con permisos específicos de división y CEO."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        if not await is_authorized_scheduler(interaction, self.settings):
            await interaction.response.send_message(
                "❌ No tienes permisos para crear partidos (se requiere Staff, Admin o CEO).",
                ephemeral=True,
            )
            return

        if jornada < 1:
            await interaction.response.send_message(
                "❌ El número de jornada debe ser un entero positivo (>= 1).",
                ephemeral=True,
            )
            return

        scheduled_dt: datetime | None = None
        fecha_clean = fecha.strip() if fecha else None
        hora_clean = hora.strip() if hora else None

        if fecha_clean and hora_clean:
            try:
                scheduled_dt = datetime.strptime(
                    f"{fecha_clean} {hora_clean}", "%d/%m/%Y %H:%M"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                scheduled_dt = None

        await interaction.response.defer(ephemeral=True)

        result: MatchResult = await self.schedule_service.create_match(
            guild=interaction.guild,
            jornada=jornada,
            team1_name=equipo1.strip(),
            team2_name=equipo2.strip(),
            scheduled_at=scheduled_dt,
            fecha=fecha_clean,
            hora=hora_clean,
        )

        if result.success and result.channel is not None:
            embed = discord.Embed(
                title=f"✅ Partido Creado — Jornada {jornada}",
                description=f"Enfrentamiento: **{result.team1_name}** VS **{result.team2_name}**",
                color=discord.Color.green(),
            )
            embed.add_field(name="Canal de Coordinación", value=result.channel.mention, inline=True)
            if scheduled_dt:
                embed.add_field(
                    name="Horario Programado",
                    value=scheduled_dt.strftime("%d/%m/%Y %H:%M UTC"),
                    inline=True,
                )
            elif fecha_clean or hora_clean:
                embed.add_field(
                    name="Horario Tentativo",
                    value=f"{fecha_clean or ''} {hora_clean or ''}".strip(),
                    inline=True,
                )
            jump_url = getattr(result.channel, "jump_url", None) or result.channel.mention
            embed.add_field(
                name="Enlace al Canal",
                value=f"[Ir al canal]({jump_url})" if jump_url.startswith("http") else jump_url,
                inline=False,
            )
        elif result.is_duplicate:
            embed = discord.Embed(
                title=f"⚠️ Partido Ya Existente — Jornada {jornada}",
                description=result.error or "El partido ya se encuentra registrado.",
                color=discord.Color.gold(),
            )
        else:
            embed = discord.Embed(
                title=f"❌ Error al Crear Partido — Jornada {jornada}",
                description=result.error or "Error desconocido durante el aprovisionamiento.",
                color=discord.Color.red(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _importar_jornada_impl(
        self,
        interaction: discord.Interaction,
        jornada: int,
        archivo: discord.Attachment,
    ) -> None:
        """Lógica común para /importar-jornada y /crear-jornada."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        if not await is_authorized_scheduler(interaction, self.settings):
            await interaction.response.send_message(
                "❌ No tienes permisos para importar jornadas (se requiere Staff, Admin o CEO).",
                ephemeral=True,
            )
            return

        if jornada < 1:
            await interaction.response.send_message(
                "❌ El número de jornada debe ser un entero positivo (>= 1).",
                ephemeral=True,
            )
            return

        if not archivo.filename.lower().endswith(".csv"):
            await interaction.response.send_message(
                "❌ El archivo adjunto debe ser de tipo CSV (.csv).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            raw_bytes = await archivo.read()
            csv_text = raw_bytes.decode("utf-8-sig", errors="replace")
        except Exception as exc:
            embed_err = discord.Embed(
                title="❌ Error al Leer Archivo CSV",
                description=f"No se pudo decodificar el archivo: {exc}",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed_err, ephemeral=True)
            return

        result: JornadaResult = await self.schedule_service.create_jornada_from_csv(
            guild=interaction.guild,
            jornada=jornada,
            csv_content=csv_text,
        )

        if result.success_count > 0 and result.error_count == 0:
            color = discord.Color.green()
            status_title = f"✅ Jornada {jornada} Importada con Éxito"
        elif result.success_count > 0:
            color = discord.Color.orange()
            status_title = f"⚠️ Jornada {jornada} Importada Parcialmente"
        else:
            color = discord.Color.red()
            status_title = f"❌ Error en la Importación — Jornada {jornada}"

        embed = discord.Embed(title=status_title, color=color)
        embed.add_field(
            name="Resumen de Filas",
            value=(
                f"**Total filas:** {result.total_rows}\n"
                f"**Partidos creados:** {result.success_count}\n"
                f"**Errores/Omitidos:** {result.error_count}"
            ),
            inline=False,
        )

        created_channels = [
            m.channel_mention for m in result.matches if m.success and m.channel_mention
        ]
        if created_channels:
            channels_text = ", ".join(created_channels)
            if len(channels_text) > 1020:
                channels_text = channels_text[:1000] + " ... (truncado)"
            embed.add_field(name="Canales Aprovisionados", value=channels_text, inline=False)

        if result.errors:
            error_lines = result.errors[:10]
            errors_text = "\n".join(f"• {e}" for e in error_lines)
            if len(result.errors) > 10:
                errors_text += f"\n*... y {len(result.errors) - 10} errores adicionales.*"
            if len(errors_text) > 1020:
                errors_text = errors_text[:1000] + " ... (truncado)"
            embed.add_field(name="Incidencias Reportadas", value=errors_text, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="importar-jornada",
        description="Crea todos los canales de partido de una jornada a partir de un archivo CSV",
    )
    @app_commands.describe(
        jornada="Número de jornada",
        archivo="Archivo CSV con columnas: equipo1,equipo2,fecha,hora",
    )
    async def importar_jornada(
        self,
        interaction: discord.Interaction,
        jornada: int,
        archivo: discord.Attachment,
    ) -> None:
        """Slash command principal para importar una jornada."""
        await self._importar_jornada_impl(interaction, jornada, archivo)

    @app_commands.command(
        name="crear-jornada",
        description="Alias de /importar-jornada: crea canales de partido desde un archivo CSV",
    )
    @app_commands.describe(
        jornada="Número de jornada",
        archivo="Archivo CSV con columnas: equipo1,equipo2,fecha,hora",
    )
    async def crear_jornada(
        self,
        interaction: discord.Interaction,
        jornada: int,
        archivo: discord.Attachment,
    ) -> None:
        """Alias legacy de /importar-jornada."""
        await self._importar_jornada_impl(interaction, jornada, archivo)


async def setup(bot: Bot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "Schedule" not in bot.cogs:
        await bot.add_cog(ScheduleCog(bot))
