"""
Módulo de resolución de miembros y política de autorización del staff de LigaBot.

Garantiza la resolución consistente de entidades discord.Member (evitando caídas
cuando interaction.user es discord.User por omisión de intents o caché incompleta)
y concentra en `has_staff_access` qué roles cuentan como staff para cada tipo de
acción. La tabla completa está en `docs/architecture/permissions.md`.

Reglas comunes a todas las acciones:

- El permiso nativo de Administrador autoriza siempre.
- «Gestionar servidor» (`manage_guild`) no autoriza por sí solo: en los comandos solo
  decide su visibilidad por defecto (`default_permissions`).
- Los roles Staff, Admin y CEO general (si `CEO_ROLE_ID` está configurado) autorizan
  todas las acciones de staff.
- Los roles CEO Premier y CEO Ascend autorizan además calendario, casters y tickets.
"""

from __future__ import annotations

import logging
from enum import StrEnum

import discord

from liga_bot.config import Settings, get_settings

logger = logging.getLogger(__name__)


class StaffAction(StrEnum):
    """Tipos de acción de staff con una política de autorización propia."""

    ROLES_Y_PLANTILLAS = "roles_y_plantillas"
    CALENDARIO_Y_CASTERS = "calendario_y_casters"
    TICKETS = "tickets"
    SINCRONIZACION = "sincronizacion"


_ACCIONES_CON_CEOS_DE_DIVISION = frozenset({StaffAction.CALENDARIO_Y_CASTERS, StaffAction.TICKETS})


def allowed_role_ids(action: StaffAction, settings: Settings) -> frozenset[int]:
    """Devuelve los IDs de rol que autorizan la acción indicada (sin contar Administrador)."""
    role_ids = {settings.staff_role_id, settings.admin_role_id, settings.ceo_role_id}
    if action in _ACCIONES_CON_CEOS_DE_DIVISION:
        role_ids |= {settings.ceo_premier_role_id, settings.ceo_ascend_role_id}
    # Un ID 0 significa «rol no configurado».
    role_ids.discard(0)
    return frozenset(role_ids)


async def resolve_member(
    interaction: discord.Interaction | discord.Member,
) -> discord.Member | None:
    """
    Resuelve la entidad discord.Member a partir de interaction o miembro directo.

    1. Si ya es una instancia de discord.Member, la retorna directamente.
    2. Si interaction.user es un discord.Member, lo retorna.
    3. Si interaction.guild existe, consulta la caché local (guild.get_member).
    4. Si no está en caché, intenta obtenerla mediante la API de red (guild.fetch_member).
    5. Si falla la resolución o se ejecuta en mensajes directos (DM), retorna None.
    """
    if isinstance(interaction, discord.Member):
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


async def has_staff_access(
    target: discord.Interaction | discord.Member,
    action: StaffAction,
    settings: Settings | None = None,
) -> bool:
    """Decide si el invocador puede ejecutar una acción de staff del tipo indicado."""
    member = await resolve_member(target)
    if member is None:
        return False
    if member.guild_permissions.administrator is True:
        return True

    app_settings = settings or get_settings()
    user_roles = {role.id for role in member.roles}
    return not user_roles.isdisjoint(allowed_role_ids(action, app_settings))


async def is_staff(
    member: discord.Interaction | discord.Member,
    settings: Settings | None = None,
    *,
    interaction: discord.Interaction | None = None,
) -> bool:
    """
    Autorización para roles y plantillas: /asignar-rol, /publicar-panel-rol, botones de
    ticket de rol, /registrar-equipo, /gestionar-posicion, /traspasa-equipo y
    /liberar-jugador.
    """
    target = member if member is not None else interaction
    if target is None:
        return False
    return await has_staff_access(target, StaffAction.ROLES_Y_PLANTILLAS, settings)


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
    interaction: discord.Interaction | discord.Member,
    settings: Settings | None = None,
) -> bool:
    """Autorización para la sincronización de comandos: /sync, /sincronizar y !sync."""
    return await has_staff_access(interaction, StaffAction.SINCRONIZACION, settings)


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
    interaction: discord.Interaction | discord.Member,
    settings: Settings | None = None,
) -> bool:
    """
    Autorización para calendario y casters: /crear-partido, /importar-jornada,
    /crear-jornada, /stream_url, /stream_url_live, /panel-casters y /cartelera-casters.
    """
    return await has_staff_access(interaction, StaffAction.CALENDARIO_Y_CASTERS, settings)


# Alias sinónimo para compatibilidad
is_staff_admin_or_ceo = is_authorized_scheduler
