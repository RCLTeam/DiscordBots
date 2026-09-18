"""
Teams cog for LigaBot managing team registration and listing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.cogs.permissions import is_staff_or_admin
from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory, transactional_session
from liga_bot.models.enums import Division
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.utils.formatting import normalize_slug, normalize_tag

if TYPE_CHECKING:
    from discord.ext.commands import Bot

logger = logging.getLogger(__name__)


class TeamsCog(commands.Cog, name="Teams"):
    """Gestión de equipos participantes, registro de roles y listados por división."""

    def __init__(
        self,
        bot: Bot | commands.Bot,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings or getattr(bot, "settings", None) or get_settings()
        self._session_factory = session_factory or getattr(bot, "session_factory", None)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Obtiene o inicializa de forma resiliente la factoría de sesiones."""
        if self._session_factory is not None:
            return self._session_factory
        bot_factory = getattr(self.bot, "session_factory", None)
        if bot_factory is not None:
            return bot_factory
        return get_session_factory()

    @app_commands.command(
        name="registrar-equipo",
        description="Registra o actualiza un equipo de la liga con su rol y división",
    )
    @app_commands.describe(
        rol="Rol de Discord asignado al equipo",
        nombre="Nombre oficial del equipo",
        tag="Acrónimo o tag del equipo (máximo 4 caracteres, ej: PSP, FNX)",
        division="División competitiva (Premier o Ascenso)",
    )
    @app_commands.choices(
        division=[
            app_commands.Choice(name="Premier", value="PREMIER"),
            app_commands.Choice(name="Ascenso", value="ASCEND"),
        ]
    )
    async def registrar_equipo(
        self,
        interaction: discord.Interaction,
        rol: discord.Role,
        nombre: str,
        tag: str,
        division: app_commands.Choice[str],
    ) -> None:
        """Registra o actualiza un equipo de forma idempotente en la base de datos."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        if not await is_staff_or_admin(interaction, self.settings):
            await interaction.response.send_message(
                "❌ No tienes permisos para registrar equipos "
                "(se requiere rol de Staff o Administrador).",
                ephemeral=True,
            )
            return

        target_name = nombre.strip()
        if not target_name:
            await interaction.response.send_message(
                "❌ El nombre del equipo no puede estar vacío.",
                ephemeral=True,
            )
            return

        if len(target_name) > 100:
            await interaction.response.send_message(
                "❌ El nombre del equipo no puede exceder los 100 caracteres.",
                ephemeral=True,
            )
            return

        # Validación de tag
        cleaned_tag = "".join(c for c in tag.strip() if not c.isspace())
        if not cleaned_tag or len(cleaned_tag) > 4:
            await interaction.response.send_message(
                "❌ El tag del equipo debe tener entre 1 y 4 caracteres (ej: 'PSP', 'FNX').",
                ephemeral=True,
            )
            return

        # Normalización de división
        div_value = division.value if isinstance(division, app_commands.Choice) else str(division)
        div_upper = div_value.strip().upper()
        if div_upper in ("PREMIER", "PREM"):
            division_enum = Division.PREMIER
        elif div_upper in ("ASCEND", "ASCENSO", "ASC"):
            division_enum = Division.ASCEND
        else:
            try:
                division_enum = Division(div_upper)
            except ValueError:
                division_enum = Division.PREMIER

        tag_normalized = normalize_tag(cleaned_tag)
        slug = normalize_slug(target_name)

        await interaction.response.defer(ephemeral=True)

        try:
            async with transactional_session(self.session_factory) as session:
                repo = TeamRepository(session)
                existing_by_role = await repo.get_by_role_id(rol.id)
                existing_by_name = await repo.get_by_name(target_name)

                if (
                    existing_by_role is not None
                    and existing_by_name is not None
                    and existing_by_role.id != existing_by_name.id
                ):
                    embed_err = discord.Embed(
                        title="❌ Conflicto de Registro",
                        description=(
                            f"El rol {rol.mention} ya pertenece a '{existing_by_role.name}', "
                            f"mientras que el nombre '{target_name}' ya está registrado "
                            "con otro rol."
                        ),
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(embed=embed_err, ephemeral=True)
                    return

                action = "actualizado"
                if existing_by_role is not None:
                    team = await repo.update(
                        existing_by_role,
                        name=target_name,
                        tag=tag_normalized,
                        slug=slug,
                        division=division_enum,
                    )
                elif existing_by_name is not None:
                    team = await repo.update(
                        existing_by_name,
                        tag=tag_normalized,
                        slug=slug,
                        division=division_enum,
                        discord_role_id=rol.id,
                    )
                else:
                    team = await repo.create(
                        name=target_name,
                        tag=tag_normalized,
                        slug=slug,
                        division=division_enum,
                        discord_role_id=rol.id,
                    )
                    action = "registrado"

            embed = discord.Embed(
                title=f"✅ Equipo {action.capitalize()} con Éxito",
                color=discord.Color.green(),
            )
            embed.add_field(name="Nombre", value=team.name, inline=True)
            embed.add_field(name="Tag", value=f"`{team.tag}`", inline=True)
            embed.add_field(name="División", value=team.division.value, inline=True)
            embed.add_field(
                name="Rol de Discord",
                value=f"{rol.mention} (`{rol.id}`)",
                inline=False,
            )
            embed.add_field(name="Slug Canal", value=f"`{team.slug}`", inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.error("Error inesperado al registrar equipo: %s", exc, exc_info=True)
            embed_err = discord.Embed(
                title="❌ Error al Registrar Equipo",
                description=(
                    "Ocurrió un error inesperado al procesar el registro en la base de datos."
                ),
                color=discord.Color.red(),
            )
            embed_err.add_field(
                name="Detalles",
                value=f"`{type(exc).__name__}`: {str(exc)[:500]}",
                inline=False,
            )
            await interaction.followup.send(embed=embed_err, ephemeral=True)

    @app_commands.command(
        name="equipos",
        description="Lista los equipos registrados en la liga y sus roles asignados",
    )
    @app_commands.describe(
        division="Filtrar por división específica (opcional)",
    )
    @app_commands.choices(
        division=[
            app_commands.Choice(name="Todas las divisiones", value="ALL"),
            app_commands.Choice(name="Premier", value="PREMIER"),
            app_commands.Choice(name="Ascenso", value="ASCEND"),
        ]
    )
    async def equipos(
        self,
        interaction: discord.Interaction,
        division: app_commands.Choice[str] | None = None,
    ) -> None:
        """Muestra el listado de equipos registrados agrupados por división."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        div_filter_val: str | None = None
        if division is not None:
            div_filter_val = (
                division.value if isinstance(division, app_commands.Choice) else str(division)
            ).upper()

        await interaction.response.defer(ephemeral=False)

        async with transactional_session(self.session_factory) as session:
            repo = TeamRepository(session)
            if div_filter_val and div_filter_val not in ("ALL", "TODAS"):
                if div_filter_val in ("PREMIER", "PREM"):
                    div_enum = Division.PREMIER
                else:
                    div_enum = Division.ASCEND
                teams_list = await repo.list_by_division(div_enum)
                filter_label = f"División {div_enum.value}"
            else:
                teams_list = await repo.list_all()
                filter_label = "Todas las Divisiones"

        if not teams_list:
            embed_empty = discord.Embed(
                title=f"🛡️ Equipos Registrados — {filter_label}",
                description="No hay equipos registrados actualmente para este criterio.",
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed_empty)
            return

        premier_teams = [t for t in teams_list if t.division == Division.PREMIER]
        ascend_teams = [t for t in teams_list if t.division == Division.ASCEND]

        embed = discord.Embed(
            title=f"🛡️ Equipos Registrados — {filter_label}",
            color=discord.Color.blurple(),
        )

        if premier_teams and (div_filter_val is None or div_filter_val in ("ALL", "PREMIER")):
            lines = [f"• **[{t.tag}]** {t.name} — <@&{t.discord_role_id}>" for t in premier_teams]
            embed.add_field(
                name=f"🏆 Premier ({len(premier_teams)})",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        if ascend_teams and (
            div_filter_val is None or div_filter_val in ("ALL", "ASCEND", "ASCENSO")
        ):
            lines = [f"• **[{t.tag}]** {t.name} — <@&{t.discord_role_id}>" for t in ascend_teams]
            embed.add_field(
                name=f"⚔️ Ascenso ({len(ascend_teams)})",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        embed.set_footer(text=f"Total: {len(teams_list)} equipo(s) registrado(s)")
        await interaction.followup.send(embed=embed)


async def setup(bot: Bot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "Teams" not in bot.cogs:
        await bot.add_cog(TeamsCog(bot))
