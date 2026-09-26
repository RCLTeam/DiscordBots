"""Pruebas unitarias e integración para WebsocketBridgeService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from liga_bot.config import Settings
from liga_bot.services.rate_limiter import SlidingWindowRateLimiter
from liga_bot.services.suggestion_service import (
    SuggestionDeliveryError,
    SuggestionService,
)
from liga_bot.services.websocket_bridge_service import WebsocketBridgeService


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.is_ready.return_value = True
    return bot


@pytest.fixture
def mock_suggestion_service():
    service = MagicMock(spec=SuggestionService)
    service.post_suggestion = AsyncMock(return_value=(987654321, 123456789))
    return service


@pytest.fixture
def base_settings():
    return Settings(
        bridge_enabled=True,
        bridge_host="127.0.0.1",
        bridge_port=0,  # Puerto efímero asignado por el kernel
        discord_bot_supertoken="valid-secret-token",
        suggestions_channel_id=123456789,
        bridge_rate_limit_per_minute=10,
    )


@pytest.fixture
async def running_service(mock_bot, mock_suggestion_service, base_settings):
    limiter = SlidingWindowRateLimiter(limit=10, window_seconds=60.0)
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=base_settings,
        suggestion_service=mock_suggestion_service,
        rate_limiter=limiter,
        auth_timeout_seconds=0.2,  # Rápido para tests
    )
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


# ---------------------------------------------------------------------------
# Tests: Lifecycle & Health Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_response(running_service):
    url = f"http://127.0.0.1:{running_service.port}/health"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["bot_ready"] is True
            assert data["active_connections"] == 0
            assert data["connections"] == 0


@pytest.mark.asyncio
async def test_health_tracks_active_connections(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    health_url = f"http://127.0.0.1:{running_service.port}/health"

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url):
            async with session.get(health_url) as resp:
                data = await resp.json()
                assert data["active_connections"] == 1

            async with session.ws_connect(ws_url):
                async with session.get(health_url) as resp2:
                    data2 = await resp2.json()
                    assert data2["active_connections"] == 2

        # Tras cerrar ambos sockets
        await asyncio.sleep(0.05)
        async with session.get(health_url) as resp3:
            data3 = await resp3.json()
            assert data3["active_connections"] == 0


@pytest.mark.asyncio
async def test_bridge_disabled_does_not_start(mock_bot, mock_suggestion_service):
    settings = Settings(bridge_enabled=False, bridge_port=0)
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
    )
    await service.start()
    assert service.is_running is False
    await service.stop()


@pytest.mark.asyncio
async def test_bridge_start_and_stop_idempotency(running_service):
    # start() ya ejecutado
    assert running_service.is_running is True
    await running_service.start()  # No-op seguro
    assert running_service.is_running is True

    await running_service.stop()
    assert running_service.is_running is False
    await running_service.stop()  # No-op seguro
    assert running_service.is_running is False


# ---------------------------------------------------------------------------
# Tests: Pre-Auth Silence & Mandatory UUID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_auth_silence_on_missing_uuid(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            # Trama sin UUID
            await ws.send_json({"type": "LOG IN", "data": {"content": "valid-secret-token"}})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive(), timeout=0.1)


@pytest.mark.asyncio
async def test_pre_auth_silence_on_invalid_uuid(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            await ws.send_json(
                {
                    "type": "LOG IN",
                    "data": {"id": "not-a-uuid", "content": "valid-secret-token"},
                }
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive(), timeout=0.1)


@pytest.mark.asyncio
async def test_pre_auth_silence_on_malformed_json(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            await ws.send_str("{invalid json payload")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive(), timeout=0.1)


@pytest.mark.asyncio
async def test_pre_auth_silence_on_non_login_command(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            # SUGGESTION_CREATED antes de autenticar -> silencio total
            await ws.send_json(
                {
                    "type": "SUGGESTION_CREATED",
                    "data": {
                        "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                        "content": {
                            "suggestion": "Prueba",
                        },
                    },
                }
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive(), timeout=0.1)


@pytest.mark.asyncio
async def test_pre_auth_silence_on_invalid_token(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            await ws.send_json(
                {
                    "type": "LOG IN",
                    "data": {
                        "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                        "content": {
                            "token": "wrong-token",
                        },
                    },
                }
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive(), timeout=0.1)


@pytest.mark.asyncio
async def test_auth_empty_configured_supertoken_security(mock_bot, mock_suggestion_service):
    # Supertoken no configurado ("") -> no se debe permitir autenticación
    settings = Settings(bridge_enabled=True, bridge_port=0, discord_bot_supertoken="")
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
        auth_timeout_seconds=0.2,
    )
    await service.start()
    try:
        ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                await ws.send_json(
                    {
                        "type": "LOG IN",
                        "data": {
                            "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                            "content": {
                                "token": "",
                            },
                        },
                    }
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive(), timeout=0.1)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_auth_timeout_closes_socket_with_code_4001(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            # Esperar a que el timeout de 0.2s cierre el socket
            msg = await ws.receive()
            assert msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            )
            assert ws.close_code == 4001


# ---------------------------------------------------------------------------
# Tests: Authentication Success & Token Extraction Variants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_success_content_dict(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    uuid_str = "e4b3c2a1-0000-4000-8000-0123456789ab"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            await ws.send_json(
                {
                    "type": "LOG IN",
                    "data": {
                        "id": uuid_str,
                        "content": {
                            "token": "valid-secret-token",
                        },
                    },
                }
            )
            resp = await ws.receive_json()
            assert resp["type"] == "LOGIN_SUCCESS"
            assert resp["data"]["id"] == uuid_str
            assert resp["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_success_fallback_data_id_and_token(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    uuid_str = "123e4567-e89b-12d3-a456-426614174000"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            await ws.send_json(
                {
                    "type": "LOGIN",
                    "data": {
                        "id": uuid_str,
                        "token": "valid-secret-token",
                    },
                }
            )
            resp = await ws.receive_json()
            assert resp["type"] == "LOGIN_SUCCESS"
            assert resp["data"]["id"] == uuid_str
            assert resp["data"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Tests: Two-Phase Delivery & Rate Limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_phase_delivery_success(running_service, mock_suggestion_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    sugg_uuid = "c7a8b9d0-1111-4222-9333-abcdef012345"

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            # 1. Login
            await ws.send_json(
                {
                    "type": "LOG IN",
                    "data": {
                        "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                        "content": {
                            "token": "valid-secret-token",
                        },
                    },
                }
            )
            login_resp = await ws.receive_json()
            assert login_resp["type"] == "LOGIN_SUCCESS"

            # 2. Enviar SUGGESTION_CREATED
            await ws.send_json(
                {
                    "type": "SUGGESTION_CREATED",
                    "data": {
                        "id": sugg_uuid,
                        "content": {
                            "author_id": "999888",
                            "author_username": "ProGamer",
                            "suggestion": "Añadir más torneos los fines de semana",
                            "avatar_url": "https://example.com/avatar.png",
                            "created_at": "2026-09-23T18:00:00Z",
                        },
                    },
                }
            )

            # Fase 1: Inmediato QUEUED
            resp_queued = await ws.receive_json()
            assert resp_queued["type"] == "QUEUED"
            assert resp_queued["data"] == {"id": sugg_uuid}

            # Fase 2: Asíncrono SUGGESTION_CONFIRMED
            resp_confirmed = await ws.receive_json()
            assert resp_confirmed["type"] == "SUGGESTION_CONFIRMED"
            assert resp_confirmed["data"]["id"] == sugg_uuid
            assert resp_confirmed["data"]["message_id"] == 987654321
            assert resp_confirmed["data"]["channel_id"] == 123456789

            # Verificar argumentos pasados a SuggestionService
            mock_suggestion_service.post_suggestion.assert_awaited_once_with(
                author_id="999888",
                author_username="ProGamer",
                suggestion="Añadir más torneos los fines de semana",
                avatar_url="https://example.com/avatar.png",
                created_at="2026-09-23T18:00:00Z",
            )


@pytest.mark.asyncio
async def test_two_phase_delivery_discord_error(running_service, mock_suggestion_service):
    mock_suggestion_service.post_suggestion.side_effect = SuggestionDeliveryError(
        "Canal no encontrado"
    )
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    sugg_uuid = "c7a8b9d0-1111-4222-9333-abcdef012345"

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            # Login
            await ws.send_json(
                {
                    "type": "LOG IN",
                    "data": {
                        "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                        "content": {
                            "token": "valid-secret-token",
                        },
                    },
                }
            )
            await ws.receive_json()

            # Enviar sugerencia
            await ws.send_json(
                {
                    "type": "SUGGESTION_CREATED",
                    "data": {
                        "id": sugg_uuid,
                        "content": {
                            "suggestion": "Test",
                        },
                    },
                }
            )

            # Fase 1: QUEUED
            r_q = await ws.receive_json()
            assert r_q["type"] == "QUEUED"

            # Fase 2: FAILED con DISCORD_ERROR
            r_f = await ws.receive_json()
            assert r_f["type"] == "SUGGESTION_FAILED"
            assert r_f["data"]["id"] == sugg_uuid
            assert r_f["data"]["code"] == "DISCORD_ERROR"
            assert "Canal no encontrado" in r_f["data"]["message"]


@pytest.mark.asyncio
async def test_rate_limiter_blocks_exceeded_requests(
    mock_bot, mock_suggestion_service, base_settings
):
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=base_settings,
        suggestion_service=mock_suggestion_service,
        rate_limiter=limiter,
        auth_timeout_seconds=10.0,
    )
    await service.start()
    try:
        ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Login
                await ws.send_json(
                    {
                        "type": "LOG IN",
                        "data": {
                            "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                            "content": {
                                "token": "valid-secret-token",
                            },
                        },
                    }
                )
                await ws.receive_json()

                # Petición 1: Aprobada
                req1 = "11111111-1111-4111-8111-111111111111"
                await ws.send_json(
                    {
                        "type": "SUGGESTION_CREATED",
                        "data": {"id": req1, "content": {"suggestion": "S1"}},
                    }
                )
                r1_q = await ws.receive_json()
                assert r1_q["type"] == "QUEUED"
                r1_c = await ws.receive_json()
                assert r1_c["type"] == "SUGGESTION_CONFIRMED"

                # Petición 2: Bloqueada por Rate Limiting
                req2 = "22222222-2222-4222-8222-222222222222"
                await ws.send_json(
                    {
                        "type": "SUGGESTION_CREATED",
                        "data": {"id": req2, "content": {"suggestion": "S2"}},
                    }
                )
                r2_err = await ws.receive_json()
                assert r2_err["type"] == "ERROR"
                assert r2_err["data"]["id"] == req2
                assert r2_err["data"]["code"] == "RATE_LIMITED"
                assert r2_err["data"]["retry_after_seconds"] > 0

                # SuggestionService solo se llamó 1 vez
                assert mock_suggestion_service.post_suggestion.await_count == 1
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_socket_disconnect_during_inflight_delivery(running_service, mock_suggestion_service):
    async def delayed_post(**kwargs):
        await asyncio.sleep(0.08)
        return (123, 456)

    mock_suggestion_service.post_suggestion.side_effect = delayed_post
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    sugg_uuid = "33333333-3333-4333-8333-333333333333"

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            # Login
            await ws.send_json(
                {
                    "type": "LOG IN",
                    "data": {
                        "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                        "content": {
                            "token": "valid-secret-token",
                        },
                    },
                }
            )
            await ws.receive_json()

            # Enviar sugerencia
            await ws.send_json(
                {
                    "type": "SUGGESTION_CREATED",
                    "data": {"id": sugg_uuid, "content": {"suggestion": "Fast close"}},
                }
            )
            r_q = await ws.receive_json()
            assert r_q["type"] == "QUEUED"

            # Cliente se desconecta abruptamente antes de la confirmación
            await ws.close()

    # Esperar a que la tarea en segundo plano finalice sin fallar
    await asyncio.sleep(0.12)
    assert len(running_service._background_tasks) == 0


@pytest.mark.asyncio
async def test_stop_closes_sockets_with_code_1000(running_service):
    ws_url = f"http://127.0.0.1:{running_service.port}/ws/bridge"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            # Detener el servicio mientras el socket está activo
            await running_service.stop()

            msg = await ws.receive()
            assert msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            )
            assert ws.close_code == 1000


@pytest.mark.asyncio
async def test_missing_suggestion_service_fails_gracefully(mock_bot, base_settings):
    # Servicio instanciado sin SuggestionService
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=base_settings,
        suggestion_service=None,
    )
    await service.start()
    try:
        ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
        sugg_uuid = "44444444-4444-4444-8444-444444444444"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Login
                await ws.send_json(
                    {
                        "type": "LOG IN",
                        "data": {
                            "id": "e4b3c2a1-0000-4000-8000-0123456789ab",
                            "content": {
                                "token": "valid-secret-token",
                            },
                        },
                    }
                )
                await ws.receive_json()

                # Enviar sugerencia
                await ws.send_json(
                    {
                        "type": "SUGGESTION_CREATED",
                        "data": {"id": sugg_uuid, "content": {"suggestion": "No service"}},
                    }
                )
                r_q = await ws.receive_json()
                assert r_q["type"] == "QUEUED"

                r_f = await ws.receive_json()
                assert r_f["type"] == "SUGGESTION_FAILED"
                assert r_f["data"]["id"] == sugg_uuid
                assert r_f["data"]["code"] == "DISCORD_ERROR"
    finally:
        await service.stop()
