"""Pruebas de concurrencia, desconexiones y estrés para WebsocketBridgeService."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
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
    service.post_suggestion = AsyncMock(return_value=(888888, 777777))
    return service


# ===========================================================================
# Vector 1: Pre-login silence under frame flooding (50+ frames)
# ===========================================================================


@pytest.mark.asyncio
async def test_challenge_pre_login_silence_flood(mock_bot, mock_suggestion_service):
    """Flood unauthenticated socket with 60+ malformed, invalid, and non-login frames.

    Verification: Exactly 0 frames/bytes must be returned by the server.
    """
    settings = Settings(
        bridge_enabled=True,
        bridge_host="127.0.0.1",
        bridge_port=0,
        discord_bot_supertoken="super-secret-pass",
    )
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
        auth_timeout_seconds=5.0,
    )
    await service.start()

    try:
        ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                flood_payloads = []

                # 1. Non-JSON raw strings
                for i in range(15):
                    flood_payloads.append(f"malformed raw string payload {i} ::: {{{{{{")

                # 2. JSON without UUID
                for i in range(15):
                    flood_payloads.append(
                        json.dumps({"type": "LOG IN", "data": {"content": f"token-{i}"}})
                    )

                # 3. JSON with invalid UUID
                for i in range(15):
                    flood_payloads.append(
                        json.dumps(
                            {
                                "type": "SUGGESTION_CREATED",
                                "data": {
                                    "id": f"invalid-uuid-{i}",
                                    "content": {"suggestion": "flood"},
                                },
                            }
                        )
                    )

                # 4. JSON with valid UUID but wrong command (unauthenticated)
                for _ in range(15):
                    valid_u = str(uuid.uuid4())
                    flood_payloads.append(
                        json.dumps(
                            {
                                "type": "SUGGESTION_CREATED",
                                "data": {
                                    "id": valid_u,
                                    "content": {
                                        "suggestion": "pre-auth suggestion",
                                    },
                                },
                            }
                        )
                    )

                # 5. JSON with valid UUID and wrong token
                for _ in range(10):
                    valid_u = str(uuid.uuid4())
                    flood_payloads.append(
                        json.dumps(
                            {
                                "type": "LOG IN",
                                "data": {
                                    "id": valid_u,
                                    "content": {
                                        "token": "wrong-secret",
                                    },
                                },
                            }
                        )
                    )

                assert len(flood_payloads) == 70

                # Send all 70 frames in rapid burst
                for p in flood_payloads:
                    await ws.send_str(p)

                # Verify that 0 frames are received back
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive(), timeout=0.4)

                # Verify socket is still alive and we can authenticate now
                legit_u = str(uuid.uuid4())
                await ws.send_json(
                    {
                        "type": "LOG IN",
                        "data": {
                            "id": legit_u,
                            "content": {
                                "token": "super-secret-pass",
                            },
                        },
                    }
                )
                auth_resp = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                assert auth_resp["type"] == "LOGIN_SUCCESS"
                assert auth_resp["data"]["id"] == legit_u
    finally:
        await service.stop()


# ===========================================================================
# Vector 2: Auth timeout (code 4001)
# ===========================================================================


@pytest.mark.asyncio
async def test_challenge_auth_timeout_disconnects_with_4001(mock_bot, mock_suggestion_service):
    """Verify that unauthenticated connection is forcibly closed with code 4001 after timeout."""
    settings = Settings(
        bridge_enabled=True,
        bridge_host="127.0.0.1",
        bridge_port=0,
        discord_bot_supertoken="super-secret-pass",
    )
    # Using 0.3s timeout for deterministic fast execution
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
        auth_timeout_seconds=0.3,
    )
    await service.start()

    try:
        ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Do not send anything; wait for close frame
                msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                assert msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                )
                assert ws.close_code == 4001
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_challenge_auth_timeout_not_postponed_by_invalid_traffic(
    mock_bot, mock_suggestion_service
):
    """Verify that sending traffic without authenticating does NOT postpone the 4001 timeout."""
    settings = Settings(
        bridge_enabled=True,
        bridge_host="127.0.0.1",
        bridge_port=0,
        discord_bot_supertoken="super-secret-pass",
    )
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
        auth_timeout_seconds=0.4,
    )
    await service.start()

    try:
        ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Continuously send unauthenticated frames for 0.2s
                for _ in range(5):
                    await ws.send_json(
                        {
                            "type": "LOG IN",
                            "data": {
                                "id": str(uuid.uuid4()),
                                "content": {
                                    "token": "bad-token",
                                },
                            },
                        }
                    )
                    await asyncio.sleep(0.04)

                # Wait for timeout close
                msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                assert msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                )
                assert ws.close_code == 4001
    finally:
        await service.stop()


# ===========================================================================
# Vector 3: Empty supertoken security bypass prevention
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_supertoken, client_token",
    [
        ("", ""),
        ("   ", ""),
        ("", "   "),
        ("   ", "   "),
        ("", "any-token"),
        ("", None),
        ("valid-token", ""),
        ("valid-token", "   "),
    ],
)
async def test_challenge_empty_supertoken_bypass_prevention(
    mock_bot, mock_suggestion_service, config_supertoken, client_token
):
    """Verify that empty supertoken in settings or client token cannot authenticate."""
    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken=config_supertoken,
    )
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
                payload = {
                    "type": "LOG IN",
                    "data": {
                        "id": str(uuid.uuid4()),
                        "content": {
                            "token": client_token,
                        },
                    },
                }
                await ws.send_json(payload)

                # Server must remain silent until closed by timeout
                msg = await asyncio.wait_for(ws.receive(), timeout=0.4)
                assert msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                )
                assert ws.close_code == 4001
    finally:
        await service.stop()


# ===========================================================================
# Vector 4: Two-phase delivery under rapid client disconnect & race conditions
# ===========================================================================


@pytest.mark.asyncio
async def test_challenge_disconnect_immediately_after_suggestion_queued(
    mock_bot, mock_suggestion_service
):
    """Client disconnects immediately after receiving SUGGESTION_QUEUED during active delivery.

    Server must complete delivery or clean up background task with ZERO unhandled exceptions.
    """
    delivery_event = asyncio.Event()

    async def slow_post(**kwargs):
        await asyncio.sleep(0.1)
        delivery_event.set()
        return (9999, 8888)

    mock_suggestion_service.post_suggestion.side_effect = slow_post

    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="test-secret",
    )
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
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
                            "id": str(uuid.uuid4()),
                            "content": {
                                "token": "test-secret",
                            },
                        },
                    }
                )
                await ws.receive_json()

                # Send suggestion
                req_id = str(uuid.uuid4())
                await ws.send_json(
                    {
                        "type": "SUGGESTION_CREATED",
                        "data": {
                            "id": req_id,
                            "content": {
                                "author_id": "123",
                                "author_username": "Racer",
                                "suggestion": "Race condition test",
                            },
                        },
                    }
                )

                queued = await ws.receive_json()
                assert queued["type"] == "SUGGESTION_QUEUED"

                # Abrupt client close right now
                await ws.close()

        # Wait for delivery to complete in background
        await asyncio.wait_for(delivery_event.wait(), timeout=1.0)
        # Give event loop a tick to run task cleanup callback
        await asyncio.sleep(0.05)
        assert len(service._background_tasks) == 0
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_challenge_disconnect_with_discord_error_in_flight(mock_bot, mock_suggestion_service):
    """Client disconnects while Discord delivery is failing with SuggestionDeliveryError."""
    mock_suggestion_service.post_suggestion.side_effect = SuggestionDeliveryError(
        "Simulated Discord failure"
    )

    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="test-secret",
    )
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
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
                            "id": str(uuid.uuid4()),
                            "content": {
                                "token": "test-secret",
                            },
                        },
                    }
                )
                await ws.receive_json()

                await ws.send_json(
                    {
                        "type": "SUGGESTION_CREATED",
                        "data": {
                            "id": str(uuid.uuid4()),
                            "content": {
                                "suggestion": "Fail test",
                            },
                        },
                    }
                )
                await ws.receive_json()  # SUGGESTION_QUEUED
                await ws.close()

        await asyncio.sleep(0.05)
        assert len(service._background_tasks) == 0
    finally:
        await service.stop()


# ===========================================================================
# Vector 5: Multi-client concurrency & shared sliding window rate limiter
# ===========================================================================


@pytest.mark.asyncio
async def test_challenge_multi_client_rate_limiter_concurrency(mock_bot, mock_suggestion_service):
    """10 concurrent clients connect and concurrently send SUGGESTION_CREATED.

    With global rate limit = 6 per minute:
    Exactly 6 must succeed (QUEUED + CONFIRMED).
    Exactly 4 must be rate-limited (ERROR with RATE_LIMITED).
    Zero lost or hung requests.
    """
    shared_limiter = SlidingWindowRateLimiter(limit=6, window_seconds=60.0)
    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="multi-client-secret",
        bridge_rate_limit_per_minute=6,
    )
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
        rate_limiter=shared_limiter,
    )
    await service.start()

    client_count = 10
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def client_worker(client_id: int):
        ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # 1. Login
                await ws.send_json(
                    {
                        "type": "LOG IN",
                        "data": {
                            "id": str(uuid.uuid4()),
                            "content": {
                                "token": "multi-client-secret",
                            },
                        },
                    }
                )
                login_resp = await ws.receive_json()
                assert login_resp["type"] == "LOGIN_SUCCESS"

                # 2. Send SUGGESTION_CREATED concurrently
                req_id = str(uuid.uuid4())
                await ws.send_json(
                    {
                        "type": "SUGGESTION_CREATED",
                        "data": {
                            "id": req_id,
                            "content": {
                                "author_id": str(1000 + client_id),
                                "author_username": f"User_{client_id}",
                                "suggestion": f"Suggestion from client {client_id}",
                            },
                        },
                    }
                )

                first_resp = await ws.receive_json()
                if first_resp["type"] == "SUGGESTION_QUEUED":
                    # Expect confirmation
                    second_resp = await ws.receive_json()
                    assert second_resp["type"] == "SUGGESTION_CONFIRMED"
                    async with lock:
                        results.append(
                            {
                                "client_id": client_id,
                                "status": "CONFIRMED",
                                "id": req_id,
                            }
                        )
                elif first_resp["type"] == "ERROR":
                    assert first_resp["data"]["code"] == "RATE_LIMITED"
                    async with lock:
                        results.append(
                            {
                                "client_id": client_id,
                                "status": "RATE_LIMITED",
                                "id": req_id,
                            }
                        )
                else:
                    pytest.fail(f"Unexpected response: {first_resp}")

    try:
        # Run all 10 clients concurrently
        await asyncio.gather(*(client_worker(i) for i in range(client_count)))

        confirmed_count = sum(1 for r in results if r["status"] == "CONFIRMED")
        rate_limited_count = sum(1 for r in results if r["status"] == "RATE_LIMITED")

        assert len(results) == client_count
        assert confirmed_count == 6, f"Expected 6 confirmed, got {confirmed_count}"
        assert rate_limited_count == 4, f"Expected 4 rate-limited, got {rate_limited_count}"
        assert mock_suggestion_service.post_suggestion.await_count == 6
    finally:
        await service.stop()


# ===========================================================================
# Vector 6: Single-Socket Pipelining & High-Frequency Stream
# ===========================================================================


@pytest.mark.asyncio
async def test_challenge_single_socket_pipelining(mock_bot, mock_suggestion_service):
    """Client sends 5 SUGGESTION_CREATED frames back-to-back without waiting for responses.

    Server must properly queue and confirm all 5 requests with exact request IDs.
    """
    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="pipeline-secret",
        bridge_rate_limit_per_minute=20,
    )
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
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
                            "id": str(uuid.uuid4()),
                            "content": {
                                "token": "pipeline-secret",
                            },
                        },
                    }
                )
                auth_resp = await ws.receive_json()
                assert auth_resp["type"] == "LOGIN_SUCCESS"

                # Send 5 requests back-to-back
                req_ids = [str(uuid.uuid4()) for _ in range(5)]
                for rid in req_ids:
                    await ws.send_json(
                        {
                            "type": "SUGGESTION_CREATED",
                            "data": {
                                "id": rid,
                                "content": {
                                    "author_id": "777",
                                    "author_username": "Pipeliner",
                                    "suggestion": f"Pipelined suggestion {rid}",
                                },
                            },
                        }
                    )

                # Expect 10 responses (5 QUEUED + 5 CONFIRMED)
                queued_ids = set()
                confirmed_ids = set()

                for _ in range(10):
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                    msg_type = msg["type"]
                    msg_id = msg["data"]["id"]
                    if msg_type == "SUGGESTION_QUEUED":
                        queued_ids.add(msg_id)
                    elif msg_type == "SUGGESTION_CONFIRMED":
                        confirmed_ids.add(msg_id)
                    else:
                        pytest.fail(f"Unexpected message type in pipeline: {msg_type}")

                assert queued_ids == set(req_ids)
                assert confirmed_ids == set(req_ids)
                assert mock_suggestion_service.post_suggestion.await_count == 5
    finally:
        await service.stop()


# ===========================================================================
# Vector 7: Graceful Server Shutdown with In-Flight Hanging Task
# ===========================================================================


@pytest.mark.asyncio
async def test_challenge_stop_drains_and_cancels_hanging_tasks(mock_bot, mock_suggestion_service):
    """When stop() is called while a delivery task is hanging/sleeping longer than timeout,

    the service must cleanly cancel the task and shut down within reasonable time
    without deadlocking.
    """
    hanging_event = asyncio.Event()

    async def infinitely_hanging_post(**kwargs):
        hanging_event.set()
        await asyncio.sleep(60.0)  # Hangs indefinitely
        return (1, 1)

    mock_suggestion_service.post_suggestion.side_effect = infinitely_hanging_post

    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="stop-secret",
    )
    service = WebsocketBridgeService(
        bot=mock_bot,
        settings=settings,
        suggestion_service=mock_suggestion_service,
    )
    await service.start()

    ws_url = f"http://127.0.0.1:{service.port}/ws/bridge"
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(ws_url)

    await ws.send_json(
        {
            "type": "LOG IN",
            "data": {
                "id": str(uuid.uuid4()),
                "content": {
                    "token": "stop-secret",
                },
            },
        }
    )
    await ws.receive_json()

    await ws.send_json(
        {
            "type": "SUGGESTION_CREATED",
            "data": {
                "id": str(uuid.uuid4()),
                "content": {
                    "suggestion": "Hang",
                },
            },
        }
    )
    await ws.receive_json()  # SUGGESTION_QUEUED

    # Wait until hanging task actually started
    await asyncio.wait_for(hanging_event.wait(), timeout=1.0)
    assert len(service._background_tasks) == 1

    # Now stop the service — it should wait up to 2.0s, cancel pending task, and exit cleanly
    start_time = asyncio.get_running_loop().time()
    await service.stop()
    duration = asyncio.get_running_loop().time() - start_time

    assert duration >= 1.9, f"Stop should have waited ~2s for draining, elapsed: {duration}"
    assert duration < 3.5, f"Stop should have finished within 3.5s, elapsed: {duration}"
    assert len(service._background_tasks) == 0
    assert service.is_running is False

    await ws.close()
    await session.close()


# ===========================================================================
# Vector 8: Health endpoint when Bot is NOT ready
# ===========================================================================


@pytest.mark.asyncio
async def test_challenge_health_endpoint_when_bot_not_ready():
    """Verify health endpoint accurately reflects bot_ready = False."""
    bot = MagicMock()
    bot.is_ready.return_value = False

    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="health-secret",
    )
    service = WebsocketBridgeService(bot=bot, settings=settings)
    await service.start()

    try:
        url = f"http://127.0.0.1:{service.port}/health"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
                assert data["bot_ready"] is False
    finally:
        await service.stop()
