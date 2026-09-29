"""
Casters cog for LigaBot managing interactive match broadcast panels,
caster assignments, and idempotent card publishing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.cogs.permissions import is_authorized_scheduler
from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory
from liga_bot.services.caster_service import CasterService
from liga_bot.ui.casters import CasterActionButton, MatchCasterView, build_match_caster_embed

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from liga_bot.bot import LigaBot

logger = logging.getLogger(__name__)


class CastersCog(commands.Cog, name="CastersCog"):
    """Comandos para la cartelera de casters y retransmisiones de partidos."""

    def __init__(
        self,
        bot: Bot | commands.Bot,
        caster_service: CasterService | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.bot = bot
        self.settings: Settings = settings or getattr(bot, "settings", None) or get_settings()
        self._session_factory = session_factory or getattr(bot, "session_factory", None)
        self._caster_service = caster_service

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
    def caster_service(self) -> CasterService:
        """Obtiene o inicializa de forma resiliente el CasterService."""
        if self._caster_service is not None:
            return self._caster_service
        service = getattr(self.bot, "caster_service", None)
        if service is not None:
            return service
        self._caster_service = CasterService(
            session_factory=self.session_factory,
            settings=self.settings,
            bot=self.bot,
        )
        return self._caster_service

    async def cog_load(self) -> None:
        """Registra el DynamicItem de botones de caster en el bot."""
        self.bot.add_dynamic_items(CasterActionButton)

    @app_commands.command(
        name="panel-casters",
        description="Publica o actualiza la cartelera interactiva de casters para una jornada",
    )
    @app_commands.describe(
        jornada="Número de la jornada a publicar (opcional, por defecto la última activa)",
        canal="Canal de texto de destino (opcional, por defecto el canal configurado)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def panel_casters(
        self,
        interaction: discord.Interaction,
        jornada: int | None = None,
        canal: discord.TextChannel | None = None,
    ) -> None:
        """Publica o actualiza las tarjetas interactivas de casters."""
        await self._panel_casters_impl(interaction, jornada, canal)

    @app_commands.command(
        name="cartelera-casters",
        description="Alias de /panel-casters: publica o actualiza la cartelera de casters",
    )
    @app_commands.describe(
        jornada="Número de la jornada a publicar (opcional, por defecto la última activa)",
        canal="Canal de texto de destino (opcional, por defecto el canal configurado)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def cartelera_casters(
        self,
        interaction: discord.Interaction,
        jornada: int | None = None,
        canal: discord.TextChannel | None = None,
    ) -> None:
        """Alias de /panel-casters para publicar o actualizar la cartelera de casters."""
        await self._panel_casters_impl(interaction, jornada, canal)

    async def _panel_casters_impl(
        self,
        interaction: discord.Interaction,
        jornada: int | None,
        canal: discord.TextChannel | None,
    ) -> None:
        """Implementación compartida de /panel-casters y /cartelera-casters."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        if not await is_authorized_scheduler(interaction, self.settings):
            await interaction.response.send_message(
                "❌ No tienes permisos para gestionar la cartelera de casters.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        target_channel: Any = canal
        if target_channel is None and self.settings.casters_channel_id:
            target_channel = interaction.guild.get_channel(self.settings.casters_channel_id)
        if target_channel is None:
            target_channel = interaction.channel

        if target_channel is None or not hasattr(target_channel, "send"):
            await interaction.followup.send(
                "❌ No se pudo determinar un canal válido para publicar la cartelera.",
                ephemeral=True,
            )
            return

        resolved_jornada = jornada
        if resolved_jornada is None:
            resolved_jornada = await self.caster_service.get_active_jornada()

        if resolved_jornada is None:
            await interaction.followup.send(
                "⚠️ No hay jornadas activas ni partidos registrados en la base de datos.",
                ephemeral=True,
            )
            return

        matches = await self.caster_service.get_matches_for_jornada(resolved_jornada)
        if not matches:
            await interaction.followup.send(
                f"⚠️ No hay partidos programados para la Jornada {resolved_jornada}.",
                ephemeral=True,
            )
            return

        published_count = 0
        synced_count = 0

        for match in matches:
            casters_data = await self.caster_service.get_match_casters_data(match.id)
            embed = build_match_caster_embed(match, casters_data)
            view = MatchCasterView(match_id=match.id, casters_data=casters_data)

            card = await self.caster_service.get_card(match.id, target_channel.id)
            if card is not None:
                msg: discord.Message | None = None
                try:
                    msg = await target_channel.fetch_message(card.message_id)
                except (discord.NotFound, discord.HTTPException):
                    msg = None

                if msg is not None:
                    await msg.edit(embed=embed)
                    synced_count += 1
                else:
                    await self.caster_service.delete_card(match.id, target_channel.id)
                    new_msg = await target_channel.send(embed=embed, view=view)
                    await self.caster_service.record_card(match.id, target_channel.id, new_msg.id)
                    published_count += 1
            else:
                new_msg = await target_channel.send(embed=embed, view=view)
                await self.caster_service.record_card(match.id, target_channel.id, new_msg.id)
                published_count += 1

        target_mention = getattr(target_channel, "mention", f"#{target_channel}")
        summary_lines = (
            f"✅ Panel de casters actualizado para la **Jornada {resolved_jornada}** "
            f"en {target_mention}:\n"
            f"- 🆕 Tarjetas publicadas: **{published_count}**\n"
            f"- 🔄 Tarjetas sincronizadas/actualizadas: **{synced_count}**"
        )
        await interaction.followup.send(summary_lines, ephemeral=True)


async def setup(bot: LigaBot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "CastersCog" not in bot.cogs:
        await bot.add_cog(CastersCog(bot))
