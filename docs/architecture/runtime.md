# Arquitectura del Runtime y Ciclo de Vida

[⬅️ Volver a Arquitectura](./README.md)

Este documento detalla la arquitectura de ejecución, el ciclo de vida, la inyección de dependencias y el sistema de manejo de procesos y señales del bot principal (`LigaBot`), implementado en `src/liga_bot/bot.py` y `src/liga_bot/__main__.py`, y el manejador global de errores de los slash commands (`src/liga_bot/command_tree.py`).

---

## 1. Visión General del Runtime

El proceso principal de ejecución está registrado canónicamente como `liga-bot` en `pyproject.toml` (`[project.scripts] liga-bot = "liga_bot.__main__:main"`), invocable mediante `uv run liga-bot` o alternativamente `python -m liga_bot`.

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
+------------------+       +------------------+       +---------------------+
|  setup_hook()    |       |   DI Container   |       |       close()       |
| 1. DB Engine     |       | - 6 Servicios    |       | 1. Bridge Stop      |
| 2. DI Services   |       | - AsyncEngine    |       | 2. Cogs Loops       |
| 3. Cogs Load     |       | - SessionFactory |       | 3. Drain Tasks      |
| 4. WS Bridge     |       +------------------+       | 4. super().close()  |
+------------------+                                  | 5. Cancel Rezagadas |
                                                      | 6. Close Engine     |
                                                      +---------------------+
```

---

## 2. Inicialización y Contenedor de Inyección de Dependencias (`LigaBot`)

La clase `LigaBot` se define en `src/liga_bot/bot.py:43-243`. Su constructor garantiza invariantes estrictos antes de iniciar cualquier conexión de red:

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

El método `setup_hook()` (`src/liga_bot/bot.py:125-218`) es invocado automáticamente por `discord.py` tras autenticarse pero antes de abrir el Gateway. Se ejecuta en 4 fases secuenciales estrictas:

### Fase 1: Infraestructura de Base de Datos (`src/liga_bot/bot.py:138-147`)
Verifica si `self.engine` es `None`. Si no ha sido pre-inyectado (típico en producción), registra a nivel `INFO` el motor que se va a inicializar con la URL saneada por `describe_database_url` (motor, host, puerto y base de datos, nunca credenciales; ver [Motor de base de datos](database-engine.md)), inicializa el motor mediante `await get_engine(self.settings)` y crea la factoría con `self.session_factory = get_session_factory(self.engine)`.

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

El método `close()` (`src/liga_bot/bot.py:225-295`) implementa un protocolo de parada de 6 fases. El objetivo es que ningún manejador encuentre la base de datos cerrada: primero se deja de recibir trabajo (puente y Discord) y solo al final se cierra el motor de base de datos.

```
[1. WebSocket Bridge]  -> Detiene servidor y anula referencia
        |
        v
[2. Cog Loops]         -> Invoca stop_loops() y cancela tasks.Loop activos
        |
        v
[3. Tareas de fondo]   -> Espera hasta 6 s las tareas registradas y cancela las restantes
        |
        v
[4. Discord Gateway]   -> await super().close(): deja de recibir eventos
        |
        v
[5. Tareas tardías]    -> Cancela las tareas registradas durante el paso 4
        |
        v
[6. DB Engine]         -> close_engine(engine) tras anular engine y session_factory
```

### Detalle de las Fases de Cierre:

1. **Parada Atómica del WebSocket Bridge (`src/liga_bot/bot.py:241-249`):**
   Si `self.websocket_bridge_service is not None`, captura la referencia local:
   ```python
   bridge = self.websocket_bridge_service
   self.websocket_bridge_service = None
   try:
       await bridge.stop()
   except Exception as exc:
       logger.warning("Error deteniendo WebSocket Bridge: %s", exc)
   ```
   Anular el puntero antes de invocar `stop()` previene condiciones de carrera si concurren llamadas simultáneas a `close()`. `stop()` drena sus propias entregas pendientes (`_background_tasks`), que publican en Discord, por eso va antes del cierre de Discord.

2. **Cancelación Defensiva de Bucles en Cogs (`src/liga_bot/bot.py:251-269`):**
   Itera sobre `list(self.cogs.items())`:
   - Si el Cog define un método `stop_loops()` invocable, lo ejecuta dentro de un bloque `try...except`.
   - Inspecciona dinámicamente los atributos del Cog mediante `dir(cog)` y `getattr(cog, attr_name, None)`. Si el atributo es una instancia de `tasks.Loop` y `attr.is_running()`, invoca `attr.cancel()`.
   - **Neutralización de Propiedades Hostiles:** La lectura con `getattr` está envuelta en un bloque `try...except Exception` para neutralizar propiedades calculadas dinámicas o mocks maliciosos que disparen `AttributeError`.
   - Debe ejecutarse antes de `super().close()`, porque `commands.Bot.close()` descarga las extensiones y elimina los Cogs.

3. **Espera de Tareas en Segundo Plano (`src/liga_bot/bot.py:271-275`):**
   Invoca `background_tasks.drain(BACKGROUND_TASKS_DRAIN_TIMEOUT)` (6 s, `src/liga_bot/bot.py:52`): espera las tareas registradas (ver §4.1) y cancela las que no terminen en el plazo. El plazo cubre el borrado diferido de canales de ticket (5 s). Esta fase va **antes** de cerrar Discord porque esas tareas usan la sesión HTTP de Discord (`channel.delete()`), que `super().close()` cierra.

4. **Cierre de Conexiones de Discord (`src/liga_bot/bot.py:277-280`):**
   Invoca `await super().close()`, que descarga extensiones y Cogs y cierra las sesiones WebSocket y HTTP de Discord. A partir de aquí no llegan nuevos eventos ni interacciones. `bot.start()` retorna en este punto, aunque `close()` todavía no haya terminado (ver §6.4).

5. **Cancelación de Tareas Tardías (`src/liga_bot/bot.py:282-286`):**
   Dentro de un `finally`, invoca `background_tasks.cancel_all()` para cancelar las tareas registradas mientras se cerraba Discord (interacciones que llegaron durante la fase 3 o 4).

6. **Liberación del Motor de Base de Datos (`src/liga_bot/bot.py:288-294`):**
   Si `self.engine is not None`, anula `self.engine` y `self.session_factory` y después invoca `await close_engine(engine)`. Esto garantiza el apagado del subproceso Node.js (en PGlite) o la liquidación del pool de conexiones en PostgreSQL. Las fases 5 y 6 se ejecutan aunque `super().close()` lance una excepción.

### 4.1 Registro de Tareas en Segundo Plano (`src/liga_bot/background_tasks.py`)

El bucle de eventos solo guarda referencias débiles a las tareas de `asyncio.create_task`; una tarea sin otra referencia puede ser eliminada por el recolector de basura antes de terminar. El módulo `background_tasks` mantiene un conjunto con referencia fuerte:

| Función | Uso |
|---|---|
| `spawn(coro, name=None)` | Crea la tarea, la añade al conjunto y registra `add_done_callback(discard)` para retirarla al terminar. |
| `pending_tasks()` | Tareas registradas que aún no han terminado. |
| `drain(timeout)` | Espera hasta `timeout` segundos a las tareas del bucle actual y cancela las restantes (fase 3). |
| `cancel_all()` | Cancela las tareas pendientes del bucle actual y espera a que terminen (fase 5). |

Lo usan `ConfirmarRolButton._schedule_deletion` y `TicketView._schedule_deletion` (`src/liga_bot/ui/roles.py`) para el borrado del canal del ticket 5 segundos después de confirmar o denegar la solicitud. El puente WebSocket mantiene su propio conjunto `_background_tasks`, que drena en `stop()`.

### Garantía de Idempotencia
El método `close()` puede invocarse múltiples veces de forma consecutiva o concurrente (`test_adversarial_double_close_sequential`, `test_adversarial_double_close_concurrent`). Las llamadas subsecuentes detectan los punteros nulos (`None`) y finalizan de manera segura sin volver a ejecutar desasignaciones ni disparar excepciones.

---

## 5. Desacoplamiento del Evento `on_ready`

Implementado en `src/liga_bot/bot.py:297-312`:

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

## 6. Proceso Principal y Manejo de Señales (`__main__.py` / `liga-bot`)

El archivo `src/liga_bot/__main__.py` contiene el bootstrap de ejecución y el control del proceso ante el sistema operativo, expuesto a través del comando canónico de consola `liga-bot` (`uv run liga-bot`):

### 6.1 Configuración de Logging (`setup_logging`, líneas 27-39)
- Resuelve el nivel numérico de log mediante `getattr(logging, settings.log_level, logging.INFO)`.
- Establece el formato canónico: `%(asctime)s [%(levelname)s] %(name)s: %(message)s` con fechado `%Y-%m-%d %H:%M:%S`.
- **Atenuación de ruido en librerías externas:** Si el nivel no es `DEBUG` profundo (`numeric_level > logging.DEBUG`), fija automáticamente los loggers `"discord"` y `"discord.http"` en `logging.WARNING`, evitando que los heartbeats continuos del Gateway saturen los archivos de registro.

### 6.2 Validación de Token (`run_bot`, líneas 51-57)
Evalúa `if not token or not token.strip():`. Si el token no está configurado o contiene exclusivamente espacios en blanco:
- Emite un mensaje con nivel `CRITICAL` alertando sobre la ausencia de `DISCORD_TOKEN`.
- Retorna el código de salida `1` inmediatamente, sin instanciar la conexión ni consumir ciclos del bucle de eventos.

### 6.3 Manejo de Señales y Salida Forzada (`handle_signal`, líneas 61-83)
Intercepta `signal.SIGINT` (Ctrl+C) y `signal.SIGTERM` (detención por systemd, Docker o Kubernetes):

```python
main_task = asyncio.current_task()
close_task: asyncio.Task[None] | None = None
forced_exit = False


def handle_signal(sig: signal.Signals) -> None:
    nonlocal close_task, forced_exit
    if close_task is not None:
        logger.warning("Señal %s recibida de nuevo. Forzando salida...", sig.name)
        forced_exit = True
        if main_task is not None:
            main_task.cancel()
        return
    logger.info("Señal %s recibida. Iniciando parada ordenada...", sig.name)
    close_task = asyncio.create_task(bot.close())
```

- **Primera señal:** programa una única tarea `bot.close()` y guarda su referencia en `close_task` (el bucle solo guarda referencias débiles a las tareas).
- **Segunda señal:** si llega otra señal SIGINT o SIGTERM durante el apagado, cancela la tarea principal de `run_bot()`, que devuelve el código de salida `1` sin esperar a que termine el cierre. Sirve para salir cuando el apagado ordenado se queda bloqueado.
- **Compatibilidad de bucles:** El registro se realiza mediante `loop.add_signal_handler(sig, functools.partial(handle_signal, sig))` envuelto en un bloque que captura `(NotImplementedError, RuntimeError)`. Esto permite que el bot se ejecute sin excepciones en bucles de eventos no POSIX (como `ProactorEventLoop` en Windows) o en hilos que no son el principal.

### 6.4 Espera del Cierre y Códigos de Salida (`run_bot`, líneas 85-115; `main`, líneas 118-125)
- `run_bot()` ejecuta `await bot.start(token)`; ante una finalización normal el código de salida es `0`.
- Captura `KeyboardInterrupt` o `asyncio.CancelledError` con código `0`, salvo que la cancelación venga de una segunda señal.
- Captura cualquier `Exception` genérica imprevista, emitiendo log `CRITICAL` con traza completa (`exc_info=True`), con código `1`.
- **Espera del cierre:** como `close()` cierra Discord antes que la base de datos, `bot.start()` retorna mientras `close()` sigue en las fases 5 y 6. Si la parada la inició una señal, `run_bot()` espera siempre a `close_task` antes de retornar; si no, invoca `bot.close()` cuando `not bot.is_closed()`. Sin esa espera, `asyncio.run` cancelaría la tarea de cierre a medio cerrar el motor de base de datos. Un error en `close_task` se registra con nivel `CRITICAL` y devuelve `1`.
- **Salida forzada:** si una segunda señal cancela `run_bot()` (también mientras espera a `close_task`), registra un `ERROR` y devuelve `1`.
- La función síncrona `main()` envuelve `asyncio.run(run_bot())` y finaliza el proceso con `sys.exit(exit_code)`.

---

## 7. Manejador Global de Errores de los Slash Commands (`command_tree.py`)

`LigaBot.__init__` crea el árbol de comandos con la subclase `LigaCommandTree` (`kwargs.setdefault("tree_cls", LigaCommandTree)`, `src/liga_bot/bot.py:86`). Quien construya `LigaBot` con otro `tree_cls` lo sustituye. La subclase está en `src/liga_bot/command_tree.py:70-144` y solo redefine `on_error`.

discord.py llama a `CommandTree.on_error` cuando un slash command o menú contextual termina con una excepción que no ha capturado: la del callback llega envuelta en `app_commands.CommandInvokeError`, y los fallos de checks, de conversión de opciones o de búsqueda del comando llegan como su propia subclase de `AppCommandError`. Sin este manejador, discord.py solo registraba la traza y el usuario veía «La aplicación no ha respondido» o el indicador «está pensando…» sin respuesta si el comando ya había hecho `defer()`.

### 7.1 Qué no pasa por el manejador

- **Comandos que gestionan sus errores:** si el comando captura la excepción y responde (por ejemplo, el `try/except discord.HTTPException` de `/sync`), discord.py no llama a `on_error` y la respuesta no cambia.
- **Denegaciones de permisos actuales:** los comandos comprueban `has_staff_access` (o sus envoltorios `is_staff`, `is_authorized_scheduler`…) dentro del propio cuerpo, responden en efímero y hacen `return` sin lanzar nada (ver [permissions.md](./permissions.md)). El manejador no interviene.
- **Comandos con manejador propio:** si el comando tiene `@comando.error` o su Cog define `cog_app_command_error`, `on_error` sale sin responder ni registrar (`src/liga_bot/command_tree.py:80-82`), igual que el `on_error` por defecto de discord.py. Hoy ningún comando lo usa.
- **Botones, desplegables y modales:** se gestionan con el `on_error` de cada `discord.ui.View` (por ejemplo `GestionarPosicionView`, ver [roster/ui.md](../features/roster/ui.md)).
- **`!sync` (comando de prefijo):** usa el sistema de `commands.Bot`, no el árbol de slash commands.

### 7.2 Decisión por tipo de error

| Error recibido | Registro (logger `liga_bot.command_tree`) | Aviso al usuario (siempre efímero) |
|---|---|---|
| `CheckFailure` si la interacción ya tiene respuesta | `INFO`, sin traza | Ninguno: el check ya respondió. |
| `NoPrivateMessage` | `INFO`, sin traza | «❌ Este comando solo puede usarse dentro de un servidor.» |
| `BotMissingPermissions` | `INFO`, sin traza | «❌ Al bot le faltan permisos para ejecutar este comando: …» con la lista de permisos. |
| `CommandOnCooldown` | `INFO`, sin traza | «⏳ Has usado este comando hace muy poco. Vuelve a intentarlo en N s.» |
| Otro `CheckFailure` (`MissingPermissions`, `MissingRole`, check propio que devuelve `False`…) | `INFO`, sin traza | «❌ No tienes permisos para usar este comando.» |
| `TransformerError` (opción que no se puede convertir) | `INFO`, sin traza | «❌ El valor «…» no es válido para una de las opciones del comando.» El valor se recorta a 100 caracteres y se escapa el Markdown. |
| `CommandNotFound` / `CommandSignatureMismatch` | `WARNING`, sin traza | «❌ Este comando no coincide con la versión actual del bot. El staff debe sincronizar los comandos con /sync.» |
| Cualquier otro (`CommandInvokeError` con la excepción del callback incluida) | `ERROR` con la traza de la excepción original, el nombre del comando, el ID del usuario y el ID del servidor | «❌ Se ha producido un error inesperado al ejecutar el comando. Inténtalo de nuevo más tarde y, si se repite, avisa al staff.» |

El aviso nunca incluye el texto de la excepción: puede contener datos internos (URL de base de datos, rutas). El detalle queda solo en el log.

### 7.3 Envío del aviso (`_avisar`, `src/liga_bot/command_tree.py:134-144`)

- Si la interacción no tiene respuesta (`interaction.response.is_done()` es `False`), usa `interaction.response.send_message(..., ephemeral=True)`.
- Si ya la tiene (el comando hizo `defer()` o respondió antes de fallar), usa `interaction.followup.send(..., ephemeral=True)`.
- Si el envío falla con `discord.HTTPException` (por ejemplo, la interacción caducó) o `discord.InteractionResponded`, registra un `WARNING` y termina: el manejador no propaga excepciones.

**Visibilidad tras un `defer()` público:** según la documentación de la API de Discord, el primer `followup` después de `defer()` sustituye al mensaje «está pensando…» y conserva la visibilidad del `defer()` (no se ha comprobado contra Discord en ejecución). En `/equipos`, que hace `defer(ephemeral=False)` (`src/liga_bot/cogs/teams.py:246`), el aviso de error sería visible para todo el canal aunque se envíe con `ephemeral=True`. El resto de comandos que hacen `defer()` lo hacen con `ephemeral=True`.

### 7.4 Pruebas

`tests/test_command_tree_errors.py` registra comandos reales en el árbol de un `LigaBot` y los invoca con `CommandTree._call`, el método por el que discord.py despacha la interacción, para que la excepción recorra el camino real hasta `on_error`. Cubre la respuesta antes y después de `defer()`, el registro `ERROR` con traza, los mensajes específicos, que un check que ya respondió no recibe un segundo aviso, que un comando con manejador propio no recibe aviso y que un fallo al enviar el aviso no propaga excepciones.

