"""
Admin cog for LigaBot managing command synchronization and administrative tools.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from liga_bot.cogs.permissions import is_staff_or_admin
from liga_bot.config import Settings, get_settings

if TYPE_CHECKING:
    from discord.ext.commands import Bot

logger = logging.getLogger(__name__)


class AdminCog(commands.Cog, name="Admin"):
    """Comandos administrativos protegidos y sincronización del árbol de Discord."""

    def __init__(
        self,
        bot: Bot | commands.Bot,
        settings: Settings | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings or getattr(bot, "settings", None) or get_settings()

    async def _sync_impl(
        self,
        interaction: discord.Interaction,
        guild_id: str | None = None,
        global_sync: bool = False,
    ) -> None:
        """Lógica centralizada para sincronización del command tree."""
        if not await is_staff_or_admin(interaction, self.settings):
            await interaction.response.send_message(
                "❌ No tienes permisos para sincronizar comandos "
                "(se requiere Staff o Administrador).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if global_sync:
                synced = await self.bot.tree.sync(guild=None)
                target_desc = "Global (todos los servidores)"
            else:
                target_guild: discord.abc.Snowflake
                if guild_id is not None and guild_id.strip():
                    try:
                        target_id = int(guild_id.strip())
                    except ValueError:
                        await interaction.followup.send(
                            "❌ El ID de servidor proporcionado debe ser un número entero válido.",
                            ephemeral=True,
                        )
                        return
                    target_guild = discord.Object(id=target_id)
                    target_desc = f"Servidor con ID `{target_id}`"
                elif interaction.guild is not None:
                    target_guild = interaction.guild
                    target_desc = (
                        f"Servidor `{getattr(interaction.guild, 'name', 'actual')}` "
                        f"(`{interaction.guild.id}`)"
                    )
                else:
                    target_guild = discord.Object(id=self.settings.guild_id)
                    target_desc = f"Servidor configurado (`{self.settings.guild_id}`)"

                synced = await self.bot.tree.sync(guild=target_guild)

            embed = discord.Embed(
                title="🔄 Árbol de Comandos Sincronizado",
                description=f"Se han sincronizado satisfactoriamente **{len(synced)}** comando(s).",
                color=discord.Color.green(),
            )
            embed.add_field(name="Alcance de Sincronización", value=target_desc, inline=False)
            if synced:
                command_names = ", ".join(f"`/{c.name}`" for c in synced)
                if len(command_names) > 1020:
                    command_names = command_names[:1000] + " ... (truncado)"
                embed.add_field(name="Comandos Registrados", value=command_names, inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info("Sincronización completada: %d comandos en %s", len(synced), target_desc)

        except discord.Forbidden as err:
            logger.error("Permiso denegado al sincronizar comandos: %s", err)
            embed_err = discord.Embed(
                title="❌ Error de Permisos en Discord",
                description=(
                    f"El bot no cuenta con permisos suficientes para sincronizar comandos: {err}"
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed_err, ephemeral=True)
        except discord.HTTPException as err:
            logger.error("Error HTTP al sincronizar comandos: %s", err)
            embed_err = discord.Embed(
                title="❌ Error de Discord API",
                description=f"Ocurrió un error al contactar la API de Discord: {err}",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed_err, ephemeral=True)

    @app_commands.command(
        name="sync",
        description="Sincroniza el árbol de slash commands del bot",
    )
    @app_commands.describe(
        guild_id="ID de servidor específico a sincronizar (opcional)",
        global_sync="Si es True, sincroniza globalmente todos los comandos (opcional)",
    )
    async def sync(
        self,
        interaction: discord.Interaction,
        guild_id: str | None = None,
        global_sync: bool = False,
    ) -> None:
        """Sincroniza slash commands vía /sync."""
        await self._sync_impl(interaction, guild_id=guild_id, global_sync=global_sync)

    @app_commands.command(
        name="sincronizar",
        description="Alias de /sync: sincroniza el árbol de slash commands del bot",
    )
    @app_commands.describe(
        guild_id="ID de servidor específico a sincronizar (opcional)",
        global_sync="Si es True, sincroniza globalmente todos los comandos (opcional)",
    )
    async def sincronizar(
        self,
        interaction: discord.Interaction,
        guild_id: str | None = None,
        global_sync: bool = False,
    ) -> None:
        """Alias en español de /sync."""
        await self._sync_impl(interaction, guild_id=guild_id, global_sync=global_sync)


async def setup(bot: Bot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "Admin" not in bot.cogs:
        await bot.add_cog(AdminCog(bot))
