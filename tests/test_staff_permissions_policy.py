"""
Matriz de la política de autorización del staff (`liga_bot.cogs.permissions`).

Cada función de autorización se recorre contra todas las combinaciones de rol y
permiso nativo de la tabla documentada en `docs/architecture/permissions.md`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from liga_bot.cogs.permissions import (
    StaffAction,
    allowed_role_ids,
    has_staff_access,
    is_authorized_scheduler,
    is_staff,
    is_staff_or_admin,
    resolve_member,
)
from liga_bot.config import Settings

STAFF = 101
ADMIN = 102
CEO_GENERAL = 103
CEO_PREMIER = 104
CEO_ASCEND = 105
JUGADOR = 999


def _settings(ceo_general: int = CEO_GENERAL) -> Settings:
    return Settings(
        staff_role_id=STAFF,
        admin_role_id=ADMIN,
        ceo_role_id=ceo_general,
        ceo_premier_role_id=CEO_PREMIER,
        ceo_ascend_role_id=CEO_ASCEND,
    )


def _role(role_id: int) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    return role


def _member(
    role_ids: tuple[int, ...] = (),
    *,
    administrator: bool = False,
    manage_guild: bool = False,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = 42
    member.roles = [_role(r) for r in role_ids]
    member.guild_permissions = discord.Permissions(
        administrator=administrator, manage_guild=manage_guild
    )
    return member


def _interaction(user: object, guild: object | None = None) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user
    inter.guild = guild
    return inter


# Combinaciones de la tabla: (id, roles, administrador, gestionar servidor, ceo general configurado)
COMBINACIONES = {
    "sin_roles": ((JUGADOR,), False, False, True),
    "administrador_nativo": ((), True, False, True),
    "gestionar_servidor": ((), False, True, True),
    "rol_staff": ((STAFF,), False, False, True),
    "rol_admin": ((ADMIN,), False, False, True),
    "ceo_general": ((CEO_GENERAL,), False, False, True),
    "ceo_general_sin_configurar": ((CEO_GENERAL,), False, False, False),
    "ceo_premier": ((CEO_PREMIER,), False, False, True),
    "ceo_ascend": ((CEO_ASCEND,), False, False, True),
}

# Resultado esperado por tipo de acción (tabla de la política)
NUCLEO_STAFF = {
    "administrador_nativo",
    "rol_staff",
    "rol_admin",
    "ceo_general",
}
CON_CEOS_DIVISION = NUCLEO_STAFF | {"ceo_premier", "ceo_ascend"}
POLITICA = {
    StaffAction.ROLES_Y_PLANTILLAS: NUCLEO_STAFF,
    StaffAction.CALENDARIO_Y_CASTERS: CON_CEOS_DIVISION,
    StaffAction.TICKETS: CON_CEOS_DIVISION,
    StaffAction.SINCRONIZACION: NUCLEO_STAFF,
}

# Funciones públicas y la acción que representan
FUNCIONES = {
    "is_staff": (is_staff, StaffAction.ROLES_Y_PLANTILLAS),
    "is_authorized_scheduler": (is_authorized_scheduler, StaffAction.CALENDARIO_Y_CASTERS),
    "is_staff_or_admin": (is_staff_or_admin, StaffAction.SINCRONIZACION),
}


def _casos_accion():
    for action, permitidos in POLITICA.items():
        for nombre in COMBINACIONES:
            yield pytest.param(action, nombre, nombre in permitidos, id=f"{action}-{nombre}")


def _casos_funcion():
    for fn_name, (_, action) in FUNCIONES.items():
        for nombre in COMBINACIONES:
            esperado = nombre in POLITICA[action]
            yield pytest.param(fn_name, nombre, esperado, id=f"{fn_name}-{nombre}")


def _miembro_de(nombre: str) -> tuple[MagicMock, Settings]:
    roles, administrator, manage_guild, ceo_configurado = COMBINACIONES[nombre]
    settings = _settings(CEO_GENERAL if ceo_configurado else 0)
    return _member(roles, administrator=administrator, manage_guild=manage_guild), settings


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "combinacion", "esperado"), list(_casos_accion()))
async def test_has_staff_access_matriz(action, combinacion, esperado):
    member, settings = _miembro_de(combinacion)
    assert await has_staff_access(_interaction(member), action, settings) is esperado


@pytest.mark.asyncio
@pytest.mark.parametrize(("fn_name", "combinacion", "esperado"), list(_casos_funcion()))
async def test_funciones_publicas_matriz(fn_name, combinacion, esperado):
    fn, _ = FUNCIONES[fn_name]
    member, settings = _miembro_de(combinacion)
    # Mismo resultado pasando la interacción o el miembro directamente.
    assert await fn(_interaction(member), settings) is esperado
    assert await fn(member, settings) is esperado


def test_allowed_role_ids_no_incluye_ceo_general_sin_configurar():
    assert 0 not in allowed_role_ids(StaffAction.ROLES_Y_PLANTILLAS, _settings(0))
    assert allowed_role_ids(StaffAction.ROLES_Y_PLANTILLAS, _settings()) == {
        STAFF,
        ADMIN,
        CEO_GENERAL,
    }


# ---------------------------------------------------------------------------
# resolve_member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_member_devuelve_member_directo():
    member = _member()
    assert await resolve_member(member) is member


@pytest.mark.asyncio
async def test_resolve_member_usa_user_de_la_interaccion():
    member = _member()
    assert await resolve_member(_interaction(member)) is member


@pytest.mark.asyncio
async def test_resolve_member_consulta_cache_y_api_para_user():
    user = MagicMock(spec=discord.User)
    user.id = 42
    member = _member()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_member.return_value = None
    guild.fetch_member = AsyncMock(return_value=member)

    assert await resolve_member(_interaction(user, guild)) is member
    guild.get_member.assert_called_once_with(42)
    guild.fetch_member.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_resolve_member_en_mensaje_directo_devuelve_none():
    user = MagicMock(spec=discord.User)
    user.id = 42
    assert await resolve_member(_interaction(user, None)) is None


@pytest.mark.asyncio
async def test_resolve_member_no_acepta_objetos_que_solo_tienen_roles():
    """Un objeto con `roles` que no es discord.Member no se trata como miembro."""

    class ConRoles:
        roles = [_role(STAFF)]

    assert await resolve_member(ConRoles()) is None
    assert await is_staff(ConRoles(), _settings()) is False


# ---------------------------------------------------------------------------
# !sync aplica la misma política que /sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("combinacion", list(COMBINACIONES))
async def test_sync_prefijo_usa_la_politica_de_sincronizacion(combinacion):
    from liga_bot.cogs.admin import AdminCog

    member, settings = _miembro_de(combinacion)
    bot = MagicMock()
    bot.settings = settings
    cog = AdminCog(bot, settings)
    ctx = MagicMock()
    ctx.author = member
    ctx.cog = cog

    esperado = combinacion in POLITICA[StaffAction.SINCRONIZACION]
    checks = cog.sync_prefix.checks
    assert len(checks) == 1
    if esperado:
        assert await checks[0](ctx) is True
    else:
        with pytest.raises(discord.ext.commands.CheckFailure):
            await checks[0](ctx)
