# Limitador de Tasa por Ventana Deslizante (SlidingWindowRateLimiter)

[⬅️ Volver a WebSocket Bridge](./README.md)

Este documento detalla el diseño algorítmico, la seguridad ante concurrencia, las estructuras de datos y el protocolo de respuesta ante agotamiento de cuota implementados en `SlidingWindowRateLimiter` (`src/liga_bot/services/rate_limiter.py`).

---

## 1. Diseño y Estructura de Datos

`SlidingWindowRateLimiter` implementa un algoritmo de **ventana deslizante en memoria** (*in-memory sliding window*) que registra las marcas de tiempo exactas de cada petición aprobada por clave.

A diferencia de algoritmos de cubo con fichas (*token bucket*) o ventanas fijas (*fixed window*), la ventana deslizante previene de forma matemática los picos de ráfaga (*burst spikes*) en los límites de intervalo, garantizando que en ningún intervalo móvil continuo de longitud $W$ (por defecto 60 segundos) se superen $L$ operaciones.

```
 Ventana Temporal Deslizante (window_seconds = 60.0s)
 ┌───────────────────────────────────────────────────────────┐
 │                                                           │
 │   t_1           t_2           t_3           t_now         │
 │    ●             ●             ●              ●           │
 └────┼─────────────┼─────────────┼──────────────┼───────────┘
      │                                          │
   oldest                                     current
  cutoff = now - 60.0s
```

### Estructura Interna

```python
self._history: dict[str, deque[float]] = defaultdict(deque)
self._lock = asyncio.Lock()
```

- **Historial por Clave:** Un diccionario por defecto `defaultdict` que asocia cada identificador de cuota (`key`, ej. `"global"`) a una cola de doble extremo (`collections.deque[float]`).
- **Complejidad Espacial:** Ocupa $O(K \times L)$ en memoria, donde $K$ es el número de claves activas y $L$ es el límite de operaciones por ventana.
- **Reloj Monótono:** Utiliza exclusivamente `time.monotonic()`. Esto previene desajustes provocados por sincronizaciones de hora por NTP, cambios de huso horario o modificaciones manuales del reloj del sistema.

---

## 2. Parámetros de Inicialización y Acotación

El constructor admite parámetros flexibles con acotación de seguridad (`src/liga_bot/services/rate_limiter.py:26-41`):

```python
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
```

- **Alias `limit` / `capacity`:** Permite especificar el límite mediante el argumento posicional `limit` o la palabra clave `capacity`. Ambos atributos quedan sincronizados (`self.limit = self.capacity`).
- **Acotación inferior (*Clamping*):**
  - `limit` queda forzado a un valor mínimo entero de `1` (`max(1, int(...))`).
  - `window_seconds` queda forzado a un valor mínimo flotante de `0.1` segundos (`max(0.1, float(...))`).

---

## 3. Algoritmo de Adquisición y Exclusión Mutua (`acquire`)

El método `acquire` (`src/liga_bot/services/rate_limiter.py:42-65`) evalúa de forma atómica si una solicitud es admitida o rechazada:

```python
async def acquire(self, key: str = "global") -> tuple[bool, float]:
    async with self._lock:
        now = time.monotonic()
        timestamps = self._history[key]

        # 1. Purgar marcas de tiempo fuera de la ventana deslizante
        cutoff = now - self.window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        # 2. Comprobar disponibilidad de cuota
        if len(timestamps) < self.limit:
            timestamps.append(now)
            return True, 0.0

        # 3. Calcular tiempo restante de espera para la marca más antigua
        oldest = timestamps[0]
        retry_after = max(0.0, (oldest + self.window_seconds) - now)
        return False, retry_after
```

### Fases de Ejecución

1. **Exclusión mutua con `asyncio.Lock`:** La sección crítica completa está protegida por un cerrojo asíncrono, garantizando que ráfagas de corrutinas concurrentes no sufran condiciones de carrera al consultar la longitud o agregar elementos a la cola.
2. **Purga en cabeza (*Head-Purging Deque*):**
   - Se calcula el corte temporal: $\text{cutoff} = \text{now} - \text{window\_seconds}$.
   - Dado que las marcas de tiempo se insertan en orden cronológico monótono estricto, la cola se mantiene siempre ordenada de menor a mayor antigüedad.
   - La purga se ejecuta con `timestamps.popleft()` mientras el elemento más antiguo esté fuera de la ventana. La complejidad temporal de la purga es $O(k)$ donde $k \le \text{limit}$.
3. **Admisión:** Si `len(timestamps) < self.limit`, se registra la marca actual con `timestamps.append(now)` y se devuelve `(True, 0.0)`.
4. **Rechazo y cálculo de `retry_after`:** Si se alcanza o supera el límite:
   - Se inspecciona la marca más antigua $\text{oldest} = \text{timestamps}[0]$.
   - El tiempo exacto para que se libere el siguiente cupo es:
     $$\text{retry\_after} = \max(0.0, (\text{oldest} + \text{window\_seconds}) - \text{now})$$
   - Se retorna `(False, retry_after)`.

---

## 4. Respuesta ante Límite de Tasa Excedido

Cuando `acquire("global")` devuelve `(False, retry_after)` en `WebsocketBridgeService._process_suggestion` (`src/liga_bot/services/websocket_bridge_service.py:276-292`), la solicitud entrante es rechazada de inmediato y el servidor emite una trama `ERROR`:

```json
{
  "type": "ERROR",
  "data": {
    "id": "a1b2c3d4-e5f6-4a1b-8c2d-3e4f5a6b7c8d",
    "code": "RATE_LIMITED",
    "retry_after_seconds": 15.3
  }
}
```

- **Invariante de Correlación:** El campo `id` reproduce exactamente el UUID de la petición rechazada, permitiendo al cliente correlacionar qué sugerencia ha sufrido el bloqueo.
- **Redondeo:** `retry_after_seconds` se formatea con `round(retry_after, 1)`, ofreciendo una cifra clara y amigable para que el cliente programe su reintento o desactive temporalmente el botón de envío en la interfaz web.

---

## 5. Reinicio Híbrido Síncrono / Asíncrono (`_AwaitableNone`)

Para facilitar el reinicio de cuotas tanto en pruebas unitarias síncronas como en pipelines asíncronos sin generar advertencias de runtime (`RuntimeWarning: coroutine was never awaited`), `reset()` utiliza la clase auxiliar `_AwaitableNone` (`src/liga_bot/services/rate_limiter.py:10-16, 66-76`):

```python
class _AwaitableNone:
    def __await__(self):
        if False:
            yield
        return None


def reset(self, key: str | None = None) -> _AwaitableNone:
    if key is None:
        self._history.clear()
    elif key in self._history:
        self._history[key].clear()
    return _AwaitableNone()
```

### Modos de Invocación Admitidos

```python
# Modo síncrono (típico en setup/teardown de tests estándar)
limiter.reset()

# Modo asíncrono (típico en corrutinas de reinicio de ciclo de vida)
await limiter.reset()

# Reinicio selectivo por clave
limiter.reset("global")
```

---

## 6. Alcance y Clave de Aislamiento

- **Clave por Defecto:** En `WebsocketBridgeService`, el método invoca `await self.rate_limiter.acquire("global")`.
- **Ámbito Global:** Todas las conexiones WebSocket conectadas al puente comparten el mismo cupo global de peticiones por minuto configurado en `bridge_rate_limit_per_minute` / `BRIDGE_RATE_LIMIT_PER_MINUTE` (por defecto `10` peticiones por minuto).
- **Aislamiento por Clave:** El limitador soporta claves arbitrarias (por ejemplo identificadores de usuario o direcciones IP) si en el futuro se desea segmentar la tasa por remitente.
