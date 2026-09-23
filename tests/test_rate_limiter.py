"""Pruebas unitarias para SlidingWindowRateLimiter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from liga_bot.services.rate_limiter import SlidingWindowRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_limit():
    """Verifica que las peticiones dentro del límite de capacidad sean permitidas."""
    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60.0)
    for _ in range(3):
        allowed, retry_after = await limiter.acquire()
        assert allowed is True
        assert retry_after == 0.0


@pytest.mark.asyncio
async def test_rate_limiter_blocks_over_limit():
    """Verifica que al superar el límite se bloquee la petición e indique retry_after > 0."""
    limiter = SlidingWindowRateLimiter(capacity=2, window_seconds=60.0)
    await limiter.acquire()
    await limiter.acquire()

    allowed, retry_after = await limiter.acquire()
    assert allowed is False
    assert retry_after > 0.0
    assert retry_after <= 60.0


@pytest.mark.asyncio
async def test_rate_limiter_constructor_aliases_and_validation():
    """Valida soporte de alias limit/capacity y acotación de parámetros mínimos."""
    l1 = SlidingWindowRateLimiter(limit=5, window_seconds=30.0)
    assert l1.limit == 5
    assert l1.capacity == 5
    assert l1.window_seconds == 30.0

    l2 = SlidingWindowRateLimiter(capacity=8, window_seconds=15.0)
    assert l2.limit == 8
    assert l2.capacity == 8

    # Clamping de valores menores a 1 o negativos
    l3 = SlidingWindowRateLimiter(limit=0, window_seconds=-10.0)
    assert l3.limit == 1
    assert l3.window_seconds == 0.1

    with pytest.raises(TypeError, match="requiere 'limit' o 'capacity'"):
        SlidingWindowRateLimiter()


@pytest.mark.asyncio
async def test_rate_limiter_reset_sync_and_async():
    """Verifica reset síncrono y asíncrono tanto global como específico por clave."""
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    await limiter.acquire()
    assert (await limiter.acquire())[0] is False

    # Reset síncrono
    limiter.reset()
    assert (await limiter.acquire())[0] is True
    assert (await limiter.acquire())[0] is False

    # Reset con await
    await limiter.reset()
    assert (await limiter.acquire())[0] is True


@pytest.mark.asyncio
async def test_rate_limiter_key_isolation():
    """Verifica que distintas claves tengan historiales y cuotas independientes."""
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    assert (await limiter.acquire("user_1"))[0] is True
    assert (await limiter.acquire("user_1"))[0] is False

    assert (await limiter.acquire("user_2"))[0] is True
    assert (await limiter.acquire("user_2"))[0] is False

    # Resetear solo user_1 no afecta a user_2
    limiter.reset("user_1")
    assert (await limiter.acquire("user_1"))[0] is True
    assert (await limiter.acquire("user_2"))[0] is False


@pytest.mark.asyncio
async def test_rate_limiter_sliding_window_eviction():
    """Verifica el desalojo dinámico y gradual según transcurre el tiempo en la ventana."""
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10.0)
    current_time = 1000.0

    with patch("time.monotonic", side_effect=lambda: current_time):
        # t=1000.0: Petición 1 aprobada
        r1, wait1 = await limiter.acquire()
        assert r1 is True
        assert wait1 == 0.0

        # t=1004.0: Petición 2 aprobada
        current_time = 1004.0
        r2, _ = await limiter.acquire()
        assert r2 is True

        # t=1006.0: Petición 3 bloqueada (expira en 1010.0, retry_after=4.0s)
        current_time = 1006.0
        r3, wait3 = await limiter.acquire()
        assert r3 is False
        assert abs(wait3 - 4.0) < 1e-4

        # t=1010.0: Petición 1 expira en el borde exacto; petición 4 aprobada
        current_time = 1010.0
        r4, _ = await limiter.acquire()
        assert r4 is True

        # t=1011.0: Petición 5 bloqueada (más antigua 1004.0, expira 1014.0 -> retry_after=3.0s)
        current_time = 1011.0
        r5, wait5 = await limiter.acquire()
        assert r5 is False
        assert abs(wait5 - 3.0) < 1e-4

        # t=1014.0: Petición 2 expira; petición 6 aprobada
        current_time = 1014.0
        r6, _ = await limiter.acquire()
        assert r6 is True


@pytest.mark.asyncio
async def test_rate_limiter_concurrency_race():
    """Verifica seguridad frente a carreras de corrutinas concurrentes masivas."""
    limiter = SlidingWindowRateLimiter(limit=10, window_seconds=60.0)
    # 50 corrutinas concurrentes intentando adquirir cuota simultáneamente
    results = await asyncio.gather(*(limiter.acquire() for _ in range(50)))

    allowed_count = sum(1 for allowed, _ in results if allowed)
    blocked_count = sum(1 for allowed, _ in results if not allowed)

    assert allowed_count == 10
    assert blocked_count == 40
