"""
Tests for /stream_url and /stream_url_live slash commands in ScheduleCog.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from liga_bot.cogs.schedule import ScheduleCog
from liga_bot.config import Settings
from liga_bot.services.schedule_service import ScheduleService, StreamUrlResult


def create_mock_role(role_id: int, name: str = "Rol") -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_member(
    user_id: int,
    name: str = "Usuario",
    roles: list[MagicMock] | None = None,
    is_admin: bool = False,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = is_admin
    return member


def create_mock_guild(
    guild_id: int = 1547725310508667010,
    name: str = "Liga Discord",
) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    return guild


def create_mock_interaction(
    user: discord.Member | discord.User | None = None,
    guild: discord.Guild | None = None,
) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.guild = guild
    inter.user = user
    inter.response = MagicMock(spec=discord.InteractionResponse)
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock(spec=discord.Webhook)
    inter.followup.send = AsyncMock()
    inter.client = MagicMock()
    return inter


@pytest.mark.asyncio
async def test_stream_url_command_success() -> None:
    """Verifica asignación exitosa de VOD con /stream_url."""
    settings = Settings(staff_role_id=101)
    service = MagicMock(spec=ScheduleService)
    service.set_stream_url = AsyncMock(
        return_value=StreamUrlResult(
            success=True,
            url="https://twitch.tv/vod/123",
            is_live=False,
            team1_name="Team Alpha",
            team2_name="Team Beta",
            jornada=3,
        )
    )
    cog = ScheduleCog(bot=MagicMock(), schedule_service=service, settings=settings)

    member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()
    interaction = create_mock_interaction(user=member, guild=guild)

    role1 = create_mock_role(1001, "Team Alpha")
    role2 = create_mock_role(1002, "Team Beta")

    await cog.stream_url.callback(
        cog,
        interaction,
        equipo1=role1,
        equipo2=role2,
        url="https://twitch.tv/vod/123",
        jornada=3,
    )

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    service.set_stream_url.assert_awaited_once_with(
        role1_id=1001,
        role2_id=1002,
        url="https://twitch.tv/vod/123",
        is_live=False,
        jornada=3,
    )
    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.call_args
    assert kwargs.get("ephemeral") is True
    embed: discord.Embed = kwargs.get("embed")
    assert embed is not None
    assert embed.title == "✅ URL de VOD / Transmisión Asignada"
    assert embed.color == discord.Color.blue()
    assert "Team Alpha" in embed.description
    assert "Team Beta" in embed.description
    assert "3" in embed.description
    assert "https://twitch.tv/vod/123" in embed.description
    assert len(embed.fields) == 1
    assert embed.fields[0].name == "Enlace directo"
    assert embed.fields[0].value == "https://twitch.tv/vod/123"


@pytest.mark.asyncio
async def test_stream_url_live_command_success() -> None:
    """Verifica asignación exitosa de directo con /stream_url_live."""
    settings = Settings(staff_role_id=101)
    service = MagicMock(spec=ScheduleService)
    service.set_stream_url = AsyncMock(
        return_value=StreamUrlResult(
            success=True,
            url="https://twitch.tv/rcl_live",
            is_live=True,
            team1_name="Team Gamma",
            team2_name="Team Delta",
            jornada=1,
        )
    )
    cog = ScheduleCog(bot=MagicMock(), schedule_service=service, settings=settings)

    member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()
    interaction = create_mock_interaction(user=member, guild=guild)

    role1 = create_mock_role(2001, "Team Gamma")
    role2 = create_mock_role(2002, "Team Delta")

    await cog.stream_url_live.callback(
        cog,
        interaction,
        equipo1=role1,
        equipo2=role2,
        url="https://twitch.tv/rcl_live",
        jornada=None,
    )

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    service.set_stream_url.assert_awaited_once_with(
        role1_id=2001,
        role2_id=2002,
        url="https://twitch.tv/rcl_live",
        is_live=True,
        jornada=None,
    )
    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.call_args
    assert kwargs.get("ephemeral") is True
    embed: discord.Embed = kwargs.get("embed")
    assert embed is not None
    assert embed.title == "✅ URL de Directo (Live) Asignada"
    assert embed.color == discord.Color.purple()
    assert "Team Gamma" in embed.description
    assert "Team Delta" in embed.description
    assert "1" in embed.description
    assert "https://twitch.tv/rcl_live" in embed.description
    assert len(embed.fields) == 1
    assert embed.fields[0].name == "Enlace directo"
    assert embed.fields[0].value == "https://twitch.tv/rcl_live"


@pytest.mark.asyncio
async def test_stream_url_permission_denied() -> None:
    """Verifica rechazo de usuario sin permisos autorizados."""
    settings = Settings(staff_role_id=101)
    service = MagicMock(spec=ScheduleService)
    cog = ScheduleCog(bot=MagicMock(), schedule_service=service, settings=settings)

    unauth = create_mock_member(12345, roles=[create_mock_role(999)], is_admin=False)
    guild = create_mock_guild()
    interaction = create_mock_interaction(user=unauth, guild=guild)

    role1 = create_mock_role(1001)
    role2 = create_mock_role(1002)

    await cog.stream_url.callback(
        cog,
        interaction,
        equipo1=role1,
        equipo2=role2,
        url="https://twitch.tv/vod/123",
    )

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "No tienes permisos" in args[0]
    assert kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_called()
    service.set_stream_url.assert_not_called()


@pytest.mark.asyncio
async def test_stream_url_live_permission_denied() -> None:
    """Verifica rechazo de usuario sin permisos en /stream_url_live."""
    settings = Settings(staff_role_id=101)
    service = MagicMock(spec=ScheduleService)
    cog = ScheduleCog(bot=MagicMock(), schedule_service=service, settings=settings)

    unauth = create_mock_member(12345, roles=[create_mock_role(999)], is_admin=False)
    guild = create_mock_guild()
    interaction = create_mock_interaction(user=unauth, guild=guild)

    role1 = create_mock_role(1001)
    role2 = create_mock_role(1002)

    await cog.stream_url_live.callback(
        cog,
        interaction,
        equipo1=role1,
        equipo2=role2,
        url="https://twitch.tv/rcl_live",
    )

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "No tienes permisos" in args[0]
    assert kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_called()
    service.set_stream_url.assert_not_called()


@pytest.mark.asyncio
async def test_stream_url_dm_context_rejected() -> None:
    """Verifica que /stream_url rechaza ejecución fuera de un guild (en DM)."""
    service = MagicMock(spec=ScheduleService)
    cog = ScheduleCog(bot=MagicMock(), schedule_service=service)

    interaction = create_mock_interaction(guild=None)
    role1 = create_mock_role(1001)
    role2 = create_mock_role(1002)

    await cog.stream_url.callback(
        cog,
        interaction,
        equipo1=role1,
        equipo2=role2,
        url="https://twitch.tv/vod/123",
    )

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "solo puede ser ejecutado dentro de un servidor" in args[0]
    assert kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_called()
    service.set_stream_url.assert_not_called()


@pytest.mark.asyncio
async def test_stream_url_live_dm_context_rejected() -> None:
    """Verifica que /stream_url_live rechaza ejecución fuera de un guild (en DM)."""
    service = MagicMock(spec=ScheduleService)
    cog = ScheduleCog(bot=MagicMock(), schedule_service=service)

    interaction = create_mock_interaction(guild=None)
    role1 = create_mock_role(1001)
    role2 = create_mock_role(1002)

    await cog.stream_url_live.callback(
        cog,
        interaction,
        equipo1=role1,
        equipo2=role2,
        url="https://twitch.tv/rcl_live",
    )

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "solo puede ser ejecutado dentro de un servidor" in args[0]
    assert kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_called()
    service.set_stream_url.assert_not_called()


@pytest.mark.asyncio
async def test_stream_url_service_error_handling() -> None:
    """Verifica respuesta con embed de error cuando el servicio falla."""
    settings = Settings(staff_role_id=101)
    service = MagicMock(spec=ScheduleService)
    service.set_stream_url = AsyncMock(
        return_value=StreamUrlResult(
            success=False,
            url="https://twitch.tv/vod/123",
            is_live=False,
            error="No se encontró ningún enfrentamiento registrado entre **Alpha** y **Beta**.",
        )
    )
    cog = ScheduleCog(bot=MagicMock(), schedule_service=service, settings=settings)

    member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()
    interaction = create_mock_interaction(user=member, guild=guild)

    role1 = create_mock_role(1001)
    role2 = create_mock_role(1002)

    await cog.stream_url.callback(
        cog,
        interaction,
        equipo1=role1,
        equipo2=role2,
        url="https://twitch.tv/vod/123",
    )

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.call_args
    assert kwargs.get("ephemeral") is True
    embed: discord.Embed = kwargs.get("embed")
    assert embed is not None
    assert embed.title == "❌ Error al Asignar URL"
    assert embed.color == discord.Color.red()
    assert (
        embed.description
        == "No se encontró ningún enfrentamiento registrado entre **Alpha** y **Beta**."
    )


def test_stream_url_command_metadata() -> None:
    """Verifica metadatos de app_commands (permisos y descripciones)."""
    cog = ScheduleCog(bot=MagicMock(), schedule_service=MagicMock())

    assert cog.stream_url.name == "stream_url"
    assert cog.stream_url.default_permissions is not None
    assert cog.stream_url.default_permissions.manage_guild is True

    assert cog.stream_url_live.name == "stream_url_live"
    assert cog.stream_url_live.default_permissions is not None
    assert cog.stream_url_live.default_permissions.manage_guild is True
