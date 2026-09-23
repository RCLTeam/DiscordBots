"""Pruebas de resiliencia, casos límite y condiciones de borde para SuggestionService."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import ClientOSError

from liga_bot.config import Settings
from liga_bot.services.suggestion_service import (
    SuggestionDeliveryError,
    SuggestionService,
)


@pytest.fixture
def mock_bot():
    bot = MagicMock(spec=discord.Client)
    bot.get_channel = MagicMock()
    bot.fetch_channel = AsyncMock()
    return bot


@pytest.fixture
def mock_channel():
    channel = MagicMock()
    channel.id = 123456789
    sent_message = MagicMock()
    sent_message.id = 987654321
    sent_message.add_reaction = AsyncMock()
    channel.send = AsyncMock(return_value=sent_message)
    return channel, sent_message


# ==============================================================================
# Vector 1: Channel Resolution & Channel State Failures
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_channel_id", [0, -1, -999999999, None])
async def test_adv_channel_id_non_positive_or_none(mock_bot, invalid_channel_id):
    """Verify non-positive or None channel IDs raise SuggestionDeliveryError immediately."""
    settings = Settings(suggestions_channel_id=invalid_channel_id or 0)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="no está configurado"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Hello world",
        )
    mock_bot.get_channel.assert_not_called()
    mock_bot.fetch_channel.assert_not_called()


@pytest.mark.asyncio
async def test_adv_fetch_channel_discord_http_404(mock_bot):
    """Verify Discord 404 (channel deleted) during fetch_channel raises SuggestionDeliveryError."""
    mock_bot.get_channel.return_value = None
    mock_bot.fetch_channel.side_effect = discord.NotFound(
        MagicMock(status=404, reason="Not Found"), "Unknown Channel"
    )

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="No se encontró el canal de sugerencias"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Hello",
        )


@pytest.mark.asyncio
async def test_adv_fetch_channel_discord_http_403_forbidden(mock_bot):
    """Verify Discord 403 (bot cannot view channel) raises SuggestionDeliveryError."""
    mock_bot.get_channel.return_value = None
    mock_bot.fetch_channel.side_effect = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"), "Missing Access"
    )

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="No se encontró el canal de sugerencias"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Hello",
        )


@pytest.mark.asyncio
async def test_adv_fetch_channel_network_timeout(mock_bot):
    """Verify asyncio.TimeoutError during fetch_channel is captured as SuggestionDeliveryError."""
    mock_bot.get_channel.return_value = None
    mock_bot.fetch_channel.side_effect = asyncio.TimeoutError("Fetch timed out")

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="No se encontró el canal de sugerencias"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Hello",
        )


@pytest.mark.asyncio
async def test_adv_channel_send_is_none_or_non_callable(mock_bot):
    """Verify channels where 'send' attribute is None or non-callable
    raise SuggestionDeliveryError.
    """
    channel = MagicMock()
    channel.send = None  # non-callable
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="no admite envío de mensajes"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Hello",
        )


@pytest.mark.asyncio
async def test_adv_channel_send_is_string_attribute(mock_bot):
    """Verify channels where 'send' attribute is a string raise SuggestionDeliveryError."""
    channel = MagicMock()
    channel.send = "not-a-callable-method"
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="no admite envío de mensajes"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Hello",
        )


# ==============================================================================
# Vector 2: Send Message & Discord API / Permission Failures
# ==============================================================================


@pytest.mark.asyncio
async def test_adv_send_discord_forbidden_missing_send_messages(mock_bot, mock_channel):
    """Verify Discord 403 Forbidden on channel.send raises SuggestionDeliveryError."""
    channel, _ = mock_channel
    channel.send.side_effect = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"), "Missing Permissions: Send Messages"
    )
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="Error al enviar mensaje al canal"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_adv_send_discord_forbidden_missing_embed_links(mock_bot, mock_channel):
    """Verify Discord 403 Forbidden for missing Embed Links raises SuggestionDeliveryError."""
    channel, _ = mock_channel
    channel.send.side_effect = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"), "Missing Permissions: Embed Links"
    )
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="Error al enviar mensaje al canal"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_adv_send_discord_rate_limit_429(mock_bot, mock_channel):
    """Verify Discord 429 Rate Limit raises SuggestionDeliveryError."""
    channel, _ = mock_channel
    channel.send.side_effect = discord.HTTPException(
        MagicMock(status=429, reason="Too Many Requests"), "Rate Limited"
    )
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="Error al enviar mensaje al canal"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_adv_send_discord_server_error_503(mock_bot, mock_channel):
    """Verify Discord 503 Service Unavailable raises SuggestionDeliveryError."""
    channel, _ = mock_channel
    channel.send.side_effect = discord.HTTPException(
        MagicMock(status=503, reason="Service Unavailable"), "Discord Server Error"
    )
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="Error al enviar mensaje al canal"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_adv_send_network_client_os_error(mock_bot, mock_channel):
    """Verify low-level socket / OS disconnect raises SuggestionDeliveryError."""
    channel, _ = mock_channel
    channel.send.side_effect = ClientOSError(104, "Connection reset by peer")
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="Error al enviar mensaje al canal"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="Test",
        )


# ==============================================================================
# Vector 3: Reaction Resilience & Partial Failure Tolerances
# ==============================================================================


@pytest.mark.asyncio
async def test_adv_reaction_first_fails_forbidden_second_succeeds(mock_bot, mock_channel):
    """Verify that when 👍 fails with Forbidden, 👎 is still attempted and post succeeds."""
    channel, sent_message = mock_channel
    mock_bot.get_channel.return_value = channel

    sent_message.add_reaction.side_effect = [
        discord.Forbidden(MagicMock(status=403), "Missing Add Reactions"),
        None,  # 👎 succeeds
    ]

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    msg_id, ch_id = await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion="Valid suggestion",
    )

    assert msg_id == 987654321
    assert ch_id == 123456789
    assert sent_message.add_reaction.await_count == 2
    sent_message.add_reaction.assert_any_await("👍")
    sent_message.add_reaction.assert_any_await("👎")


@pytest.mark.asyncio
async def test_adv_reaction_both_fail_discord_404_message_deleted(mock_bot, mock_channel):
    """Verify that if message is instantly deleted by AutoMod (404), post still returns IDs."""
    channel, sent_message = mock_channel
    mock_bot.get_channel.return_value = channel

    sent_message.add_reaction.side_effect = discord.NotFound(
        MagicMock(status=404), "Unknown Message"
    )

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    msg_id, ch_id = await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion="Valid suggestion",
    )

    assert msg_id == 987654321
    assert ch_id == 123456789
    assert sent_message.add_reaction.await_count == 2


@pytest.mark.asyncio
async def test_adv_reaction_raises_unexpected_exception(mock_bot, mock_channel):
    """Verify arbitrary unexpected exception during reactions is logged
    and does not fail delivery.
    """
    channel, sent_message = mock_channel
    mock_bot.get_channel.return_value = channel

    sent_message.add_reaction.side_effect = TypeError("Unexpected NoneType in discord internal")

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    msg_id, ch_id = await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion="Valid suggestion",
    )

    assert msg_id == 987654321
    assert ch_id == 123456789


# ==============================================================================
# Vector 4: Large Suggestion Bodies, Boundaries, and Author Sanitization
# ==============================================================================


@pytest.mark.asyncio
async def test_adv_suggestion_exact_4096_chars(mock_bot, mock_channel):
    """Verify suggestion text of exactly 4096 characters (Discord embed description max limit)."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    massive_text = "A" * 4096
    msg_id, ch_id = await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion=massive_text,
    )

    assert msg_id == 987654321
    embed = channel.send.call_args[1]["embed"]
    assert len(embed.description) == 4096
    assert embed.description == massive_text


@pytest.mark.asyncio
async def test_adv_suggestion_over_4096_chars_discord_http_400(mock_bot, mock_channel):
    """Verify oversized suggestion that Discord rejects with 400 Bad Request
    raises SuggestionDeliveryError.
    """
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    channel.send.side_effect = discord.HTTPException(
        MagicMock(status=400, reason="Bad Request"),
        "Invalid Form Body: In embed.description: Must be 4096 or fewer in length.",
    )

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="Error al enviar mensaje al canal"):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion="B" * 5000,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "empty_text",
    ["", "   ", "\n\t  \r\n"],
)
async def test_adv_empty_or_whitespace_suggestion(mock_bot, empty_text):
    """Verify empty or purely whitespace suggestion strings raise SuggestionDeliveryError."""
    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(
        SuggestionDeliveryError, match="El texto de la sugerencia no puede estar vacío"
    ):
        await service.post_suggestion(
            author_id="123",
            author_username="User",
            suggestion=empty_text,
        )


@pytest.mark.asyncio
async def test_adv_zwsp_unicode_suggestion_delivered(mock_bot, mock_channel):
    """Verify Zero-Width Space (\\u200b) is preserved and delivered to Discord."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    msg_id, ch_id = await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion=" \u200b ",
    )

    assert msg_id == 987654321
    embed = channel.send.call_args[1]["embed"]
    assert embed.description == "\u200b"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_author", ["", "   ", "\t\n"])
async def test_adv_empty_author_id(mock_bot, empty_author):
    """Verify empty or purely whitespace author_id raises SuggestionDeliveryError."""
    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(
        SuggestionDeliveryError, match="El autor de la sugerencia no puede estar vacío"
    ):
        await service.post_suggestion(
            author_id=empty_author,
            author_username="User",
            suggestion="Hello",
        )


@pytest.mark.asyncio
async def test_adv_numeric_author_id_and_empty_username_fallback(mock_bot, mock_channel):
    """Verify numeric author_id is stringified and empty username falls back to 'Anónimo'."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    await service.post_suggestion(
        author_id=987654321098765432,
        author_username="   ",
        suggestion="Valid suggestion",
    )

    embed = channel.send.call_args[1]["embed"]
    assert embed.author.name == "Anónimo"
    assert "<@987654321098765432>" in embed.fields[0].value
    assert "(Anónimo)" in embed.fields[0].value


# ==============================================================================
# Vector 5: Avatar URL Scheme Hardening & Malicious Schemes
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malicious_url",
    [
        "javascript:alert(document.cookie)",
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxzY3JpcHQ+YWxlcnQoMSk8L3NjcmlwdD48L3N2Zz4=",
        "file:///etc/passwd",
        "ftp://mirror.example.com/pub/avatar.png",
        "blob:https://discord.com/97821634",
        "//protocol-relative.example.com/avatar.png",
        "chrome-extension://someid/icon.png",
        "git://github.com/repo.git",
    ],
)
async def test_adv_avatar_url_disallowed_schemes(mock_bot, mock_channel, malicious_url):
    """Verify non-http/https URL schemes are strictly excluded from icon_url."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    await service.post_suggestion(
        author_id="123",
        author_username="SecurityTester",
        suggestion="Test avatar scheme",
        avatar_url=malicious_url,
    )

    embed = channel.send.call_args[1]["embed"]
    assert embed.author.icon_url is None, (
        f"Scheme in '{malicious_url}' should not be set as icon_url"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "valid_url",
    [
        "http://example.com/avatar.png",
        "https://cdn.discordapp.com/avatars/12345/abcdef.webp?size=1024",
        "https://sub.domain.org/path/to/img.jpg?param=val#hash",
    ],
)
async def test_adv_avatar_url_allowed_schemes(mock_bot, mock_channel, valid_url):
    """Verify valid http and https URLs are preserved as icon_url."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion="Test avatar valid",
        avatar_url=valid_url,
    )

    embed = channel.send.call_args[1]["embed"]
    assert embed.author.icon_url == valid_url


# ==============================================================================
# Vector 6: Timestamp Formats, Timezones, and Parsing Resilience
# ==============================================================================


@pytest.mark.asyncio
async def test_adv_timestamp_naive_datetime(mock_bot, mock_channel):
    """Verify naive datetime object gets tzinfo=UTC without shifting time."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    naive_dt = datetime(2026, 9, 23, 14, 30, 45)
    await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion="Naive test",
        created_at=naive_dt,
    )

    embed = channel.send.call_args[1]["embed"]
    assert embed.timestamp == naive_dt.replace(tzinfo=timezone.utc)
    assert embed.timestamp.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_adv_timestamp_iso_variants(mock_bot, mock_channel):
    """Verify parsing of diverse ISO-8601 string variants."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    # 1. Lowercase 'z'
    await service.post_suggestion(
        author_id="1", author_username="U", suggestion="S", created_at="2026-09-23T10:00:00z"
    )
    e1 = channel.send.call_args[1]["embed"]
    assert e1.timestamp == datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)

    # 2. Offset +02:00
    await service.post_suggestion(
        author_id="1", author_username="U", suggestion="S", created_at="2026-09-23T12:00:00+02:00"
    )
    e2 = channel.send.call_args[1]["embed"]
    assert e2.timestamp.utcoffset().total_seconds() == 7200

    # 3. Space-separated ISO format
    await service.post_suggestion(
        author_id="1", author_username="U", suggestion="S", created_at="2026-09-23 15:45:00"
    )
    e3 = channel.send.call_args[1]["embed"]
    assert e3.timestamp == datetime(2026, 9, 23, 15, 45, 0, tzinfo=timezone.utc)

    # 4. Fractional seconds with Z
    await service.post_suggestion(
        author_id="1",
        author_username="U",
        suggestion="S",
        created_at="2026-09-23T18:30:15.123456Z",
    )
    e4 = channel.send.call_args[1]["embed"]
    assert e4.timestamp == datetime(2026, 9, 23, 18, 30, 15, 123456, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_timestamp",
    [
        "not-a-timestamp",
        "2026-99-99T99:99:99Z",
        "23/09/2026 15:00",
        "",
        "   ",
        1727110800,  # epoch integer
    ],
)
async def test_adv_timestamp_corrupted_fallback(mock_bot, mock_channel, corrupt_timestamp):
    """Verify corrupted timestamps gracefully fall back to current UTC datetime."""
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    before = datetime.now(timezone.utc)
    await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion="Corrupted TS",
        created_at=corrupt_timestamp,
    )
    after = datetime.now(timezone.utc)

    embed = channel.send.call_args[1]["embed"]
    assert embed.timestamp is not None
    assert before <= embed.timestamp <= after


# ==============================================================================
# Vector 7: Parameter Interoperability (suggestion vs content)
# ==============================================================================


@pytest.mark.asyncio
async def test_adv_suggestion_and_content_precedence(mock_bot, mock_channel):
    """Verify that when both 'suggestion' and 'content' are provided,
    'suggestion' takes precedence.
    """
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    await service.post_suggestion(
        author_id="123",
        author_username="User",
        suggestion="Primary Suggestion Text",
        content="Secondary Content Text",
    )

    embed = channel.send.call_args[1]["embed"]
    assert embed.description == "Primary Suggestion Text"
