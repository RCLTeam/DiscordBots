# Arquitectura del Runtime y Ciclo de Vida

[⬅️ Volver a Arquitectura](./README.md)

Este documento detalla la arquitectura de ejecución, el ciclo de vida, la inyección de dependencias y el sistema de manejo de procesos y señales del bot principal (`LigaBot`), implementado en `src/liga_bot/bot.py` y `src/liga_bot/__main__.py`.

---

## 1. Visión General del Runtime

El runtime de `LigaBot` está estructurado sobre la subclase `LigaBot(commands.Bot)` de `discord.py`, desacoplando completamente la lógica de negocio, la persistencia relacional, la pasarela WebSocket y los controladores de comandos (Cogs).

```
                      +-----------------------------+
                      |     src/liga_bot/__main__.py |
                      |  - setup_logging()          |
                      |  - handle_signal() (SIGINT) |
                      |  - run_bot() / main()       |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |      LigaBot(commands.Bot)  |
                      |  src/liga_bot/bot.py        |
                      +--------------+--------------+
                                     |
         +---------------------------+---------------------------+
         |                           |                           |
         v                           v                           v
+------------------+       +------------------+       +-------------------+
|  setup_hook()    |       |   DI Container   |       |      close()      |
| 1. DB Engine     |       | - 6 Servicios    |       | 1. Bridge Stop    |
| 2. DI Services   |       | - AsyncEngine    |       | 2. Cogs Loops     |
| 3. Cogs Load     |       | - SessionFactory |       | 3. Close Engine   |
| 4. WS Bridge     |       +------------------+       | 4. super().close()|
+------------------+                                  +-------------------+
```

---

## 2. Inicialización y Contenedor de Inyección de Dependencias (`LigaBot`)

La clase `LigaBot` se define en `src/liga_bot/bot.py:43-244`. Su constructor garantiza invariantes estrictos antes de iniciar cualquier conexión de red:

### 2.1 Intents Privilegiados Obligatorios

En `src/liga_bot/bot.py:64-69`, el bot fuerza la activación de intents privilegiados necesarios para la operativa de la liga:

```python
if intents is None:
    intents = discord.Intents.default()
intents.members = True  # Sincronización de roles, miembros de equipos y caché
intents.message_content = True  # Lectura de comandos con prefijo y mensajes administrativos
```

Si el invocador no suministra intents, se toma `discord.Intents.default()` y se activan explícitamente ambas banderas.

### 2.2 Extensiones Predeterminadas

En `src/liga_bot/bot.py:33-40`, se define la constante inmutable de extensiones que cargará el bot en ausencia de una lista personalizada:

```python
DEFAULT_EXTENSIONS: Final[tuple[str, ...]] = (
    "liga_bot.cogs.admin",
    "liga_bot.cogs.roles",
    "liga_bot.cogs.roster",
    "liga_bot.cogs.schedule",
    "liga_bot.cogs.teams",
    "liga_bot.cogs.tickets",
)
```

El constructor asigna `self.extensions_to_load = tuple(extensions) if extensions is not None else DEFAULT_EXTENSIONS` (`src/liga_bot/bot.py:77-79`), permitiendo a las suites de pruebas aislar Cogs específicos sin cargar la suite completa.

### 2.3 Contenedor de Inyección de Dependencias (DI)

La instancia de `LigaBot` actúa como el contenedor central de servicios para todos los Cogs del sistema (`src/liga_bot/bot.py:81-90`). Mantiene exactamente 6 servicios de dominio y la infraestructura de datos:

| Atributo | Tipo | Inicialización | Propósito en el Sistema |
|---|---|---|---|
| `self.engine` | `AsyncEngine \| None` | `setup_hook` | Motor asíncrono SQLAlchemy (PostgreSQL o PGlite). |
| `self.session_factory` | `async_sessionmaker[AsyncSession] \| None` | `setup_hook` | Factoría de sesiones asíncronas de base de datos. |
| `self.schedule_service` | `ScheduleService \| None` | `setup_hook` | Orquestación de jornadas, partidos y calendarios. |
| `self.ticket_service` | `TicketService \| None` | `setup_hook` | Gestión de tickets, canales de soporte y revisiones. |
| `self.role_service` | `RoleService \| None` | `setup_hook` | Solicitudes y asignaciones de roles Discord. |
| `self.roster_sync_service` | `RosterSyncService \| None` | `setup_hook` | Sincronización de plantillas y movimientos de jugadores. |
| `self.suggestion_service` | `SuggestionService \| None` | Constructor / `setup_hook` | Procesamiento y publicación de sugerencias en canal dedicado. |
| `self.websocket_bridge_service` | `WebsocketBridgeService \| None` | Constructor / `setup_hook` | Pasarela WebSocket bidireccional para integraciones externas. |

---

## 3. Fase de Inicialización Asíncrona (`setup_hook`)

El método `setup_hook()` (`src/liga_bot/bot.py:91-175`) es invocado automáticamente por `discord.py` tras autenticarse pero antes de abrir el Gateway. Se ejecuta en 4 fases secuenciales estrictas:

### Fase 1: Infraestructura de Base de Datos (`src/liga_bot/bot.py:105-110`)
Verifica si `self.engine` es `None`. Si no ha sido pre-inyectado (típico en producción), inicializa el motor mediante `await get_engine(self.settings)` y crea la factoría con `self.session_factory = get_session_factory(self.engine)`.

### Fase 2: Instanciación de Servicios de Dominio (`src/liga_bot/bot.py:113-153`)
Inicializa de forma perezosa (*lazy*) cada uno de los 6 servicios de dominio, preservando cualquier instancia inyectada previamente (estrategia utilizada en pruebas de integración):
- `ScheduleService(session_factory=self.session_factory, settings=self.settings, bot=self)`
- `TicketService(session_factory=self.session_factory, settings=self.settings, bot=self)`
- `RoleService(session_factory=self.session_factory, settings=self.settings, bot=self)`
- `RosterSyncService(session_factory=self.session_factory, settings=self.settings, bot=self)`
- `SuggestionService(bot=self, settings=self.settings)`
- `WebsocketBridgeService(bot=self, settings=self.settings, suggestion_service=self.suggestion_service)`

### Fase 3: Carga Asíncrona de Extensiones (`src/liga_bot/bot.py:155-162`)
Itera sobre la tupla `self.extensions_to_load` ejecutando `await self.load_extension(extension)`.
- **Estrategia Fail-Fast:** Si cualquier Cog o módulo falla durante su carga (por error de sintaxis, importación o configuración), se registra la traza de error con `logger.error(..., exc_info=True)` y se re-lanza la excepción (`raise`). Esto impide que el bot entre en línea en un estado parcialmente inicializado o inconsistente.

### Fase 4: Servidor WebSocket Bridge (`src/liga_bot/bot.py:164-173`)
Si `self.settings.bridge_enabled` es `True` y el servicio está presente, ejecuta `await self.websocket_bridge_service.start()`, enlazando el servidor WebSocket en la dirección `ws://{bridge_host}:{bridge_port}/ws/bridge`. Si está deshabilitado por configuración, emite un registro informativo y continúa.

---

## 4. Secuencia de Cierre Ordenado e Idempotente (`close`)

El método `close()` (`src/liga_bot/bot.py:176-227`) implementa un protocolo de parada atómico de 4 fases para evitar pérdidas de transacciones, procesos colgados o fugas de descriptores:

```
[1. WebSocket Bridge] -> Detiene servidor y anula referencia
        |
        v
[2. Cog Loops]        -> Invoca stop_loops() y cancela tasks.Loop activos
        |
        v
[3. DB Engine]        -> close_engine(self.engine) y anula factorías
        |
        v
[4. Discord Gateway]  -> await super().close()
```

### Detalle de las Fases de Cierre:

1. **Parada Atómica del WebSocket Bridge (`src/liga_bot/bot.py:188-196`):**
   Si `self.websocket_bridge_service is not None`, captura la referencia local:
   ```python
   bridge = self.websocket_bridge_service
   self.websocket_bridge_service = None
   try:
       await bridge.stop()
   except Exception as exc:
       logger.warning("Error deteniendo WebSocket Bridge: %s", exc)
   ```
   Anular el puntero antes de invocar `stop()` previene condiciones de carrera si concurren llamadas simultáneas a `close()`.

2. **Cancelación Defensiva de Bucles en Cogs (`src/liga_bot/bot.py:198-215`):**
   Itera sobre `list(self.cogs.items())`:
   - Si el Cog define un método `stop_loops()` invocable, lo ejecuta dentro de un bloque `try...except`.
   - Inspecciona dinámicamente los atributos del Cog mediante `dir(cog)` y `getattr(cog, attr_name, None)`. Si el atributo es una instancia de `tasks.Loop` y `attr.is_running()`, invoca `attr.cancel()`.
   - **Neutralización de Propiedades Hostiles:** La lectura con `getattr` está envuelta en un bloque `try...except Exception` para neutralizar propiedades calculadas dinámicas o mocks maliciosos que disparen `AttributeError`.

3. **Liberación del Motor de Base de Datos (`src/liga_bot/bot.py:217-222`):**
   Si `self.engine is not None`, invoca `await close_engine(self.engine)` y restablece `self.engine = None` y `self.session_factory = None`. Esto garantiza el apagado del subproceso Node.js (en PGlite) o la liquidación del pool de conexiones en PostgreSQL.

4. **Cierre de Conexiones de Discord (`src/liga_bot/bot.py:224-226`):**
   Invoca `await super().close()`, cerrando las sesiones WebSocket y HTTP de `aiohttp` subyacentes de Discord.

### Garantía de Idempotencia
El método `close()` puede invocarse múltiples veces de forma consecutiva o concurrente (`test_adversarial_double_close_sequential`, `test_adversarial_double_close_concurrent`). Las llamadas subsecuentes detectan los punteros nulos (`None`) y finalizan de manera segura sin volver a ejecutar desasignaciones ni disparar excepciones.

---

## 5. Desacoplamiento del Evento `on_ready`

Implementado en `src/liga_bot/bot.py:228-244`:

```python
async def on_ready(self) -> None:
    user_str = str(self.user) if self.user else "Desconocido"
    user_id = self.user.id if self.user else 0
    logger.info(
        "LigaBot conectado exitosamente como %s (ID: %s). Servidores conectados: %d",
        user_str,
        user_id,
        len(self.guilds),
    )
```

### Justificación Arquitectónica: Prevención de HTTP 429
En muchas aplicaciones de Discord es común colocar `await self.tree.sync()` dentro de `on_ready()`. En `LigaBot`, **`tree.sync()` está deliberadamente omitido en `on_ready()`**:
- `on_ready()` se dispara cada vez que el Gateway de Discord restablece la sesión tras una caída de red o reconexión (*reconnect storm*).
- Sincronizar el árbol de comandos slash globalmente consume cuotas estrictas de la API REST de Discord.
- Múltiples reconexiones sucesivas provocan un bloqueo inmediato por límite de tasa (HTTP 429).
- **Mecanismo de sincronización:** La sincronización de comandos se ejecuta de forma manual, explícita y controlada mediante el comando administrativo `/sync` (`src/liga_bot/cogs/admin.py`).

---

## 6. Proceso Principal y Manejo de Señales (`__main__.py`)

El archivo `src/liga_bot/__main__.py` contiene el bootstrap de ejecución y el control del proceso ante el sistema operativo:

### 6.1 Configuración de Logging (`setup_logging`, líneas 27-39)
- Resuelve el nivel numérico de log mediante `getattr(logging, settings.log_level, logging.INFO)`.
- Establece el formato canónico: `%(asctime)s [%(levelname)s] %(name)s: %(message)s` con fechado `%Y-%m-%d %H:%M:%S`.
- **Atenuación de ruido en librerías externas:** Si el nivel no es `DEBUG` profundo (`numeric_level > logging.DEBUG`), fija automáticamente los loggers `"discord"` y `"discord.http"` en `logging.WARNING`, evitando que los heartbeats continuos del Gateway saturen los archivos de registro.

### 6.2 Validación de Token (`run_bot`, líneas 51-57)
Evalúa `if not token or not token.strip():`. Si el token no está configurado o contiene exclusivamente espacios en blanco:
- Emite un mensaje con nivel `CRITICAL` alertando sobre la ausencia de `DISCORD_TOKEN`.
- Retorna el código de salida `1` inmediatamente, sin instanciar la conexión ni consumir ciclos del bucle de eventos.

### 6.3 Trampa de Doble Señal OS (`handle_signal`, líneas 60-79)
Intercepta `signal.SIGINT` (Ctrl+C) y `signal.SIGTERM` (detención por systemd, Docker o Kubernetes):

```python
shutdown_initiated = False


def handle_signal(sig: signal.Signals) -> None:
    nonlocal shutdown_initiated
    if shutdown_initiated:
        logger.warning("Señal %s recibida de nuevo. Forzando salida...", sig.name)
        return
    shutdown_initiated = True
    logger.info("Señal %s recibida. Iniciando parada ordenada...", sig.name)
    asyncio.create_task(bot.close())
```

- **Prevención de re-entrada:** Si el usuario pulsa repetidamente `Ctrl+C` durante el cierre, la bandera `shutdown_initiated` evita programar múltiples tareas de cierre concurrentes.
- **Compatibilidad de bucles:** El registro se realiza mediante `loop.add_signal_handler(sig, functools.partial(handle_signal, sig))` envuelto en un bloque que captura `(NotImplementedError, RuntimeError)`. Esto permite que el bot se ejecute sin excepciones en bucles de eventos no POSIX (como `ProactorEventLoop` en Windows) o en hilos que no son el principal.

### 6.4 Bloque Finally y Códigos de Salida (`main`, líneas 80-103)
- `run_bot()` ejecuta `await bot.start(token)` y retorna `0` ante una finalización normal.
- Captura `KeyboardInterrupt` o `asyncio.CancelledError` retornando `0`.
- Captura cualquier `Exception` genérica imprevista, emitiendo log `CRITICAL` con traza completa (`exc_info=True`) y retornando `1`.
- El bloque `finally:` asegura que `if not bot.is_closed(): await bot.close()`.
- La función síncrona `main()` envuelve `asyncio.run(run_bot())` y finaliza el proceso con `sys.exit(exit_code)`.
