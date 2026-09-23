"""Limitador de tasa asíncrono basado en ventana deslizante en memoria."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class _AwaitableNone:
    """Objeto auxiliar que permite que reset() funcione de forma síncrona o con await."""

    def __await__(self):
        if False:
            yield
        return None


class SlidingWindowRateLimiter:
    """Limitador de tasa coroutine-safe basado en ventana deslizante.

    Almacena las marcas de tiempo de cada llamada por clave y descarta
    automáticamente las que caen fuera de la ventana temporal.
    """

    def __init__(
        self,
        limit: int | None = None,
        window_seconds: float = 60.0,
        *,
        capacity: int | None = None,
    ) -> None:
        effective_limit = limit if limit is not None else capacity
        if effective_limit is None:
            raise TypeError("SlidingWindowRateLimiter requiere 'limit' o 'capacity'.")
        self.limit = max(1, int(effective_limit))
        self.capacity = self.limit
        self.window_seconds = max(0.1, float(window_seconds))
        self._history: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str = "global") -> tuple[bool, float]:
        """Intenta adquirir cuota para la clave especificada.

        Devuelve una tupla (permitido, segundos_para_reintentar).
        - Si está permitido: (True, 0.0)
        - Si está bloqueado: (False, retry_after_seconds > 0.0)
        """
        async with self._lock:
            now = time.monotonic()
            timestamps = self._history[key]

            # Purgar marcas de tiempo fuera de la ventana deslizante
            cutoff = now - self.window_seconds
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) < self.limit:
                timestamps.append(now)
                return True, 0.0

            oldest = timestamps[0]
            retry_after = max(0.0, (oldest + self.window_seconds) - now)
            return False, retry_after

    def reset(self, key: str | None = None) -> _AwaitableNone:
        """Limpia el historial de cuotas en memoria.

        Si se especifica key, reinicia solo esa clave; si es None, limpia todas las claves.
        Puede invocarse de forma síncrona o asíncrona (awaitable).
        """
        if key is None:
            self._history.clear()
        elif key in self._history:
            self._history[key].clear()
        return _AwaitableNone()
