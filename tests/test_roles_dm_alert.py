"""
Pruebas exhaustivas para la funcionalidad de Alerta de Moderación
en Fallo de DM de Bienvenida (Requisito R1).

Cubre:
1. Configuración de Settings.moderators_channel_id (por defecto y override por env).
2. Constructores de embeds de alerta (dorado para DMs bloqueados, rojo para errores inesperados).
3. Servicio RoleService.send_welcome_dm:
   - Éxito en envío de DM (sin alerta, retorna True).
   - DMs bloqueados / cerrados (discord.Forbidden -> alerta dorada a #moderators-only).
   - DMs bloqueados con código 50007 (discord.HTTPException -> alerta dorada).
   - Errores HTTP inesperados (status 500, 429 -> alerta roja con traceback en código).
   - Excepciones genéricas inesperadas (RuntimeError, ConnectionResetError -> alerta roja).
   - Resolución resiliente del canal (#moderators-only) vía get_channel / fetch_channel.
   - Degradación defensiva ante canal no encontrado o fallos en send (sin excepciones).
4. Integración en RoleService.handle_member_join garantizando incorporación ininterrumpida.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from liga_bot.config import Settings
from liga_bot.services.role_service import RoleService
from liga_bot.ui.roles import (
    build_welcome_dm_blocked_embed,
    build_welcome_dm_error_embed,
)

# ---------------------------------------------------------------------------
# Mocks y Fixtures Locales
# ---------------------------------------------------------------------------


def create_mock_role(role_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.Role."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_channel(channel_id: int, name: str = "moderators-only") -> AsyncMock:
    """Crea un mock asíncrono de discord.TextChannel."""
    chan = AsyncMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return chan


def create_mock_member(
    user_id: int = 123456789,
    name: str = "TestMember",
    display_name: str | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.Member."""
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = display_name or name
    member.mention = f"<@{user_id}>"
    member.guild = guild
    member.add_roles = AsyncMock()
    member.send = AsyncMock()
    return member


def create_mock_guild(
    settings: Settings,
    channels: list[AsyncMock] | None = None,
    roles: list[MagicMock] | None = None,
) -> MagicMock:
    """Crea un mock de discord.Guild con mapa de canales y roles."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = settings.guild_id
    guild.name = "RCL Test Server"

    # Roles
    sin_verificar_role = (
        create_mock_role(settings.sin_verificar_role_id, "Sin Verificar")
        if settings.sin_verificar_role_id
        else None
    )
    all_roles = [sin_verificar_role] if sin_verificar_role else []
    if roles:
        all_roles.extend(roles)
    role_map = {r.id: r for r in all_roles if r is not None}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_map.get(rid)

    # Canales
    channel_list = list(channels or [])
    channel_map = {c.id: c for c in channel_list}
    guild.channels = channel_list
    guild.get_channel.side_effect = lambda cid: channel_map.get(cid)

    return guild


# ---------------------------------------------------------------------------
# 1. Pruebas de Configuración
# ---------------------------------------------------------------------------


def test_moderators_channel_id_default_value():
    """Verifica que moderators_channel_id tenga por defecto el ID canónico 1548038711697080494."""
    settings = Settings()
    assert settings.moderators_channel_id == 1548038711697080494


def test_moderators_channel_id_env_override(monkeypatch):
    """Verifica que MODERATORS_CHANNEL_ID sobrescriba el valor por defecto."""
    monkeypatch.setenv("MODERATORS_CHANNEL_ID", "987654321012345678")
    settings = Settings()
    assert settings.moderators_channel_id == 987654321012345678


# ---------------------------------------------------------------------------
# 2. Pruebas de Embeds de Alerta
# ---------------------------------------------------------------------------


def test_build_welcome_dm_blocked_embed():
    """Valida la estructura, colores, campos y mención del embed dorado de DMs bloqueados."""
    member = create_mock_member(user_id=11223344, name="PrivacyUser", display_name="PrivacyNick")
    embed = build_welcome_dm_blocked_embed(member)

    assert embed.color == discord.Color.gold()
    assert "Alerta de Moderación" in (embed.title or "")
    assert member.mention in (embed.description or "")
    assert "PrivacyNick" in (embed.description or "")
    assert "/pedir-rol" in (embed.description or "")
    assert embed.timestamp is not None
    assert embed.footer is not None
    assert "Moderación" in (embed.footer.text or "")

    # Verificar campos adicionales
    field_names = [f.name for f in embed.fields]
    assert "Usuario" in field_names
    assert "ID de Usuario" in field_names


def test_build_welcome_dm_error_embed():
    """Valida la estructura, colores y bloque de código del embed rojo de error inesperado."""
    member = create_mock_member(user_id=55667788, name="ErrorUser", display_name="ErrorNick")
    exc = RuntimeError("Simulated network timeout during send")
    embed = build_welcome_dm_error_embed(member, exc)

    assert embed.color == discord.Color.red()
    assert "Alerta de Sistema" in (embed.title or "")
    assert member.mention in (embed.description or "")
    assert "ErrorNick" in (embed.description or "")
    assert embed.timestamp is not None
    assert embed.footer is not None
    assert "Alerta de Sistema" in (embed.footer.text or "")

    # Verificar que el mensaje de error está formateado dentro de un bloque de código
    fields_content = " ".join([f.value or "" for f in embed.fields])
    assert "```RuntimeError: Simulated network timeout during send```" in fields_content


def test_build_welcome_dm_error_embed_sanitizes_backticks():
    """Valida que las triples comillas invertidas en el mensaje de error sean saneadas."""
    member = create_mock_member(user_id=55667788, name="BacktickUser")
    exc = RuntimeError("Error containing ```internal markdown```")
    embed = build_welcome_dm_error_embed(member, exc)

    fields_content = " ".join([f.value or "" for f in embed.fields])
    assert "```RuntimeError: Error containing '''internal markdown'''```" in fields_content


# ---------------------------------------------------------------------------
# 3. Pruebas del Método send_welcome_dm en RoleService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_welcome_dm_success():
    """Cuando el DM se envía con éxito, retorna True y no se envía ninguna alerta."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=123, name="HappyUser", guild=guild)

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)

    assert result is True
    member.send.assert_awaited_once()
    # No se debe enviar alerta si el DM fue exitoso
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_send_welcome_dm_forbidden_sends_gold_alert():
    """Si member.send lanza discord.Forbidden, envía embed dorado a moderadores y retorna False."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=124, name="BlockedUser", guild=guild)

    mock_resp = MagicMock(status=403, reason="Forbidden")
    member.send.side_effect = discord.Forbidden(mock_resp, "Cannot send messages to this user")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)

    assert result is False
    member.send.assert_awaited_once()
    mod_channel.send.assert_awaited_once()

    sent_kwargs = mod_channel.send.await_args.kwargs
    sent_embed = sent_kwargs.get("embed")
    assert sent_embed is not None
    assert sent_embed.color == discord.Color.gold()
    assert "Alerta de Moderación" in sent_embed.title


@pytest.mark.asyncio
async def test_send_welcome_dm_code_50007_sends_gold_alert():
    """Si member.send lanza discord.HTTPException con code=50007, envía alerta dorada."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=125, name="Code50007User", guild=guild)

    mock_resp = MagicMock(status=400, reason="Bad Request")
    http_exc = discord.HTTPException(mock_resp, "Cannot send messages to this user")
    http_exc.code = 50007
    member.send.side_effect = http_exc

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)

    assert result is False
    mod_channel.send.assert_awaited_once()
    sent_embed = mod_channel.send.await_args.kwargs.get("embed")
    assert sent_embed is not None
    assert sent_embed.color == discord.Color.gold()


@pytest.mark.asyncio
async def test_send_welcome_dm_http_500_sends_red_alert():
    """Si member.send lanza HTTPException con status 500, envía alerta roja."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=126, name="ServerOutageUser", guild=guild)

    mock_resp = MagicMock(status=500, reason="Internal Server Error")
    member.send.side_effect = discord.HTTPException(mock_resp, "Discord API 500 Server Error")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)

    assert result is False
    mod_channel.send.assert_awaited_once()
    sent_embed = mod_channel.send.await_args.kwargs.get("embed")
    assert sent_embed is not None
    assert sent_embed.color == discord.Color.red()
    assert "Alerta de Sistema" in sent_embed.title
    fields_content = " ".join([f.value or "" for f in sent_embed.fields])
    assert "HTTPException" in fields_content
    assert "500 Server Error" in fields_content


@pytest.mark.asyncio
async def test_send_welcome_dm_rate_limited_429_sends_red_alert():
    """Si member.send lanza rate limit 429, envía alerta roja."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=127, name="RateLimitedUser", guild=guild)

    mock_resp = MagicMock(status=429, reason="Too Many Requests")
    member.send.side_effect = discord.HTTPException(mock_resp, "Rate limited")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)

    assert result is False
    mod_channel.send.assert_awaited_once()
    sent_embed = mod_channel.send.await_args.kwargs.get("embed")
    assert sent_embed is not None
    assert sent_embed.color == discord.Color.red()


@pytest.mark.asyncio
async def test_send_welcome_dm_generic_exception_sends_red_alert():
    """Si member.send lanza una excepción no Discord (ej. OSError/Timeout), envía alerta roja."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=128, name="NetworkFailUser", guild=guild)

    member.send.side_effect = ConnectionResetError("Connection lost abruptly")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)

    assert result is False
    mod_channel.send.assert_awaited_once()
    sent_embed = mod_channel.send.await_args.kwargs.get("embed")
    assert sent_embed is not None
    assert sent_embed.color == discord.Color.red()
    fields_content = " ".join([f.value or "" for f in sent_embed.fields])
    assert "ConnectionResetError: Connection lost abruptly" in fields_content


# ---------------------------------------------------------------------------
# 4. Pruebas de Resolución Resiliente de Canales y Manejo Defensivo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_resolution_via_guild_fetch_channel():
    """Si el canal no está en la caché del guild, se resuelve vía guild.fetch_channel."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[])  # Vacío en caché

    guild.fetch_channel = AsyncMock(return_value=mod_channel)
    member = create_mock_member(user_id=129, name="FetchUser", guild=guild)
    member.send.side_effect = discord.Forbidden(MagicMock(status=403), "Closed DMs")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)

    assert result is False
    guild.fetch_channel.assert_awaited_once_with(settings.moderators_channel_id)
    mod_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_resolution_via_bot_get_channel():
    """Si guild no tiene el canal, se resuelve a través de bot.get_channel."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[])
    guild.fetch_channel = AsyncMock(return_value=None)
    member = create_mock_member(user_id=130, name="BotCacheUser", guild=guild)
    member.send.side_effect = discord.Forbidden(MagicMock(status=403), "Closed DMs")

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=mod_channel)

    service = RoleService(
        session_factory=MagicMock(spec=async_sessionmaker),
        settings=settings,
        bot=mock_bot,
    )
    result = await service.send_welcome_dm(member)

    assert result is False
    mock_bot.get_channel.assert_called_once_with(settings.moderators_channel_id)
    mod_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_resolution_via_bot_fetch_channel():
    """Si guild ni bot.get_channel tienen el canal, se resuelve vía bot.fetch_channel."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[])
    guild.fetch_channel = AsyncMock(return_value=None)
    member = create_mock_member(user_id=130, name="BotFallbackUser", guild=guild)
    member.send.side_effect = discord.Forbidden(MagicMock(status=403), "Closed DMs")

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=None)
    mock_bot.fetch_channel = AsyncMock(return_value=mod_channel)

    service = RoleService(
        session_factory=MagicMock(spec=async_sessionmaker),
        settings=settings,
        bot=mock_bot,
    )
    result = await service.send_welcome_dm(member)

    assert result is False
    mock_bot.fetch_channel.assert_awaited_once_with(settings.moderators_channel_id)
    mod_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_not_found_defensive_never_raises():
    """Si el canal de moderación no existe en absoluto, no lanza error y retorna False."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    guild = create_mock_guild(settings, channels=[])
    not_found_exc = discord.NotFound(MagicMock(status=404), "Not found")
    guild.fetch_channel = AsyncMock(side_effect=not_found_exc)
    member = create_mock_member(user_id=131, name="OrphanUser", guild=guild)
    member.send.side_effect = discord.Forbidden(MagicMock(status=403), "Closed DMs")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    # No debe levantar ninguna excepción
    result = await service.send_welcome_dm(member)
    assert result is False


@pytest.mark.asyncio
async def test_channel_send_failure_defensive_never_raises():
    """Si channel.send en moderadores falla (ej. sin permisos en el canal), no lanza excepción."""
    settings = Settings(moderators_channel_id=1548038711697080494)
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    mod_channel.send.side_effect = discord.Forbidden(
        MagicMock(status=403), "Missing Permissions in mod channel"
    )
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=132, name="CrashTestUser", guild=guild)
    member.send.side_effect = discord.Forbidden(MagicMock(status=403), "Closed DMs")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    # Debe capturar el error de mod_channel.send y no levantar
    result = await service.send_welcome_dm(member)
    assert result is False


@pytest.mark.asyncio
async def test_moderators_channel_id_zero_skips_alert():
    """Si moderators_channel_id está en 0 (desactivado), se omite el envío de alerta."""
    settings = Settings(moderators_channel_id=0)
    guild = create_mock_guild(settings, channels=[])
    member = create_mock_member(user_id=133, name="ZeroConfigUser", guild=guild)
    member.send.side_effect = discord.Forbidden(MagicMock(status=403), "Closed DMs")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    result = await service.send_welcome_dm(member)
    assert result is False


@pytest.mark.asyncio
async def test_send_welcome_dm_when_member_guild_is_none():
    """
    Verifica que send_welcome_dm con member.guild = None no lance AttributeError,
    resuelva el nombre del servidor de forma defensiva y maneje la situación con elegancia.
    """
    settings = Settings(moderators_channel_id=1548038711697080494)
    member = create_mock_member(user_id=137, name="NoGuildUser", guild=None)

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)

    # 1. Caso de éxito: member.send es llamado y se usa el nombre por defecto 'la liga'
    result = await service.send_welcome_dm(member)
    assert result is True
    member.send.assert_awaited_once()
    send_args = member.send.await_args[0]
    assert "¡Bienvenido/a a **la liga**!" in send_args[0]

    # 2. Caso de fallo: member.send lanza Forbidden -> retorna False sin lanzar excepción
    member.send.reset_mock()
    member.send.side_effect = discord.Forbidden(MagicMock(status=403), "Closed DMs")
    result_fail = await service.send_welcome_dm(member)
    assert result_fail is False


# ---------------------------------------------------------------------------
# 5. Pruebas de Integración con handle_member_join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_member_join_delegates_to_send_welcome_dm():
    """Verifica que handle_member_join delega explícitamente en send_welcome_dm."""
    settings = Settings(sin_verificar_role_id=0)
    guild = create_mock_guild(settings)
    member = create_mock_member(user_id=134, name="DelegateUser", guild=guild)

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    with patch.object(service, "send_welcome_dm", new_callable=AsyncMock) as mock_send_dm:
        mock_send_dm.return_value = True
        success = await service.handle_member_join(member)

        assert success is True
        mock_send_dm.assert_awaited_once_with(member)


@pytest.mark.asyncio
async def test_handle_member_join_e2e_blocked_dms_never_blocks_onboarding():
    """
    Flujo completo de incorporación:
    - Miembro entra al servidor.
    - Se le asigna el rol 'Sin Verificar'.
    - DM falla por DMs cerrados.
    - Se despacha la alerta dorada al canal de moderación.
    - handle_member_join retorna True (onboarding completado con éxito a pesar del DM).
    """
    settings = Settings(
        sin_verificar_role_id=888999,
        moderators_channel_id=1548038711697080494,
    )
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=135, name="OnboardingUser", guild=guild)

    mock_resp = MagicMock(status=403, reason="Forbidden")
    member.send.side_effect = discord.Forbidden(mock_resp, "Cannot send messages to this user")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    success = await service.handle_member_join(member)

    assert success is True
    # Rol asignado
    role = guild.get_role(settings.sin_verificar_role_id)
    member.add_roles.assert_awaited_once_with(role)
    # Alerta dorada enviada a moderadores
    mod_channel.send.assert_awaited_once()
    sent_embed = mod_channel.send.await_args.kwargs.get("embed")
    assert sent_embed is not None
    assert sent_embed.color == discord.Color.gold()


@pytest.mark.asyncio
async def test_handle_member_join_e2e_total_failure_in_dm_and_channel_never_blocks_onboarding():
    """
    Caso de degradación crítica:
    - Falla el DM del miembro (status 500).
    - Falla también el envío al canal de moderación (status 403 Forbidden).
    - handle_member_join NO DEBE fallar, debe devolver True y asignar el rol.
    """
    settings = Settings(
        sin_verificar_role_id=888999,
        moderators_channel_id=1548038711697080494,
    )
    mod_channel = create_mock_channel(settings.moderators_channel_id)
    forbidden_exc = discord.Forbidden(MagicMock(status=403), "Bot lacks channel perms")
    mod_channel.send.side_effect = forbidden_exc
    guild = create_mock_guild(settings, channels=[mod_channel])
    member = create_mock_member(user_id=136, name="DoubleFailUser", guild=guild)

    member.send.side_effect = discord.HTTPException(MagicMock(status=500), "Server error")

    service = RoleService(session_factory=MagicMock(spec=async_sessionmaker), settings=settings)
    success = await service.handle_member_join(member)

    assert success is True
    role = guild.get_role(settings.sin_verificar_role_id)
    member.add_roles.assert_awaited_once_with(role)
    mod_channel.send.assert_awaited_once()
