# Estrategia de Pruebas y Arquitectura de Ejecución

[⬅️ Volver a Testing](README.md) | [⬅️ Volver a Documentación](../README.md)

---

## 1. Filosofía de Pruebas y Principios Rectores

La infraestructura de pruebas de `DiscordBots` está construida sobre dos principios de ingeniería fundamentales: **hermetismo determinista** y **fidelidad de motor relacional**.

1. **Hermetismo sin Dependencias Externas:**
   La totalidad de la suite de pruebas se ejecuta de forma autosuficiente en el entorno local. Prescinde de contenedores Docker en tiempo de ejecución, procesos de base de datos previamente aprovisionados o llamadas de red salientes hacia la API de Discord o servicios de terceros. Esto garantiza una ejecución determinista, reproducible y con tiempos de arranque mínimos.

2. **Fidelidad Estricta con PostgreSQL Real (PGlite):**
   A diferencia de aproximaciones basadas en SQLite en memoria que ignoran las características específicas de PostgreSQL, el proyecto utiliza **PGlite** (`py-pglite[sqlalchemy]`). PGlite es una compilación WebAssembly/C de PostgreSQL empaquetada en un subproceso local de Node.js comunicado a través de un socket de dominio UNIX (`/tmp/.s.PGSQL.5432`). Esto permite validar con total exactitud de dialecto:
   - Tipos nativos de PostgreSQL: columnas `JSONB`, enumerados nativos (`ENUM`) y tipos de fecha con zona horaria (`TIMESTAMP WITH TIME ZONE`).
   - Restricciones DDL avanzadas: integridad referencial con `ON DELETE CASCADE`, claves primarias compuestas y restricciones `UNIQUE`.
   - Índices parciales condicionales (por ejemplo, el índice único parcial de capitanía `uq_team_single_captain` donde `is_captain = true`).
   - Comportamientos transaccionales idénticos a PostgreSQL en producción, incluyendo bloqueos a nivel de fila y aislamiento de lecturas.

3. **Prevención de Falsos Positivos mediante Pruebas de Desafío:**
   Los tests no solo verifican los caminos felices (*happy paths*), sino que atacan activamente las invariantes relacionales, las condiciones de carrera concurrentes y los límites del runtime del sistema operativo.

---

## 2. Arquitectura Piramidal de 4 Niveles

La suite de pruebas contiene **1.138 casos de prueba** distribuidos en **49 archivos de test** más el módulo central de fixtures `tests/conftest.py` (total de 50 archivos y **33.982 líneas totales de test**, 27.014 líneas efectivas de código excluyendo comentarios y blancos).

La estructura de las pruebas sigue una pirámide de cuatro niveles claramente segregados por responsabilidad, velocidad de ejecución y profundidad de integración:

```
                        ▲
                       / \
                      /   \     Nivel 4: Adversarial Challenge Suites
                     /-----\    (16 archivos | 333 tests | 29.3% | 12.758 LoC)
                    /       \
                   /         \   Nivel 3: Resilience, Concurrency & Stress
                  /-----------\  (5 archivos | 110 tests | 9.7% | 3.305 LoC)
                 /             \
                /               \ Nivel 2: Integration & End-to-End Suites
               /-----------------\ (6 archivos | 171 tests | 15.0% | 5.853 LoC)
              /                   \
             /                     \ Nivel 1: Unit Suites
            /-----------------------\ (22 archivos | 524 tests | 46.0% | 11.986 LoC)
```

### Métricas de Distribución de la Pirámide

| Nivel | Categoría | Archivos | Tests | % Tests | LoC | % LoC | Tiempo Medio |
|---|---|---:|---:|---:|---:|---:|---|
| **Nivel 1** | Unit Suites | 22 | 524 | 46.0% | 11.986 | 35.3% | < 0.005s / test |
| **Nivel 2** | Integration & E2E Suites | 6 | 171 | 15.0% | 5.853 | 17.2% | ~0.02s / test |
| **Nivel 3** | Resilience, Concurrency & Stress | 5 | 110 | 9.7% | 3.305 | 9.7% | ~0.05s / test |
| **Nivel 4** | Adversarial Challenge Suites | 16 | 333 | 29.3% | 12.758 | 37.5% | ~0.03s / test |
| **Fixtures**| Módulo Raíz (`conftest.py`) | 1 | - | - | 80 | 0.2% | Overhead de sesión |
| **TOTAL** | **Suite Completa** | **49 (+1)** | **1.138** | **100.0%** | **33.982** | **100.0%** | **~35.8s total** |

### Descripción Funcional de los Niveles

- **Nivel 1 — Pruebas Unitarias (524 tests):**
  Aíslan componentes individuales (modelos SQLAlchemy, lógica de configuración Pydantic, utilidades de formateo, comandos CLI, servicios con dependencias simuladas mediante mocks, vistas y cogs de Discord). No tocan la base de datos ni sockets de red; su tiempo de ejecución es del orden de submilisegundos.

- **Nivel 2 — Pruebas de Integración y E2E (171 tests):**
  Validan la interacción de repositorios (`BaseRepository`, `RoleRequestRepository`, repositorios de plantillas) directamente contra el motor PGlite migrado. También cubren flujos de extremo a extremo (E2E), como el ciclo completo de verificación de roles (`solicitud -> embed en canal staff -> interacción con botones -> persistencia y roles`) y sincronización de plantillas ante eventos del gateway.

- **Nivel 3 — Pruebas de Resiliencia, Concurrencia y Estrés (110 tests):**
  Someten al sistema a ráfagas asíncronas con `asyncio.gather()`, ciclos rápidos de apertura y cierre de sockets de red (`port=0`), contención sobre pools de conexión única, y simulación de fallos transitorios de red o códigos HTTP de error de la API de Discord (429 Rate Limit, 503 Service Unavailable, 403 Forbidden).

- **Nivel 4 — Suites de Desafío Adversarial (333 tests):**
  Suites concebidas específicamente para forzar fallos en el sistema: borrados en cascada con SQL crudo frente al ORM, eliminación masiva de entidades con lotes de inserción límite, ataques de violación de clave primaria compuesta (`team_id`, `discord_user_id`), alteración de auditoría inmutable, parámetros corruptos en comandos slash y fuzzing de columnas JSONB.

---

## 3. Jerarquía y Ciclo de Vida de Fixtures (`tests/conftest.py`)

La configuración global de fixtures en `tests/conftest.py` define una jerarquía estricta de scopes que maximiza la velocidad de ejecución preservando el aislamiento absoluto entre pruebas.

### 3.1 Servidor PGlite de Ámbito de Sesión (`pglite_manager`)

El arranque de un proceso Node.js con WebAssembly conlleva una sobrecarga inicial de ~0.4 segundos. Para amortizar este coste en una sola ocasión durante toda la sesión de pruebas, se utiliza un fixture de ámbito de sesión:

```python
# tests/conftest.py:25-33
@pytest_asyncio.fixture(scope="session")
async def pglite_manager() -> AsyncGenerator[SQLAlchemyAsyncPGliteManager, None]:
    """Inicia el servidor local de PGlite para la sesión de pruebas."""
    manager = SQLAlchemyAsyncPGliteManager()
    manager.start()
    await manager.wait_for_ready()
    yield manager
    await manager.stop()
```

- **`manager.start()`**: Despliega el proceso Node.js en segundo plano y crea el socket de dominio UNIX (`/tmp/.s.PGSQL.5432`).
- **`await manager.wait_for_ready()`**: Bloquea asíncronamente hasta verificar que el socket acepta conexiones TCP/UNIX válidas.
- **`yield manager`**: Mantiene vivo el servidor durante los 1.138 tests.
- **`await manager.stop()`**: Al terminar toda la sesión de pytest, envía la señal de terminación limpia al subproceso y purga los descriptores del sistema de archivos.

### 3.2 Migraciones de Alembic y Modelos Compartidos (`migrated_db`)

El fixture `migrated_db` aplica el esquema completo sobre PGlite una sola vez por sesión:

```python
# tests/conftest.py:41-64
@pytest_asyncio.fixture(scope="session")
async def migrated_db(async_engine: AsyncEngine) -> AsyncEngine:
    """Aplica las migraciones de Alembic sobre PGlite y crea en memoria las tablas compartidas."""
    cfg = Config("alembic.ini")

    async with async_engine.connect() as conn:

        def do_upgrade(sync_conn):
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

            # Garantizar que los modelos compartidos estén importados y registrados en Base.metadata
            import liga_bot.models  # noqa: F401
            import liga_bot.models.roster  # noqa: F401
            from liga_bot.models.base import Base

            # Crea en memoria las tablas excluidas de Alembic respetando checkfirst=True
            Base.metadata.create_all(sync_conn)

        await conn.run_sync(do_upgrade)
        await conn.commit()

    return async_engine
```

- Aplica todas las revisiones DDL de Alembic hasta la versión más reciente (`head`).
- Importa todos los modelos declarativos de `liga_bot.models` y `liga_bot.models.roster`, registrándolos en `Base.metadata`.
- Ejecuta `Base.metadata.create_all(sync_conn)` con `checkfirst=True` para asegurar que las tablas compartidas (como `teams`, `discord_users`, `team_memberships`, `roster_movements`, `audit_logs`) queden creadas con sus índices, restricciones de unicidad y claves foráneas.

### 3.3 Aislamiento por Test mediante Savepoints Anidados (`session`)

Para las pruebas que requieren acceso a base de datos en ámbito de función (`scope="function"`), se aplica el patrón canónico de **Savepoint Rollback** de SQLAlchemy 2.0:

```python
# tests/conftest.py:66-80
@pytest_asyncio.fixture
async def session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Proporciona una AsyncSession aislada por test con rollback automático."""
    async with migrated_db.connect() as conn:
        trans = await conn.begin()
        async_session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            await trans.rollback()
```

#### Mecánica de Aislamiento
1. **Transacción Raíz:** `trans = await conn.begin()` inicia una transacción en la conexión compartida.
2. **Modo `create_savepoint`:** Al configurar `join_transaction_mode="create_savepoint"`, cualquier llamada interna a `await session.commit()` realizada por repositorios o servicios no emite un `COMMIT` real en la base de datos, sino un `RELEASE SAVEPOINT` o `SAVEPOINT`.
3. **Rollback en Bloque `finally`:** Al concluir el test (tanto si pasa como si falla con excepción), `await trans.rollback()` descarta de forma instantánea cualquier mutación sobre el esquema en menos de 1 milisegundo, sin requerir reconstrucción de tablas.

---

## 4. Estrategia de Aislamiento Avanzado en Concurrencia y Estrés

### 4.1 La Limitación de los Savepoints en Multi-Sesión
Cuando se prueban servicios que gestionan sus propias sesiones asíncronas a través de una factoría (`session_factory = async_sessionmaker(bind=migrated_db)`), como ocurre en `tests/test_adversarial_roster_concurrency_and_audit.py` y `tests/test_roster_sync_stress.py`, no es posible utilizar el fixture `session` basado en savepoints de una sola conexión. Cada sesión abierta por el servicio intentaría abrir transacciones concurrentes independientes sobre el mismo pool de conexión única (`StaticPool`), provocando conflictos de protocolo o visibilidad de datos parciales.

### 4.2 Fixture de Truncado en Cascada (`clean_roster_tables`)
Para resolver este escenario, las suites de concurrencia y estrés emplean una fixture con limpieza atómica por DDL:

```python
# tests/test_adversarial_roster_concurrency_and_audit.py:115-140
@pytest_asyncio.fixture(autouse=True)
async def clean_roster_tables(migrated_db: AsyncEngine) -> AsyncGenerator[None, None]:
    """
    Limpia las tablas compartidas antes y después de cada prueba
    y refresca el lock del event loop actual para PGlite.
    """
    _engine_locks[migrated_db] = asyncio.Lock()
    async with migrated_db.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                "players, teams, discord_users CASCADE;"
            )
        )
        await conn.commit()
    yield
    _engine_locks[migrated_db] = asyncio.Lock()
    async with migrated_db.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                "players, teams, discord_users CASCADE;"
            )
        )
        await conn.commit()
```

#### Ventajas Técnicas
- **Reinicio del Lock de Serialización:** `_engine_locks[migrated_db] = asyncio.Lock()` reasigna el cerrojo de serialización de transacciones vinculándolo al bucle de eventos (`asyncio.AbstractEventLoop`) creado por `pytest-asyncio` para ese test en concreto, evitando el error `RuntimeError: Task attached to a different loop`.
- **`TRUNCATE TABLE ... CASCADE` Atómico:** Limpia el contenido de las tablas respetando la integridad referencial en un único viaje de ida y vuelta al socket, dejando la base de datos limpia en ~3 a 5 milisegundos.

---

## 5. Factoría de Mocks Especializados de Discord

Para garantizar la independencia de la red externa y simular las estructuras de la biblioteca `discord.py` con fidelidad de tipos e interfaces, la suite utiliza factorías especializadas de mocks en memoria:

### 5.1 Factoría de Roles (`make_mock_role`)
```python
# tests/test_adversarial_roster_concurrency_and_audit.py:60-66
def make_mock_role(role_id: int, name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role
```
Garantiza el cumplimiento estricto de la especificación de `discord.Role`, permitiendo comparar identidades por ID y generar menciones sintácticamente válidas.

### 5.2 Factoría de Miembros (`make_mock_member`)
```python
# tests/test_adversarial_roster_concurrency_and_audit.py:69-91
def make_mock_member(
    user_id: int,
    name: str,
    roles: list[discord.Role] | None = None,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.global_name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles) if roles is not None else []
    member.guild = MagicMock(spec=discord.Guild)
    member.guild.id = 123456789
    member.avatar = MagicMock()
    member.avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/abc.png"
    member.display_avatar = member.avatar

    # Espías asíncronos para registrar efectos secundarios
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member
```
Expone métodos asíncronos simulados (`AsyncMock`) para `add_roles` y `remove_roles`, permitiendo verificar las llamadas de sincronización de roles en los servicios y cogs sin interactuar con los servidores de Discord.

### 5.3 Factoría de Servidores (`make_mock_guild`) e Interacciones (`make_mock_interaction`)
- **`make_mock_guild`** (`tests/test_roster_cog.py:87-100`): Simula `discord.Guild`, proveyendo `get_role` síncrono y `fetch_member` asíncrono para resolución de entidades.
- **`make_mock_interaction`** (`tests/test_roster_cog.py:102-120`): Simula `discord.Interaction` con `response.send_message = AsyncMock()`, `response.defer = AsyncMock()` y `followup.send = AsyncMock()`, asegurando que las respuestas efímeras o públicas a comandos slash queden capturadas para aserción.
