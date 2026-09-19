"""
Módulo de resolución de miembros y verificación de permisos para Cogs de LigaBot.

Garantiza la resolución consistente de entidades discord.Member (evitando caídas
cuando interaction.user es discord.User por omisión de intents o caché incompleta)
y verifica los roles autorizados para la operativa de la liga.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from liga_bot.config import Settings, get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def resolve_member(
    interaction: discord.Interaction | discord.Member,
) -> discord.Member | None:
    """
    Resuelve la entidad discord.Member a partir de interaction o miembro directo.

    1. Si ya es una instancia de discord.Member (o mock con roles), la retorna directamente.
    2. Si interaction.guild existe, consulta la caché local (guild.get_member).
    3. Si no está en caché, intenta obtenerla mediante la API de red (guild.fetch_member).
    4. Si falla la resolución o se ejecuta en mensajes directos (DM), retorna None.
    """
    if isinstance(interaction, discord.Member) or (
        hasattr(interaction, "roles") and not hasattr(interaction, "user")
    ):
        return interaction

    user = getattr(interaction, "user", None)
    if isinstance(user, discord.Member):
        return user

    guild = getattr(interaction, "guild", None)
    if guild is not None and user is not None:
        cached_member = guild.get_member(user.id)
        if cached_member is not None:
            return cached_member
        try:
            return await guild.fetch_member(user.id)
        except (discord.NotFound, discord.HTTPException) as exc:
            logger.debug(
                "No se pudo resolver discord.Member para usuario %s en guild %s: %s",
                getattr(user, "id", "desconocido"),
                guild.id,
                exc,
            )
            return None

    return None


async def is_staff(
    member: discord.Interaction | discord.Member,
    settings: Settings | None = None,
    *,
    interaction: discord.Interaction | None = None,
) -> bool:
    """
    Verifica si el miembro o interacción posee rol de Staff, rol de CEO,
    o permisos nativos de Administrador o Administrar Servidor (manage_guild).
    """
    target = member if member is not None else interaction
    if target is None:
        return False

    actual_member = await resolve_member(target)
    if actual_member is None:
        return False

    perms = getattr(actual_member, "guild_permissions", None)
    if perms is not None:
        admin = getattr(perms, "administrator", False)
        manage_guild = getattr(perms, "manage_guild", False)
        if (admin is True or (isinstance(admin, bool) and admin)) or (
            manage_guild is True or (isinstance(manage_guild, bool) and manage_guild)
        ):
            return True

    app_settings = settings or get_settings()
    user_roles = {r.id for r in getattr(actual_member, "roles", [])}
    if app_settings.staff_role_id in user_roles:
        return True
    if app_settings.ceo_role_id > 0 and app_settings.ceo_role_id in user_roles:
        return True
    return False


async def is_admin(
    interaction: discord.Interaction,
    settings: Settings | None = None,
) -> bool:
    """Verifica si el usuario posee rol de Administrador o permisos de Administrador nativo."""
    member = await resolve_member(interaction)
    if member is None:
        return False
    if getattr(getattr(member, "guild_permissions", None), "administrator", False):
        return True

    app_settings = settings or get_settings()
    user_roles = {r.id for r in getattr(member, "roles", [])}
    return app_settings.admin_role_id in user_roles


async def is_staff_or_admin(
    interaction: discord.Interaction,
    settings: Settings | None = None,
) -> bool:
    """
    Verifica si el invocador posee rol de Staff, Administrador o permisos nativos de Administrador.
    Utilizado en: /registrar-equipo, /sync, /revisar-tickets.
    """
    member = await resolve_member(interaction)
    if member is None:
        return False
    if getattr(getattr(member, "guild_permissions", None), "administrator", False):
        return True

    app_settings = settings or get_settings()
    user_roles = {r.id for r in getattr(member, "roles", [])}
    allowed_roles = {app_settings.staff_role_id, app_settings.admin_role_id}
    return bool(user_roles & allowed_roles)


async def is_ceo_premier(
    interaction: discord.Interaction,
    settings: Settings | None = None,
) -> bool:
    """Verifica si el usuario posee el rol de CEO Premier o es Administrador."""
    member = await resolve_member(interaction)
    if member is None:
        return False
    if getattr(getattr(member, "guild_permissions", None), "administrator", False):
        return True

    app_settings = settings or get_settings()
    user_roles = {r.id for r in getattr(member, "roles", [])}
    return app_settings.ceo_premier_role_id in user_roles


async def is_ceo_ascend(
    interaction: discord.Interaction,
    settings: Settings | None = None,
) -> bool:
    """Verifica si el usuario posee el rol de CEO Ascend o es Administrador."""
    member = await resolve_member(interaction)
    if member is None:
        return False
    if getattr(getattr(member, "guild_permissions", None), "administrator", False):
        return True

    app_settings = settings or get_settings()
    user_roles = {r.id for r in getattr(member, "roles", [])}
    return app_settings.ceo_ascend_role_id in user_roles


async def is_authorized_scheduler(
    interaction: discord.Interaction,
    settings: Settings | None = None,
) -> bool:
    """
    Verifica si el usuario está autorizado para programar partidos
    (Staff, Administrador, CEO Premier o CEO Ascend).
    """
    member = await resolve_member(interaction)
    if member is None:
        return False
    if getattr(getattr(member, "guild_permissions", None), "administrator", False):
        return True

    app_settings = settings or get_settings()
    user_roles = {r.id for r in getattr(member, "roles", [])}
    allowed_roles = {
        app_settings.staff_role_id,
        app_settings.admin_role_id,
        app_settings.ceo_premier_role_id,
        app_settings.ceo_ascend_role_id,
    }
    return bool(user_roles & allowed_roles)


# Alias sinónimo para compatibilidad
is_staff_admin_or_ceo = is_authorized_scheduler
