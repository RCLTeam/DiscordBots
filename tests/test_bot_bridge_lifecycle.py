"""Pruebas del ciclo de vida e integración de WebSocket Bridge en LigaBot."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from discord.ext import commands

from liga_bot.bot import LigaBot
from liga_bot.config import Settings
from liga_bot.services.suggestion_service import SuggestionService
from liga_bot.services.websocket_bridge_service import WebsocketBridgeService


@pytest.mark.asyncio
async def test_setup_hook_starts_bridge_when_enabled():
    """Verifica que setup_hook instancia e inicia el WebSocket Bridge cuando bridge_enabled=True."""
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock) as mock_start,
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()

        # Comprobar inicialización e inyección mutua de dependencias
        assert isinstance(bot.suggestion_service, SuggestionService)
        assert isinstance(bot.websocket_bridge_service, WebsocketBridgeService)
        assert bot.suggestion_service.bot is bot
        assert bot.websocket_bridge_service.bot is bot
        assert bot.websocket_bridge_service.suggestion_service is bot.suggestion_service

        # Comprobar inicio del bridge
        mock_start.assert_awaited_once()

        # Cierre ordenado
        await bot.close()
        mock_stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None


@pytest.mark.asyncio
async def test_setup_hook_skips_bridge_when_disabled():
    """Verifica que setup_hook no inicia el WebSocket Bridge cuando bridge_enabled=False."""
    settings = Settings(bridge_enabled=False)
    bot = LigaBot(settings=settings, extensions=())

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock) as mock_start,
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()

        # El servicio se instancia para consistencia del contrato de interfaz, pero no se inicia
        assert isinstance(bot.suggestion_service, SuggestionService)
        assert isinstance(bot.websocket_bridge_service, WebsocketBridgeService)
        mock_start.assert_not_called()

        await bot.close()
        mock_stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None


@pytest.mark.asyncio
async def test_setup_hook_preserves_injected_services():
    """Verifica que setup_hook respeta instancias pre-inyectadas (por atributo o constructor)."""
    # 1. Inyección por atributos
    mock_custom_suggestion = MagicMock(spec=SuggestionService)
    mock_custom_bridge = MagicMock(spec=WebsocketBridgeService)
    mock_custom_bridge.start = AsyncMock()
    mock_custom_bridge.stop = AsyncMock()

    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())
    bot.suggestion_service = mock_custom_suggestion
    bot.websocket_bridge_service = mock_custom_bridge

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()

        assert bot.suggestion_service is mock_custom_suggestion
        assert bot.websocket_bridge_service is mock_custom_bridge
        mock_custom_bridge.start.assert_awaited_once()

        await bot.close()
        mock_custom_bridge.stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None

    # 2. Inyección mediante constructor (kwargs)
    mock_sugg_ctor = MagicMock(spec=SuggestionService)
    mock_bridge_ctor = MagicMock(spec=WebsocketBridgeService)
    mock_bridge_ctor.start = AsyncMock()
    mock_bridge_ctor.stop = AsyncMock()

    bot_ctor = LigaBot(
        settings=settings,
        extensions=(),
        suggestion_service=mock_sugg_ctor,
        websocket_bridge_service=mock_bridge_ctor,
    )
    assert bot_ctor.suggestion_service is mock_sugg_ctor
    assert bot_ctor.websocket_bridge_service is mock_bridge_ctor

    # 3. Inyección parcial (solo suggestion_service pre-inyectado)
    bot_partial = LigaBot(
        settings=Settings(bridge_enabled=False),
        extensions=(),
        suggestion_service=mock_custom_suggestion,
    )
    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot_partial.setup_hook()
        assert bot_partial.suggestion_service is mock_custom_suggestion
        assert isinstance(bot_partial.websocket_bridge_service, WebsocketBridgeService)
        assert bot_partial.websocket_bridge_service.suggestion_service is mock_custom_suggestion
        await bot_partial.close()


@pytest.mark.asyncio
async def test_close_stops_bridge_service():
    """Verifica que bot.close() detiene el bridge y libera su referencia a None."""
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock) as mock_start,
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()
        mock_start.assert_awaited_once()
        assert bot.websocket_bridge_service is not None

        # Ejecutar cierre
        await bot.close()
        mock_stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None


@pytest.mark.asyncio
async def test_close_idempotent_when_called_multiple_times():
    """Verifica la idempotencia de close() bajo llamadas secuenciales, concurrentes y no init."""
    settings = Settings(bridge_enabled=True)

    # Caso 1: Múltiples llamadas secuenciales tras setup_hook
    bot_seq = LigaBot(settings=settings, extensions=())
    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop_seq,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot_seq.setup_hook()

        for _ in range(5):
            await bot_seq.close()

        # stop() debe llamarse exactamente 1 vez
        mock_stop_seq.assert_awaited_once()
        assert bot_seq.websocket_bridge_service is None

    # Caso 2: Llamada a close() cuando setup_hook nunca fue ejecutado
    bot_uninit = LigaBot(extensions=())
    with (
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop_uninit,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        assert bot_uninit.websocket_bridge_service is None
        await bot_uninit.close()
        await bot_uninit.close()
        mock_stop_uninit.assert_not_called()
        assert bot_uninit.websocket_bridge_service is None

    # Caso 3: Llamadas concurrentes simultáneas (asyncio.gather)
    bot_concurrent = LigaBot(settings=settings, extensions=())
    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(
            WebsocketBridgeService, "stop", new_callable=AsyncMock
        ) as mock_stop_concurrent,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot_concurrent.setup_hook()
        results = await asyncio.gather(
            bot_concurrent.close(),
            bot_concurrent.close(),
            bot_concurrent.close(),
            bot_concurrent.close(),
            bot_concurrent.close(),
            return_exceptions=True,
        )
        for res in results:
            assert res is None or not isinstance(res, Exception), f"Error en close: {res}"

        mock_stop_concurrent.assert_awaited_once()
        assert bot_concurrent.websocket_bridge_service is None


@pytest.mark.asyncio
async def test_close_bridge_failure_does_not_block_teardown():
    """Verifica que un fallo al detener bridge no bloquea liberar base de datos ni Discord."""
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())
    mock_engine = MagicMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(
            WebsocketBridgeService,
            "stop",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Simulated bridge failure during stop"),
        ) as mock_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
    ):
        mock_get_engine.return_value = mock_engine
        await bot.setup_hook()

        # Debe ejecutar close sin propagar la excepción hacia arriba
        await bot.close()

        mock_stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None
        mock_close_engine.assert_awaited_once_with(mock_engine)
        assert bot.engine is None
        mock_super_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_real_bridge_socket_lifecycle_integration():
    """Prueba empírica de integración de socket real en puerto efímero (bridge_port=0)."""
    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="empirical-secret",
    )
    bot = LigaBot(settings=settings, extensions=())

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()
        assert bot.websocket_bridge_service is not None
        assert bot.websocket_bridge_service.is_running is True

        port = bot.websocket_bridge_service.port
        assert port > 0

        # Verificar respuesta HTTP real del endpoint /health
        url = f"http://127.0.0.1:{port}/health"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
                assert "connections" in data

        # Cierre ordenado
        await bot.close()
        assert bot.websocket_bridge_service is None
