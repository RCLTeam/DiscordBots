"""Pruebas unitarias para SuggestionService."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from liga_bot.config import Settings
from liga_bot.services.suggestion_service import (
    SuggestionDeliveryError,
    SuggestionService,
)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
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


def test_suggestion_service_init(mock_bot):
    settings = Settings(suggestions_channel_id=123)
    service = SuggestionService(bot=mock_bot, settings=settings)
    assert service.bot is mock_bot
    assert service.settings is settings


@pytest.mark.asyncio
async def test_post_suggestion_success_cached_channel(mock_bot, mock_channel):
    channel, sent_message = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    msg_id, ch_id = await service.post_suggestion(
        author_id="111222333",
        author_username="TestUser",
        suggestion="Sugerencia de prueba",
        avatar_url="https://example.com/avatar.png",
        created_at="2026-09-23T18:00:00Z",
    )

    assert msg_id == 987654321
    assert ch_id == 123456789
    mock_bot.get_channel.assert_called_once_with(123456789)
    mock_bot.fetch_channel.assert_not_called()
    channel.send.assert_awaited_once()

    # Inspeccionar embed
    _, kwargs = channel.send.call_args
    embed = kwargs.get("embed")
    assert embed is not None
    assert embed.title == "💡 Nueva Sugerencia"
    assert embed.description == "Sugerencia de prueba"
    assert embed.color.value == 0x5865F2
    assert embed.author.name == "TestUser"
    assert embed.author.icon_url == "https://example.com/avatar.png"
    assert embed.footer.text == "RCL • Sistema de Sugerencias"
    assert embed.fields[0].name == "Autor"
    assert "<@111222333>" in embed.fields[0].value

    # Reacciones añadidas
    assert sent_message.add_reaction.await_count == 2
    sent_message.add_reaction.assert_any_await("👍")
    sent_message.add_reaction.assert_any_await("👎")


@pytest.mark.asyncio
async def test_post_suggestion_success_fetch_channel_fallback(mock_bot, mock_channel):
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = None
    mock_bot.fetch_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    msg_id, ch_id = await service.post_suggestion(
        author_id=444555,
        author_username="FetchUser",
        suggestion="Fallback suggestion",
    )

    assert msg_id == 987654321
    assert ch_id == 123456789
    mock_bot.get_channel.assert_called_once_with(123456789)
    mock_bot.fetch_channel.assert_awaited_once_with(123456789)


@pytest.mark.asyncio
async def test_post_suggestion_channel_id_zero(mock_bot):
    settings = Settings(suggestions_channel_id=0)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="no está configurado"):
        await service.post_suggestion(
            author_id="111",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_post_suggestion_channel_id_negative(mock_bot):
    settings = Settings(suggestions_channel_id=-5)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="no está configurado"):
        await service.post_suggestion(
            author_id="111",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_post_suggestion_channel_not_found_fetch_error(mock_bot):
    mock_bot.get_channel.return_value = None
    mock_bot.fetch_channel.side_effect = RuntimeError("Channel does not exist")

    settings = Settings(suggestions_channel_id=999999)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="No se encontró el canal"):
        await service.post_suggestion(
            author_id="111",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_post_suggestion_channel_not_messageable(mock_bot):
    unmessageable_channel = MagicMock(spec=[])  # Sin método .send
    mock_bot.get_channel.return_value = unmessageable_channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="no admite envío de mensajes"):
        await service.post_suggestion(
            author_id="111",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_post_suggestion_send_exception(mock_bot):
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=RuntimeError("Discord API 500"))
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="Error al enviar mensaje"):
        await service.post_suggestion(
            author_id="111",
            author_username="User",
            suggestion="Test",
        )


@pytest.mark.asyncio
async def test_post_suggestion_content_kwarg_compatibility(mock_bot, mock_channel):
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    msg_id, ch_id = await service.post_suggestion(
        author_id="111",
        author_username="ContentUser",
        content="Compat content text",
    )
    assert msg_id == 987654321
    assert ch_id == 123456789


@pytest.mark.asyncio
async def test_post_suggestion_empty_suggestion_raises(mock_bot):
    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="no puede estar vacío"):
        await service.post_suggestion(
            author_id="111",
            author_username="User",
            suggestion="   ",
        )


@pytest.mark.asyncio
async def test_post_suggestion_empty_author_raises(mock_bot):
    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    with pytest.raises(SuggestionDeliveryError, match="El autor de la sugerencia no puede estar"):
        await service.post_suggestion(
            author_id="   ",
            author_username="User",
            suggestion="Valid suggestion",
        )


@pytest.mark.asyncio
async def test_post_suggestion_avatar_url_variants(mock_bot, mock_channel):
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel
    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    # 1. avatar_url=None
    await service.post_suggestion(
        author_id="1", author_username="U1", suggestion="S1", avatar_url=None
    )
    embed1 = channel.send.call_args[1]["embed"]
    assert embed1.author.icon_url is None

    # 2. avatar_url con esquema no http/https
    await service.post_suggestion(
        author_id="1",
        author_username="U2",
        suggestion="S2",
        avatar_url="ftp://example.com/avatar.png",
    )
    embed2 = channel.send.call_args[1]["embed"]
    assert embed2.author.icon_url is None

    # 3. avatar_url http
    await service.post_suggestion(
        author_id="1",
        author_username="U3",
        suggestion="S3",
        avatar_url="http://example.com/avatar.png",
    )
    embed3 = channel.send.call_args[1]["embed"]
    assert embed3.author.icon_url == "http://example.com/avatar.png"


@pytest.mark.asyncio
async def test_post_suggestion_created_at_parsing(mock_bot, mock_channel):
    channel, _ = mock_channel
    mock_bot.get_channel.return_value = channel
    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    # 1. datetime object
    dt = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    await service.post_suggestion(author_id="1", author_username="U", suggestion="S", created_at=dt)
    embed1 = channel.send.call_args[1]["embed"]
    assert embed1.timestamp == dt

    # 2. ISO string con 'Z'
    await service.post_suggestion(
        author_id="1",
        author_username="U",
        suggestion="S",
        created_at="2026-09-23T15:30:00Z",
    )
    embed2 = channel.send.call_args[1]["embed"]
    assert embed2.timestamp == datetime(2026, 9, 23, 15, 30, 0, tzinfo=timezone.utc)

    # 3. ISO string inválido -> fallback a now
    await service.post_suggestion(
        author_id="1",
        author_username="U",
        suggestion="S",
        created_at="invalid-date",
    )
    embed3 = channel.send.call_args[1]["embed"]
    assert embed3.timestamp is not None


@pytest.mark.asyncio
async def test_post_suggestion_reaction_failure_is_non_fatal(mock_bot, mock_channel):
    channel, sent_message = mock_channel
    sent_message.add_reaction.side_effect = RuntimeError("Rate limited or forbidden reactions")
    mock_bot.get_channel.return_value = channel

    settings = Settings(suggestions_channel_id=123456789)
    service = SuggestionService(bot=mock_bot, settings=settings)

    # No debe propagar la excepción de reacción, la entrega del mensaje se considera exitosa
    msg_id, ch_id = await service.post_suggestion(
        author_id="111",
        author_username="User",
        suggestion="Valid suggestion",
    )
    assert msg_id == 987654321
    assert ch_id == 123456789
