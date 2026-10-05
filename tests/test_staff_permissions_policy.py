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


# ---------------------------------------------------------------------------
# Tipo de acción que comprueba cada comando slash con efectos
# ---------------------------------------------------------------------------


def _comando(nombre: str):
    """Devuelve (cog, callback, argumentos) para invocar el comando sin efectos."""
    from discord import app_commands

    from liga_bot.cogs.admin import AdminCog
    from liga_bot.cogs.casters import CastersCog
    from liga_bot.cogs.roles import RolesCog
    from liga_bot.cogs.roster import RosterCog
    from liga_bot.cogs.schedule import ScheduleCog
    from liga_bot.cogs.teams import TeamsCog
    from liga_bot.cogs.tickets import TicketsCog

    settings = _settings()
    bot = MagicMock()
    bot.settings = settings
    rol = _role(JUGADOR)
    otro = _member()
    otro.id = 7
    archivo = MagicMock(spec=discord.Attachment)
    division = app_commands.Choice(name="Premier", value="PREMIER")

    tabla = {
        "registrar-equipo": (
            TeamsCog(bot, settings=settings),
            "registrar_equipo",
            (rol, "Equipo", "EQ", division),
        ),
        "revisar-tickets": (
            TicketsCog(bot, ticket_service=MagicMock(), auto_start=False),
            "revisar_tickets",
            (),
        ),
        "revisar-tickets-manual": (
            TicketsCog(bot, ticket_service=MagicMock(), auto_start=False),
            "revisar_tickets_manual",
            (),
        ),
        "sync": (AdminCog(bot, settings), "sync", ()),
        "sincronizar": (AdminCog(bot, settings), "sincronizar", ()),
        "asignar-rol": (RolesCog(bot), "asignar_rol", (otro, "Equipo", "nombre", "tag")),
        "publicar-panel-rol": (RolesCog(bot), "publicar_panel_rol", ()),
        "gestionar-posicion": (RosterCog(bot, settings), "gestionar_posicion", (otro,)),
        "liberar-jugador": (RosterCog(bot, settings), "liberar_jugador", (rol, otro)),
        "crear-partido": (ScheduleCog(bot, settings=settings), "crear_partido", (1, "A", "B")),
        "importar-jornada": (ScheduleCog(bot, settings=settings), "importar_jornada", (1, archivo)),
        "stream_url": (ScheduleCog(bot, settings=settings), "stream_url", (rol, rol, "https://x")),
        "panel-casters": (CastersCog(bot, settings=settings), "panel_casters", ()),
    }
    cog, attr, args = tabla[nombre]
    return cog, getattr(cog, attr).callback, args


ACCION_POR_COMANDO = {
    "registrar-equipo": StaffAction.ROLES_Y_PLANTILLAS,
    "asignar-rol": StaffAction.ROLES_Y_PLANTILLAS,
    "publicar-panel-rol": StaffAction.ROLES_Y_PLANTILLAS,
    "gestionar-posicion": StaffAction.ROLES_Y_PLANTILLAS,
    "liberar-jugador": StaffAction.ROLES_Y_PLANTILLAS,
    "revisar-tickets": StaffAction.TICKETS,
    "revisar-tickets-manual": StaffAction.TICKETS,
    "sync": StaffAction.SINCRONIZACION,
    "sincronizar": StaffAction.SINCRONIZACION,
    "crear-partido": StaffAction.CALENDARIO_Y_CASTERS,
    "importar-jornada": StaffAction.CALENDARIO_Y_CASTERS,
    "stream_url": StaffAction.CALENDARIO_Y_CASTERS,
    "panel-casters": StaffAction.CALENDARIO_Y_CASTERS,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("comando", "accion"), list(ACCION_POR_COMANDO.items()))
async def test_cada_comando_comprueba_su_tipo_de_accion(monkeypatch, comando, accion):
    import liga_bot.cogs.permissions as permissions
    import liga_bot.cogs.tickets as tickets

    espia = AsyncMock(return_value=False)
    monkeypatch.setattr(permissions, "has_staff_access", espia)
    monkeypatch.setattr(tickets, "has_staff_access", espia)

    cog, callback, args = _comando(comando)
    inter = _interaction(_member(), MagicMock(spec=discord.Guild))
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()

    await callback(cog, inter, *args)

    espia.assert_awaited_once()
    assert espia.await_args.args[1] is accion
    # Denegado: responde un mensaje efímero y no difiere la interacción.
    inter.response.defer.assert_not_awaited()
    assert inter.response.send_message.await_args.kwargs.get("ephemeral") is True
