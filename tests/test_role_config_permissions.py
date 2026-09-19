"""
Pruebas unitarias para la configuración de verificación de roles,
constantes de los 20 equipos oficiales y verificación de permisos administrativos (Staff / CEO).
"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import ValidationError

from liga_bot.cli import CANONICAL_DEFAULT_TEAMS
from liga_bot.cogs.permissions import is_staff, resolve_member
from liga_bot.config import (
    TEAMS_ALL,
    TEAMS_ASCEND,
    TEAMS_PREMIER,
    Settings,
)
from liga_bot.models.enums import Division

# ---------------------------------------------------------------------------
# Mocks auxiliares para pruebas de permisos
# ---------------------------------------------------------------------------


def make_mock_role(role_id: int, name: str = "Rol") -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_mock_member(
    user_id: int,
    name: str = "Usuario",
    roles: list[MagicMock] | None = None,
    is_admin: bool = False,
    can_manage_guild: bool = False,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = is_admin
    member.guild_permissions.manage_guild = can_manage_guild
    return member


def make_mock_interaction(
    user: discord.Member | discord.User | None = None,
    guild: discord.Guild | None = None,
) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.guild = guild
    inter.user = user
    return inter


# ---------------------------------------------------------------------------
# 1. Pruebas de Configuración (Settings)
# ---------------------------------------------------------------------------


def test_settings_role_verification_defaults():
    """Valida los valores por defecto de la configuración de verificación de roles."""
    settings = Settings()
    assert settings.ceo_role_id == 0
    assert settings.sin_verificar_role_id == 0
    assert settings.ticket_rol_category_id == 0
    assert settings.free_role_name == "Libre"


def test_settings_role_verification_env_overrides(monkeypatch):
    """Valida que las variables de entorno sobreescriban los campos de verificación de roles."""
    monkeypatch.setenv("CEO_ROLE_ID", "1548795781174009977")
    monkeypatch.setenv("SIN_VERIFICAR_ROLE_ID", "1550466708593180682")
    monkeypatch.setenv("TICKET_ROL_CATEGORY_ID", "1550476204014969022")
    monkeypatch.setenv("FREE_ROLE_NAME", "Agente Libre")

    settings = Settings()
    assert settings.ceo_role_id == 1548795781174009977
    assert settings.sin_verificar_role_id == 1550466708593180682
    assert settings.ticket_rol_category_id == 1550476204014969022
    assert settings.free_role_name == "Agente Libre"


@pytest.mark.parametrize(
    "field_name",
    ["ceo_role_id", "sin_verificar_role_id", "ticket_rol_category_id"],
)
def test_settings_role_verification_invalid_type(field_name: str):
    """Valida que valores no numéricos en campos enteros disparen ValidationError."""
    with pytest.raises(ValidationError):
        Settings(**{field_name: "not_a_number"})


# ---------------------------------------------------------------------------
# 2. Pruebas de Constantes de Equipos Oficiales
# ---------------------------------------------------------------------------


def test_teams_constants_counts_and_structure():
    """Valida la cardinalidad y estructura de TEAMS_PREMIER, TEAMS_ASCEND y TEAMS_ALL."""
    assert len(TEAMS_PREMIER) == 10
    assert len(TEAMS_ASCEND) == 10
    assert len(TEAMS_ALL) == 20
    assert TEAMS_ALL == TEAMS_PREMIER + TEAMS_ASCEND


def test_teams_constants_no_duplicates():
    """Valida que no existan nombres de equipos duplicados en la liga."""
    assert len(set(TEAMS_PREMIER)) == 10
    assert len(set(TEAMS_ASCEND)) == 10
    assert len(set(TEAMS_ALL)) == 20

    # Premier y Ascend deben ser disjuntos
    assert set(TEAMS_PREMIER).isdisjoint(set(TEAMS_ASCEND))


def test_teams_constants_canonical_names():
    """Valida la presencia de todos los nombres oficiales en sus respectivas divisiones."""
    expected_premier = {
        "Vanguard Gaming",
        "Nexus Esports",
        "Aegis Club",
        "Eclipse Gaming",
        "Apex Predators",
        "Storm Legion",
        "Titan Gaming",
        "Ironclad Esports",
        "Shadow Guard",
        "Valiant Esports",
    }
    expected_ascend = {
        "Frostbite Esports",
        "Infernal Gaming",
        "Thunder Squad",
        "Venomous Club",
        "Quantum Gaming",
        "Zephyr Esports",
        "Nova Core",
        "Crimson Tide",
        "Spectre Gaming",
        "Blaze Syndicate",
    }
    assert set(TEAMS_PREMIER) == expected_premier
    assert set(TEAMS_ASCEND) == expected_ascend


# ---------------------------------------------------------------------------
# 3. Pruebas de CANONICAL_DEFAULT_TEAMS en CLI
# ---------------------------------------------------------------------------


def test_canonical_default_teams_seed_alignment():
    """Valida que CANONICAL_DEFAULT_TEAMS contenga los 20 equipos alineados con las constantes."""
    assert len(CANONICAL_DEFAULT_TEAMS) == 20

    premier_teams = [t for t in CANONICAL_DEFAULT_TEAMS if t["division"] == Division.PREMIER]
    ascend_teams = [t for t in CANONICAL_DEFAULT_TEAMS if t["division"] == Division.ASCEND]

    assert len(premier_teams) == 10
    assert len(ascend_teams) == 10

    assert tuple(t["name"] for t in premier_teams) == TEAMS_PREMIER
    assert tuple(t["name"] for t in ascend_teams) == TEAMS_ASCEND

    # Validar unicidad de tags y roles
    tags = [t["tag"] for t in CANONICAL_DEFAULT_TEAMS]
    role_ids = [t["discord_role_id"] for t in CANONICAL_DEFAULT_TEAMS]

    assert len(set(tags)) == 20
    assert len(set(role_ids)) == 20

    for team_data in CANONICAL_DEFAULT_TEAMS:
        assert 1 <= len(team_data["tag"]) <= 4
        assert team_data["discord_role_id"] > 0


# ---------------------------------------------------------------------------
# 4. Pruebas de Verificación de Permisos (is_staff)
# ---------------------------------------------------------------------------


@pytest.fixture
def custom_settings() -> Settings:
    return Settings(
        staff_role_id=101,
        admin_role_id=102,
        ceo_role_id=103,
        ceo_premier_role_id=104,
        ceo_ascend_role_id=105,
    )


@pytest.mark.asyncio
async def test_is_staff_with_staff_role(custom_settings: Settings):
    """Valida que un miembro con staff_role_id sea reconocido como staff."""
    member = make_mock_member(1, roles=[make_mock_role(101)])
    inter = make_mock_interaction(user=member)

    # Invocación vía Interaction y vía Member directo
    assert await is_staff(inter, custom_settings) is True
    assert await is_staff(member, custom_settings) is True


@pytest.mark.asyncio
async def test_is_staff_with_ceo_role(custom_settings: Settings):
    """Valida que un miembro con ceo_role_id sea reconocido como staff."""
    member = make_mock_member(2, roles=[make_mock_role(103)])
    inter = make_mock_interaction(user=member)

    assert await is_staff(inter, custom_settings) is True
    assert await is_staff(member, custom_settings) is True


@pytest.mark.asyncio
async def test_is_staff_with_ceo_role_zero_default():
    """Valida que si ceo_role_id es 0 (default), un rol con id 0 no otorgue permisos de staff."""
    zero_settings = Settings(ceo_role_id=0, staff_role_id=999)
    member = make_mock_member(3, roles=[make_mock_role(0)])

    assert await is_staff(member, zero_settings) is False


@pytest.mark.asyncio
async def test_is_staff_with_administrator_permission(custom_settings: Settings):
    """Valida que un miembro con permiso nativo de administrador sea reconocido como staff."""
    member = make_mock_member(4, roles=[], is_admin=True)
    inter = make_mock_interaction(user=member)

    assert await is_staff(inter, custom_settings) is True
    assert await is_staff(member, custom_settings) is True


@pytest.mark.asyncio
async def test_is_staff_with_manage_guild_permission(custom_settings: Settings):
    """Valida que un miembro con permiso nativo manage_guild sea reconocido como staff."""
    member = make_mock_member(5, roles=[], can_manage_guild=True)
    inter = make_mock_interaction(user=member)

    assert await is_staff(inter, custom_settings) is True
    assert await is_staff(member, custom_settings) is True


@pytest.mark.asyncio
async def test_is_staff_with_neither(custom_settings: Settings):
    """Valida que un miembro sin roles ni permisos administrativos sea denegado."""
    member = make_mock_member(6, roles=[make_mock_role(999)])
    inter = make_mock_interaction(user=member)

    assert await is_staff(inter, custom_settings) is False
    assert await is_staff(member, custom_settings) is False


@pytest.mark.asyncio
async def test_is_staff_with_none_or_unresolvable(custom_settings: Settings):
    """Valida el manejo defensivo cuando el miembro es None o la interacción carece de usuario."""
    inter_empty = make_mock_interaction(user=None)
    assert await is_staff(inter_empty, custom_settings) is False

    # Invocación con None directo
    assert await is_staff(None, custom_settings) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_is_staff_with_discord_user_resolving_via_guild(custom_settings: Settings):
    """Valida la resolución de discord.User hacia discord.Member vía guild.fetch_member."""
    user = MagicMock(spec=discord.User)
    user.id = 77
    user.name = "UserExternal"

    resolved_member = make_mock_member(77, roles=[make_mock_role(101)])
    guild = MagicMock(spec=discord.Guild)
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=resolved_member)

    inter = make_mock_interaction(user=user, guild=guild)

    assert await is_staff(inter, custom_settings) is True
    guild.fetch_member.assert_awaited_once_with(77)


@pytest.mark.asyncio
async def test_resolve_member_direct():
    """Valida que resolve_member retorne el objeto discord.Member si se pasa directamente."""
    member = make_mock_member(42)
    assert await resolve_member(member) is member
    assert await resolve_member(None) is None  # type: ignore[arg-type]
