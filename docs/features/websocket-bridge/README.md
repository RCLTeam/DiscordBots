[⬅️ Volver a Base de Datos](../database/README.md) | [Siguiente: Plantillas ➡️](../roster/README.md)

---

# WebSocket Bridge & Protocolo de Integración

Este directorio documenta la arquitectura técnica, el protocolo de comunicación, los mecanismos de seguridad perimetral y el control de concurrencia del puente WebSocket (**WebSocket Bridge**) de **LigaBot**.

---

## Resumen Ejecutivo

El subsistema WebSocket Bridge actúa como pasarela asíncrona bidireccional entre servicios externos (tales como paneles de control web, aplicaciones administrativas o frontends de la liga RCL) y la instancia en ejecución del bot de Discord.

El componente central `WebsocketBridgeService` (`src/liga_bot/services/websocket_bridge_service.py`) gestiona un servidor HTTP/WebSocket sobre `aiohttp.web`, proporcionando:
- Ingesta de sugerencias comunitarias mediante un protocolo desacoplado en dos fases (*two-phase decoupled protocol*).
- Validación estricta y determinista de identificadores de solicitud conforme a RFC 4122 (`src/liga_bot/services/bridge_protocol.py`).
- Protección de seguridad con descarte silencioso pre-autenticación (*zero reconnaissance leakage*) y comparación criptográfica en tiempo constante.
- Limitador de tasa por ventana deslizante en memoria coroutine-safe (`src/liga_bot/services/rate_limiter.py`).

---

## Tabla de Contenidos

| Documento | Descripción Técnica |
|---|---|
| [**protocol.md**](protocol.md) | Arquitectura del servidor, endpoints `/ws/bridge` y `/health`, opcodes de cliente/servidor, decodificación de tramas y protocolo de sugerencias en 2 fases con tareas desacopladas. |
| [**security.md**](security.md) | Validación estricta RFC 4122, política de descarte silencioso pre-login, `secrets.compare_digest`, protección de supertoken vacío y códigos de cierre 4001 (timeout) y 1000 (shutdown). |
| [**rate-limiter.md**](rate-limiter.md) | Algoritmo `SlidingWindowRateLimiter`, cerrojo `asyncio.Lock`, marcas de tiempo monótonas, purga en deque, respuesta `RATE_LIMITED` y reinicio híbrido `_AwaitableNone`. |

---

## Variables de Configuración

El comportamiento del puente WebSocket se controla mediante las siguientes variables de entorno definidas en `Settings` (`src/liga_bot/config.py`):

| Variable de Entorno | Atributo en `Settings` | Tipo | Valor por Defecto | Descripción |
|---|---|---|---|---|
| `BRIDGE_ENABLED` | `bridge_enabled` | `bool` | `True` | Habilita o deshabilita el servidor WebSocket interno. |
| `BRIDGE_HOST` | `bridge_host` | `str` | `"127.0.0.1"` | Dirección IP o interfaz de red de escucha. |
| `BRIDGE_PORT` | `bridge_port` | `int` | `8765` | Puerto TCP de escucha (admite `0` para asignación de puerto efímero). |
| `DISCORD_BOT_SUPERTOKEN` | `discord_bot_supertoken` | `str` | `""` | Secreto maestro para autenticar sesiones WebSocket entrantes. |
| `SUGGESTIONS_CHANNEL_ID` | `suggestions_channel_id` | `int` | `0` | Identificador del canal de Discord donde se publican las sugerencias. |
| `SUGGESTIONS_RATE_LIMIT_PER_MINUTE` | `suggestions_rate_limit_per_minute` | `int` | `10` | Capacidad máxima de sugerencias admitidas por minuto por el limitador. |

---

## Verificación de Pruebas

La suite de pruebas automatizadas del WebSocket Bridge y sus componentes asociados se ejecuta mediante:

```bash
# Pruebas primarias de protocolo, servicio y resiliencia de ciclo de vida
uv run pytest tests/test_bridge_protocol.py tests/test_websocket_bridge_service.py tests/test_bot_bridge_lifecycle_resilience.py

# Suite extendida que incluye limitador de tasa y concurrencia
uv run pytest tests/test_bridge_protocol.py tests/test_websocket_bridge_service.py tests/test_bot_bridge_lifecycle_resilience.py tests/test_rate_limiter.py tests/test_websocket_bridge_concurrency.py tests/test_bot_bridge_lifecycle.py tests/test_bridge_config.py
```
