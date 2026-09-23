# Motor de Base de Datos y Gestión de Concurrencia

[⬅️ Volver a Arquitectura](./README.md)

Este documento detalla la arquitectura de persistencia base de `LigaBot`, implementada en `src/liga_bot/database.py`. Analiza la estrategia de motor dual (PostgreSQL para producción y PGlite para desarrollo y pruebas), el subsistema de serialización concurrente a nivel de motor (`_engine_locks`) para mitigar el fenómeno de *rollback bleed*, la configuración de factorías de sesión asíncronas y el ciclo de vida de recursos.

---

## 1. Arquitectura de Motor Dual

El módulo `src/liga_bot/database.py` desacopla la aplicación del gestor relacional físico mediante una factoría adaptativa (`get_engine`) capaz de operar sobre dos tecnologías complementarias:

```
                                  Settings.database_url
                                            |
                    +-----------------------+-----------------------+
                    |                                               |
                    v                                               v
        is_postgres (startswith "postgres")             is_pglite (startswith "pglite")
                    |                                               |
                    v                                               v
        create_async_engine()                        SQLAlchemyAsyncPGliteManager()
        - Driver: asyncpg                            - WebAssembly / Node.js
        - Pool: AsyncAdaptedQueuePool                - Socket UNIX (.s.PGSQL.5432)
        - pool_pre_ping=True                         - Single Connection (StaticPool)
        - Concurrencia nativa paralela               - Serialización con _engine_locks
```

### 1.1 PostgreSQL de Producción
Configurado cuando la URL inicia con `postgres://` o `postgresql://` (`src/liga_bot/database.py:107-112`):
- **Driver Asíncrono:** Utiliza `asyncpg` mediante la normalización en `settings.async_database_url` (`postgresql+asyncpg://...`).
- **Verificación de Liveness (`pool_pre_ping=True`):** Ejecuta una consulta de prueba transparente (`SELECT 1`) antes de entregar una conexión del pool, descartando de forma automática conexiones caídas por reinicios del servidor o timeouts de firewall.
- **Logging Adaptativo (`echo=(settings.log_level == "DEBUG")`):** Muestra sentencias SQL generadas únicamente cuando el nivel de logging del sistema está configurado en `DEBUG`.
- **Pool de Conexiones:** Opera sobre `AsyncAdaptedQueuePool` con concurrencia real multi-conexión (por defecto 5 conexiones fijas + 10 de desbordamiento).

### 1.2 PGlite para Entornos de Pruebas y Local
Configurado cuando la URL inicia con `pglite://` (`src/liga_bot/database.py:113-122`):
- **Motor WebAssembly:** Ejecuta PostgreSQL compilado a WebAssembly en un subproceso Node.js mediante `py-pglite[sqlalchemy]`.
- **Comunicación por Socket UNIX:** Se comunica mediante un archivo de socket UNIX dedicado (`.s.PGSQL.5432`).
- **Modos de Operación:**
  - *En Memoria (`pglite:///:memory:`):* Efímero, ideal para suites de pruebas automatizadas aisladas.
  - *Directorio Persistente (`pglite:///ruta/al/directorio`):* En `_prepare_pglite_config()`, resuelve la ruta absoluta y asegura el directorio con `mkdir(parents=True, exist_ok=True)`.

### 1.3 Validación Temprana de Esquemas
Si la URL no pertenece a los esquemas soportados (ej. `sqlite://`, `mysql://`), la factoría dispara de inmediato un `ValueError` explicativo (`src/liga_bot/database.py:124-127`), previniendo configuraciones inválidas en el arranque.

---

## 2. Serialización Concurrente a Nivel de Motor (`_engine_locks`)

### 2.1 El Problema: *Rollback Bleed* en Conexiones Únicas
PGlite y los motores configurados con `StaticPool` operan sobre una **única conexión física compartida** a nivel de socket o memoria:
- Si dos corrutinas asíncronas abren transacciones concurrentemente (`session_a` y `session_b`), ambas comparten el mismo descriptor de conexión física.
- Si `session_b` encuentra un error o ejecuta un `ROLLBACK`, envía la orden a nivel de protocolo PostgreSQL en la conexión compartida.
- Como consecuencia, las operaciones activas de `session_a` son abortadas de manera cruzada y silenciosa (*rollback bleed*), corrompiendo la consistencia de los datos.

### 2.2 La Solución: Registro de Locks por Motor
Para erradicar esta condición de carrera sin degradar el rendimiento de PostgreSQL en producción, `database.py` implementa un registro de cerrojos asíncronos indexado por motor (`src/liga_bot/database.py:34`):

```python
_engine_locks: dict[AsyncEngine, asyncio.Lock] = {}
```

#### Detección de Necesidad de Serialización (`_requires_serialization`, líneas 48-63)
```python
def _requires_serialization(engine: AsyncEngine | None) -> bool:
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

1. **Motores PGlite:** Si la instancia de motor está registrada en `_pglite_managers`, requiere serialización obligatoria (`True`).
2. **Motores con `StaticPool`:** Inspecciona de forma reflexiva el pool subyacente. Si es una instancia de `StaticPool`, activa la serialización (`True`).
3. **PostgreSQL Estándar:** Retorna `False`, permitiendo que las transacciones se ejecuten en paralelo aprovechando todas las conexiones del pool.

---

## 3. Factoría de Sesiones Asíncronas (`get_session_factory`)

Definida en `src/liga_bot/database.py:135-162`:

```python
factory = async_sessionmaker(
    bind=target_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)
```

### Decisiones de Diseño Críticas:

1. **`expire_on_commit=False` (Prevención de `MissingGreenlet`):**
   - En SQLAlchemy tradicional, al realizar un `commit()`, los atributos de los modelos se marcan como expirados.
   - En una aplicación asíncrona de Discord, tras persistir una entidad (por ejemplo un equipo o un jugador), el bot utiliza sus propiedades para construir mensajes incrustados (*embeds*).
   - Si los atributos expiraran, acceder a `team.name` requeriría una recarga perezosa sincrónica (*lazy load*), lo cual en SQLAlchemy 2.0 asíncrono dispara una excepción fatal: `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called`.
   - Con `expire_on_commit=False`, los objetos en memoria permanecen hidratados y utilizables de forma segura fuera del contexto transaccional.

2. **`autoflush=False`:**
   - Deshabilita los flushes automáticos e implícitos antes de cada sentencia `SELECT`.
   - Otorga a los repositorios el control absoluto sobre el momento en que se emiten las sentencias DML hacia el motor.

---

## 4. Context Managers Transaccionales

El módulo expone dos context managers asíncronos para delimitar transacciones:

### 4.1 `transactional_session` (`src/liga_bot/database.py:190-214`)
Diseñado para operaciones que requieren la semántica atómica de `session.begin()` de SQLAlchemy:

```python
@asynccontextmanager
async def transactional_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    factory = session_factory or get_session_factory()
    target_engine = factory.kw.get("bind") or _default_engine
    if isinstance(target_engine, AsyncConnection):
        target_engine = target_engine.engine

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

- **Comportamiento:**
  - Al entrar en el bloque, emite `BEGIN`.
  - Si el bloque finaliza sin errores, emite `COMMIT`.
  - Si se propaga una excepción, emite `ROLLBACK` y re-lanza el error.
  - Adquiere preventivamente el `asyncio.Lock` solo si `_requires_serialization()` es `True`.

### 4.2 `get_session` (`src/liga_bot/database.py:216-248`)
Diseñado como inyector de dependencias para comandos y servicios donde se requiere control explícito del ciclo `try...commit / except...rollback`:

```python
@asynccontextmanager
async def get_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    factory = session_factory or get_session_factory()
    target_engine = factory.kw.get("bind") or _default_engine
    if isinstance(target_engine, AsyncConnection):
        target_engine = target_engine.engine

    if _requires_serialization(target_engine):
        lock = _get_engine_lock(target_engine)
        async with lock:
            async with factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
    else:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
```

---

## 5. Ciclo de Vida y Prevención de Fugas de Memoria (`close_engine`)

La función `close_engine()` (`src/liga_bot/database.py:165-188`) garantiza la liberación ordenada de recursos físicos y de memoria:

```python
async def close_engine(engine: AsyncEngine | None = None) -> None:
    target_engine = engine or _default_engine
    if target_engine is None:
        return

    _engine_locks.pop(target_engine, None)

    manager = _pglite_managers.pop(target_engine, None)
    if manager is not None:
        await manager.stop()
    else:
        await target_engine.dispose()

    if target_engine is _default_engine:
        _default_engine = None
        _default_session_factory = None
```

### Mitigaciones de Seguridad Implementadas:
1. **Idempotencia:** Si se invoca con `None` o sobre un motor ya liberado, retorna inmediatamente sin error.
2. **Evicción de Cerrojos en `_engine_locks`:** En las suites de tests se crean y destruyen decenas de motores. Desalojar la clave mediante `_engine_locks.pop(target_engine, None)` evita que los motores recolectados queden referenciados en el diccionario global, previniendo fugas de memoria.
3. **Terminación de Procesos:** En PGlite invoca `await manager.stop()`, enviando la señal de parada al proceso Node.js y eliminando los sockets temporales. En PostgreSQL invoca `await target_engine.dispose()`, cerrando todas las conexiones abiertas en el pool.
