# Límites de Entorno e Infraestructura del Sistema

[⬅️ Volver a Testing](README.md) | [⬅️ Volver a Documentación](../README.md)

---

## 1. Introducción y Contexto de Infraestructura

El software de alta concurrencia no opera en un vacío teórico; interactúa continuamente con las capas de red, los descriptores del kernel y la memoria intermedia del sistema operativo. Durante las pruebas de estrés del proyecto `DiscordBots`, se identificaron y resolvieron **tres límites empíricos críticos** relacionados con la infraestructura y el entorno de ejecución:

1. **Desbordamiento de buffer en sockets UNIX (Node.js 24+ con PGlite)**: saturación en sentencias `INSERT ... RETURNING` superiores a 16KB.
2. **Colisión de transacciones concurrentes en motores de conexión única**: contención de operaciones concurrentes en `StaticPool` / PGlite.
3. **Agotamiento de descriptores de socket de red**: fuga o contención de descriptores TCP en ciclos rápidos de reinicio y parada del WebSocket Bridge.

Este documento detalla la causa raíz técnica, el mecanismo de fallo, el impacto en el sistema y la mitigación exacta implementada y verificada en el código fuente.

---

## 2. Límite 1: Desbordamiento de Buffer en Sockets UNIX (Node.js 24+ con PGlite)

### 2.1 Ubicación y Evidencia en el Código Fuente
- **Archivo:** `tests/test_adversarial_roster_cascades.py`
- **Líneas:** 1121 a 1138, y 1150 a 1184 (en el método `test_bulk_team_cascade_deletion_stress`).

```python
# tests/test_adversarial_roster_cascades.py:1121-1138
        # NOTA DE ENTORNO (Node.js 24+ / PGlite):
        # En versiones recientes de Node.js (24+), la gestión de fragmentación de buffers
        # sobre sockets UNIX en @electric-sql/pglite-socket sufre desbordamientos al recibir
        # consultas INSERT multi-fila con RETURNING superiores a 16KB (~97+ registros de golpe),
        # cerrando el socket y tumbando la instancia efímera de la base de datos para el resto
        # de la sesión de tests. Para evitar este fallo de infraestructura, creamos los 100
        # usuarios en lotes de 25, preservando íntegra la volumetría del test de estrés sin
        # saturar el socket.
        # Create 100 users
        for chunk in range(4):
            batch = [
                DiscordUser(discord_id=f"stress_u_{i:03d}", username=f"stress_user_{i}")
                for i in range(chunk * 25, (chunk + 1) * 25)
            ]
            session.add_all(batch)
            await session.flush()
            users.extend(batch)
```

### 2.2 Mecanismo Técnico del Fallo
1. **La Pila de Comunicación:** PGlite se ejecuta en WebAssembly dentro de un proceso hijo de Node.js mediante el paquete `@electric-sql/pglite-socket`. El driver de Python (`py-pglite`) se comunica con este proceso hijo a través de un socket de dominio UNIX (`/tmp/.s.PGSQL.5432`).
2. **Fragmentación de Buffers en Node.js 24+:** A partir de Node.js v24.0.0, el subsistema de streams y sockets de dominio UNIX (`net.Socket`) incorporó optimizaciones de tamaño de buffer. Cuando un cliente despacha un mensaje o recibe una respuesta continua que excede los **16.384 bytes (16KB)** sin pausas para vaciar el flujo, el módulo intermediario de `@electric-sql/pglite-socket` pierde la sincronización de fragmentos al no gestionar adecuadamente la contrapresión (*backpressure*).
3. **El Punto de Quiebre (~97 registros):**
   - SQLAlchemy 2.0 compila las inserciones multi-entidad en sentencias SQL masivas agregando la cláusula `RETURNING *` para rellenar los atributos generados en el servidor (claves primarias, marcas de tiempo).
   - Un registro de `DiscordUser` serializado en el protocolo binario de PostgreSQL ocupa entre 165 y 175 bytes.
   - Al intentar insertar 100 registros en una única sentencia atómica `INSERT INTO discord_users ... VALUES (...), (...) RETURNING *`, el tamaño total de la trama de respuesta excede los 16.800 bytes (>16KB).
4. **Impacto en el Sistema:**
   El socket UNIX experimenta una ruptura de tubería con señales `SIGPIPE` / `ECONNRESET`. El proceso Node.js finaliza abruptamente. Como el fixture `pglite_manager` tiene ámbito de sesión, **todas las pruebas restantes de pytest fallan en cadena** con `ConnectionClosedError`, invalidando la ejecución de la suite completa.

### 2.3 Mitigación Quirúrgica Verificada
Para conservar íntegramente la volumetría requerida por el test de estrés (100 usuarios, 20 equipos, 100 miembros y 100 movimientos) sin desbordar el socket, se implementó una estrategia de **fraccionamiento en lotes (*chunking*) con vaciado forzado**:

1. **Lotes de 25 Registros:**
   La creación de los 100 usuarios se divide en 4 iteraciones de 25 registros (`for chunk in range(4):`). Un bloque de 25 registros genera una trama de ~4.2KB, manteniéndose holgadamente por debajo del umbral de saturación de 16KB.
2. **Vaciado Intermedio con `await session.flush()`:**
   En cada iteración, tras `session.add_all(batch)`, se invoca inmediatamente `await session.flush()` (`línea 1136`). Esto fuerza el envío de la consulta, la recepción del resultado y la liberación completa del buffer del socket UNIX antes de emitir el siguiente bloque.
3. **Inserción de Entidades Relacionadas:**
   Para los 20 equipos y sus entidades dependientes:
   - Cada equipo se añade e introduce con su propio `flush()` individual (`líneas 1157-1158`).
   - Para cada equipo, sus 5 membresías y 5 movimientos de plantilla (10 registros en total, ~1.8KB) se agregan en bloque y se vacían inmediatamente con `await session.flush()` (`línea 1183`).

Esta disciplina de fraccionamiento garantiza una estabilidad del 100% en la ejecución de la prueba sobre Node.js 24+, preservando la rigurosidad de las validaciones de borrado en cascada con SQL directo (`DELETE FROM teams WHERE id = ANY(:tids)`).

---

## 3. Límite 2: Serialización de Transacciones en Motores de Conexión Única (`StaticPool` / PGlite)

### 3.1 Ubicación y Evidencia en el Código Fuente
- **Implementación Central:** `src/liga_bot/database.py:33-64, 200-214`
- **Verificación Adversarial:** `tests/test_adversarial_roster_concurrency_and_audit.py:451-525`

```python
# src/liga_bot/database.py:33-64
# Registro de locks de serialización por motor para motores de conexión única (PGlite / StaticPool)
_engine_locks: dict[AsyncEngine, asyncio.Lock] = {}


def _get_engine_lock(engine: AsyncEngine) -> asyncio.Lock:
    """Devuelve o crea el asyncio.Lock asociado al motor especificado."""
    lock = _engine_locks.get(engine)
    if lock is None:
        lock = asyncio.Lock()
        _engine_locks[engine] = lock
    return lock


def _requires_serialization(engine: AsyncEngine | None) -> bool:
    """
    Determina si un motor de base de datos requiere serialización de transacciones.
    Retorna True para motores PGlite o aquellos que utilicen StaticPool (conexión única compartida).
    Retorna False para motores multi-conexión como PostgreSQL con AsyncAdaptedQueuePool.
    """
    if engine is None:
        return False
    if engine in _pglite_managers:
        return True
    sync_engine = getattr(engine, "sync_engine", None)
    if sync_engine is not None:
        pool = getattr(sync_engine, "pool", None)
        if isinstance(pool, StaticPool):
            return True
    return False
```

```python
# src/liga_bot/database.py:204-214
    if _requires_serialization(target_engine):
        lock = _get_engine_lock(target_engine)
        async with lock:
            async with factory() as session:
                async with session.begin():
                    yield session
    else:
        async with factory() as session:
            async with session.begin():
                yield session
```

### 3.2 Mecanismo Técnico del Fallo
1. **Diferencia entre Producción y PGlite:** En producción sobre PostgreSQL estándar, SQLAlchemy utiliza `AsyncAdaptedQueuePool`, asignando una conexión física independiente a cada sesión asíncrona concurrente. En entornos de prueba o desarrollo local con PGlite, se utiliza `StaticPool`, donde existe **una única conexión física compartida** hacia el socket UNIX.
2. **Colisión de Protocolo:**
   Si dos corrutinas asíncronas ejecutan consultas concurrentemente mediante `asyncio.gather()` utilizando sesiones distintas sobre la misma conexión física sin serialización, ambas corrutinas intentan escribir y leer paquetes de protocolo PostgreSQL de forma entrelazada.
3. **Impacto:**
   El driver asíncrono detecta que el canal está ocupado y lanza inmediatamente una excepción irrecuperable:
   ```
   asyncpg.exceptions.InterfaceError: cannot perform operation: another operation is in progress
   ```
   o bien provoca desincronización de tramas en el socket UNIX.

### 3.3 Mitigación y Resiliencia Verificada
1. **Detección Dinámica de Necesidad de Serialización:**
   `_requires_serialization()` inspecciona el motor. Si el motor pertenece al registro de PGlite (`_pglite_managers`) o su pool subyacente es una instancia de `StaticPool`, activa la serialización. Si es un PostgreSQL de producción con pool de colas, retorna `False` y no impone sobrecarga alguna de cerrojos.
2. **Serialización Transaccional con `asyncio.Lock`:**
   En los context managers `transactional_session` y `get_session`, el acceso al bloque de transacción se protege con `async with lock:`. Esto asegura que las operaciones concurrentes hagan cola ordenadamente y ejecuten sus transacciones de principio a fin de forma atómica.
3. **Resiliencia ante Fallos de Transacción (`test_engine_lock_recovery_after_transaction_failure`):**
   En `tests/test_adversarial_roster_concurrency_and_audit.py:492-525`, se fuerza deliberadamente una excepción no controlada (`ValueError("Intentional failure")`) en mitad de una transacción mientras el cerrojo está retenido:
   ```python
   # tests/test_adversarial_roster_concurrency_and_audit.py:507-516
   with pytest.raises(ValueError, match="Intentional failure"):
       async with transactional_session(session_factory) as session:
           session.add(DiscordUser(discord_id=user_id_str, username="FailingUser"))
           await session.flush()
           raise ValueError("Intentional failure")

   # Inmediatamente ejecutar una transacción normal; el lock debe estar libre
   async with session_factory() as session:
       user = await session.get(DiscordUser, user_id_str)
       assert user is None  # Rollback exitoso
   ```
   El diseño garantiza que la salida del bloque `async with lock:` libera el cerrojo de manera atómica e incondicional y ejecuta el rollback, permitiendo que la corrutina subsiguiente opere inmediatamente sin interbloqueos (*deadlocks*).
4. **Aislamiento de Bucle de Eventos en Pytest:**
   Dado que `pytest-asyncio` crea un nuevo `event_loop` para cada test, un `asyncio.Lock` vinculado a un loop anterior ya cerrado arrojaría `RuntimeError: Task attached to a different loop`. Para evitarlo, la fixture `clean_roster_tables` (`líneas 121 y 131`) refresca el diccionario `_engine_locks[migrated_db] = asyncio.Lock()` al inicio y al final de cada test.

---

## 4. Límite 3: Agotamiento y Ciclo de Vida de Descriptores de Sockets de Red

### 4.1 Ubicación y Evidencia en el Código Fuente
- **Prueba de 20 Ciclos con Puertos Efímeros:** `tests/test_bot_bridge_lifecycle_resilience.py:75-123`
- **Prueba de Cierre con Clientes Activos:** `tests/test_bot_bridge_lifecycle_resilience.py:125-191`
- **Prueba de 50 Cierres Concurrentes:** `tests/test_bot_bridge_lifecycle_resilience.py:583-620`
- **Implementación Atómica de Cierre:** `src/liga_bot/bot.py:188-196`
- **Rutina de Apagado de Sockets:** `src/liga_bot/services/websocket_bridge_service.py:108-140`

```python
# tests/test_bot_bridge_lifecycle_resilience.py:75-123
@pytest.mark.asyncio
async def test_resilience_rapid_start_stop_real_ephemeral_sockets_20_cycles():
    """
    Stress-test empírico con la pila de red real:
    Abre y cierra el servidor WebSocket Bridge real 20 veces en puertos efímeros (port=0).
    Verifica que el sistema operativo y aiohttp liberan sockets sin fugas de descriptores
    y que /health responde 200 en cada ciclo.
    """
    for cycle in range(20):
        settings = Settings(
            bridge_enabled=True,
            bridge_port=0,
            discord_bot_supertoken="resilience-token-xyz",
        )
        bot = LigaBot(settings=settings, extensions=())
        ...
        await bot.setup_hook()
        port = bot.websocket_bridge_service.port
        assert port > 0
        ...
        await bot.close()
        assert bot.websocket_bridge_service is None
        ...
```

### 4.2 Mecanismo Técnico del Fallo
1. **Descriptores del Kernel y Estados TCP:**
   Cuando una aplicación basada en `aiohttp` abre y cierra repetidamente servidores HTTP y WebSocket durante suites de pruebas o reinicios rápidos de procesos, los descriptores de archivo del sistema operativo (`file descriptors`) pueden quedar retenidos en el kernel de Linux. Si los sockets no se limpian de manera ordenada, entran en estado `TIME_WAIT` o quedan huérfanos, derivando en:
   - Error `EMFILE` (*Too many open files*) al superar el límite de descriptores por proceso (`ulimit -n`).
   - Error `EADDRINUSE` (*Address already in use*) al intentar enlazar nuevamente un puerto estático.
2. **Carrera por Concurrencia en el Apagado (`bot.close()`):**
   Si múltiples corrutinas o controladores de señales invocan `bot.close()` simultáneamente, se produce una condición de carrera sobre `self.websocket_bridge_service`: dos corrutinas podrían intentar invocar `bridge.stop()` a la vez, causando excepciones `AttributeError` o intentos redundantes de cerrar sockets ya destruidos.

### 4.3 Mitigación Quirúrgica Verificada
1. **Asignación Dinámica de Puertos Efímeros (`port=0`):**
   Para pruebas y entornos de integración, el servicio permite configurar `bridge_port=0`. El kernel del sistema operativo asigna dinámicamente un puerto libre disponible, eliminando cualquier posibilidad de conflicto de puerto (`EADDRINUSE`) entre ejecuciones paralelas.
2. **Desacoplamiento Atómico Previo al `await`:**
   En `src/liga_bot/bot.py:188-196`:
   ```python
   # 1. Detener WebSocket Bridge si está activo
   if self.websocket_bridge_service is not None:
       logger.info("Deteniendo WebSocket Bridge...")
       bridge = self.websocket_bridge_service
       self.websocket_bridge_service = None  # Extracción atómica previa al await
       try:
           await bridge.stop()
       except Exception as exc:
           logger.warning("Error deteniendo WebSocket Bridge: %s", exc)
   ```
   Al asignar `self.websocket_bridge_service = None` **antes** de ceder el control asíncrono con `await bridge.stop()`, cualquier otra corrutina concurrente que llame a `bot.close()` encuentra la referencia en `None` y no ejecuta la parada por duplicado.
3. **Procedimiento Trifásico de Cierre en `WebsocketBridgeService.stop()`:**
   En `src/liga_bot/services/websocket_bridge_service.py:108-140`:
   - **Fase 1 (Cierre de Sockets Activos):** Itera sobre `list(self._active_sockets)` y envía a cada cliente WebSocket un marco de cierre RFC 6455 con código 1000 (`code=1000, message=b"Server shutting down"`). Luego vacía la colección con `clear()`.
   - **Fase 2 (Drenaje de Tareas en Segundo Plano):** Espera hasta 2.0 segundos para que las tareas en curso finalicen (`asyncio.wait(self._background_tasks, timeout=2.0)`). Aquellas que permanezcan pendientes son canceladas con `t.cancel()` y recolectadas con `asyncio.gather(*pending, return_exceptions=True)`.
   - **Fase 3 (Limpieza del Runner HTTP):** Invoca `await self._runner.cleanup()`, cerrando el `AppRunner`, el `TCPSite` y los sockets del sistema operativo, reseteando las referencias internas a `None`.
4. **Verificación de Estrés Masivo (50 Tareas Concurrentes):**
   En `test_resilience_concurrent_close_stress_50_tasks` (`tests/test_bot_bridge_lifecycle_resilience.py:583-620`), 50 corrutinas asíncronas llaman simultáneamente a `bot.close()` con retardos asíncronos (*jitter*) inyectados en `bridge.stop()`. La prueba certifica que:
   - `bridge_stop_counter == 1` exactamente (idempotencia y exclusión mutua perfecta).
   - Ninguna tarea arroja excepciones de atributo ni de referencia nula.
   - Todos los descriptores de socket quedan liberados y el endpoint `/health` rechaza nuevas conexiones de forma inmediata.

---

## 5. Tabla Comparativa de Límites y Mitigaciones

| Límite Identificado | Subsistema Afectado | Síntoma Sin Mitigación | Estrategia de Mitigación | Verificación Automatizada |
|---|---|---|---|---|
| **Buffer Overflow UNIX** | Node.js 24+ / `@electric-sql/pglite-socket` | `SIGPIPE` / `ECONNRESET` al superar 16KB (~97+ registros con RETURNING) | Lotes de 25 registros + `await session.flush()` explícito | `test_bulk_team_cascade_deletion_stress` (20 equipos, 100 usuarios, 100 miembros, 100 movimientos) |
| **Serialización Transaccional** | `StaticPool` / PGlite | `InterfaceError: another operation is in progress` en queries concurrentes | Registro `_engine_locks` + `asyncio.Lock` en `transactional_session` | `test_pglite_engine_lock_handling_across_test_iterations`, `test_engine_lock_recovery_after_transaction_failure` |
| **Agotamiento de Sockets** | Kernel Linux / `aiohttp` / WebSocket Bridge | `EMFILE` / `EADDRINUSE` / excepciones concurrentes en parada | `port=0` efímero + extracción atómica `self.websocket_bridge_service = None` + cierre trifásico | `test_resilience_rapid_start_stop_real_ephemeral_sockets_20_cycles`, `test_resilience_concurrent_close_stress_50_tasks` |
