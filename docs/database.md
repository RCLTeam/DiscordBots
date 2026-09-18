# Base de Datos y Persistencia — LigaBot

Este documento describe la arquitectura relacional, el modelo entidad-relación, las restricciones de integridad, el sistema de migraciones versionadas con Alembic y la configuración del motor dual (PGlite y PostgreSQL) en **LigaBot**.

---

## 1. Filosofía de Persistencia y SQLAlchemy 2.0

LigaBot utiliza **SQLAlchemy 2.0** en modo puramente asíncrono (`AsyncEngine` y `AsyncSession`). Los principios de diseño de la capa de persistencia son:

- **Modelos Declarativos Fuertemente Tipados**: Uso de `Mapped[...]` y `mapped_column(...)` con anotaciones de tipo nativas de Python (PEP 484 / PEP 681).
- **Identificadores Universales Únicos (UUID v4)**: Todas las claves primarias son de tipo UUID, generadas en el cliente o servidor, desacoplando la generación de IDs de secuencias numéricas autoincrementales.
- **Transacciones Atómicas y Unit of Work**: Encapsulamiento de las operaciones de escritura en context managers transaccionales (`transactional_session`), asegurando que o bien todas las escrituras se confirman (`COMMIT`), o ninguna surte efecto ante cualquier fallo (`ROLLBACK`).
- **Carga Voraz (*Eager Loading*) Segura**: Las relaciones entre modelos utilizan la estrategia `lazy="selectin"`. En SQLAlchemy asíncrono, el acceso a relaciones diferidas (*lazy loading*) fuera del contexto de una sesión activa desencadena el error `MissingGreenlet`. El uso de `selectinload` resuelve las dependencias en una consulta secundaria inmediata sin incurrir en bloqueos.
- **Neutralidad del Motor**: El código de modelos y repositorios está diseñado para ejecutarse indistintamente sobre **PostgreSQL** estándar y sobre **PGlite** (PostgreSQL 17 en WebAssembly/Node.js).

---

## 2. Diagrama Entidad-Relación (ER)

```mermaid
erDiagram
    teams ||--o{ matches : "participa como team1"
    teams ||--o{ matches : "participa como team2"

    teams {
        uuid id PK "UUID v4 clave primaria"
        varchar(100) name UK "Nombre oficial del equipo"
        varchar(4) tag "Acrónimo de 1 a 4 caracteres"
        varchar(100) slug "Slug normalizado para canal"
        division division "PREMIER o ASCEND"
        bigint discord_role_id UK "Snowflake ID del rol de Discord"
        timestamptz created_at "Fecha y hora de registro"
    }

    matches {
        uuid id PK "UUID v4 clave primaria"
        integer jornada "Número de la jornada (>= 1)"
        division division "PREMIER o ASCEND"
        uuid team1_id FK "Clave foránea hacia teams.id (ondelete CASCADE)"
        uuid team2_id FK "Clave foránea hacia teams.id (ondelete CASCADE)"
        bigint discord_channel_id UK "Snowflake ID del canal creado (nullable)"
        timestamptz scheduled_at "Fecha y hora programada (nullable)"
        matchstatus status "PENDIENTE, CANAL_CREADO, JUGADO, CANCELADO"
        timestamptz created_at "Fecha y hora de creación"
    }

    ticket_notices {
        uuid id PK "UUID v4 clave primaria"
        bigint discord_channel_id UK "Snowflake ID del canal de soporte"
        varchar(100) category_name "Nombre de la categoría del ticket"
        timestamptz last_staff_message_at "Último mensaje emitido por staff"
        timestamptz last_alert_sent_at "Fecha y hora del último aviso de inactividad"
        boolean is_pending_staff "Indica si requiere intervención de staff"
        timestamptz created_at "Fecha y hora de registro"
    }
```

---

## 3. Catálogo de Esquema y Tablas

### 3.1 Tipos Enumerados Nativos (PostgreSQL ENUM)

- **`division`**:
  - Valores: `'PREMIER'`, `'ASCEND'`
  - Representa el circuito competitivo en el que compite el equipo o partido.
- **`matchstatus`**:
  - Valores: `'PENDIENTE'`, `'CANAL_CREADO'`, `'JUGADO'`, `'CANCELADO'`
  - Representa el estado en el flujo operativo del partido.

---

### 3.2 Tabla `teams`

Almacena los equipos registrados en la liga, sus acrónimos y la vinculación con los roles de Discord.

| Columna | Tipo de Dato | Nulable | Valor por Defecto | Descripción |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `UUID` | No | `uuid.uuid4()` | Clave primaria única. |
| `name` | `VARCHAR(100)` | No | — | Nombre oficial del equipo (ej: `Planar Shock Pingus`). |
| `tag` | `VARCHAR(4)` | No | — | Acrónimo o tag competitivo (máximo 4 caracteres, ej: `PSP`). |
| `slug` | `VARCHAR(100)` | No | — | Cadena normalizada en minúsculas y sin acentos para nombres de canal (ej: `planar-shock-pingus`). |
| `division` | `ENUM('PREMIER', 'ASCEND')` | No | — | División a la que pertenece el equipo. |
| `discord_role_id` | `BIGINT` | No | — | Identificador Snowflake del rol asignado en el servidor de Discord. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | No | `now()` | Marca de tiempo UTC de creación del registro. |

#### Restricciones e Índices en `teams`
- **`pk_teams`**: Clave primaria sobre `id`.
- **`uq_teams_name`**: Restricción única sobre `name`. Evita duplicar equipos con nombres idénticos.
- **`uq_teams_discord_role_id`**: Restricción única sobre `discord_role_id`. Cada rol de Discord solo puede estar asociado a un equipo.
- **`ck_teams_tag_length`**: `CHECK (char_length(tag) <= 4)`. Garantiza a nivel de motor relacional que el tag nunca supere los 4 caracteres.
- **`ix_teams_slug`**: Índice no único sobre `slug` para agilizar búsquedas por nombre normalizado.
- **`ix_teams_division`**: Índice no único sobre `division` para optimizar consultas de filtrado en `/equipos` o servicios de calendario.

---

### 3.3 Tabla `matches`

Gestiona los enfrentamientos deportivos de cada jornada, los equipos participantes y el canal de texto asignado.

| Columna | Tipo de Dato | Nulable | Valor por Defecto | Descripción |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `UUID` | No | `uuid.uuid4()` | Clave primaria única. |
| `jornada` | `INTEGER` | No | — | Número de la jornada de competición (ej: `1`, `2`, ...). |
| `division` | `ENUM('PREMIER', 'ASCEND')` | No | — | División del enfrentamiento. Debe coincidir con la división de ambos equipos. |
| `team1_id` | `UUID` | No | — | Clave foránea al equipo local (`teams.id`). |
| `team2_id` | `UUID` | No | — | Clave foránea al equipo visitante (`teams.id`). |
| `discord_channel_id`| `BIGINT` | Sí | `NULL` | Snowflake ID del canal de Discord creado para el partido. |
| `scheduled_at` | `TIMESTAMP WITH TIME ZONE` | Sí | `NULL` | Fecha y hora pactada para el inicio del partido. |
| `status` | `ENUM('PENDIENTE', ...)` | No | `'PENDIENTE'` | Estado operativo del partido. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | No | `now()` | Marca de tiempo UTC de creación del partido. |

#### Restricciones e Índices en `matches`
- **`pk_matches`**: Clave primaria sobre `id`.
- **`fk_matches_team1_id`**: Clave foránea referenciando `teams(id)` con `ON DELETE CASCADE`.
- **`fk_matches_team2_id`**: Clave foránea referenciando `teams(id)` con `ON DELETE CASCADE`.
- **`uq_matches_discord_channel_id`**: Restricción única sobre `discord_channel_id`. Ningún canal de Discord puede estar vinculado a más de un partido.
- **`uq_matches_jornada_teams`**: Restricción única compuesta sobre `(jornada, team1_id, team2_id)`. Impide duplicar el mismo enfrentamiento dentro de una misma jornada.
- **`ck_matches_distinct_teams`**: `CHECK (team1_id != team2_id)`. Impide registrar un partido donde un equipo juegue contra sí mismo.
- **`ix_matches_jornada`**: Índice para acelerar consultas por número de jornada.
- **`ix_matches_status`**: Índice para filtrar partidos pendientes de canal o finalizados.

---

### 3.4 Tabla `ticket_notices`

Registra el estado de auditoría y alertas de inactividad de los canales de tickets de soporte técnico, fichajes y administración.

| Columna | Tipo de Dato | Nulable | Valor por Defecto | Descripción |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `UUID` | No | `uuid.uuid4()` | Clave primaria única. |
| `discord_channel_id`| `BIGINT` | No | — | Snowflake ID del canal de ticket en Discord. |
| `category_name` | `VARCHAR(100)` | Sí | `NULL` | Nombre de la categoría de Discord en la que reside el ticket. |
| `last_staff_message_at`| `TIMESTAMP WITH TIME ZONE` | Sí | `NULL` | Marca de tiempo del último mensaje enviado por un miembro del Staff. |
| `last_alert_sent_at` | `TIMESTAMP WITH TIME ZONE` | Sí | `NULL` | Marca de tiempo del último aviso de inactividad emitido por el bot. |
| `is_pending_staff` | `BOOLEAN` | No | `FALSE` | Indica si el ticket requiere respuesta prioritaria por parte del Staff. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | No | `now()` | Marca de tiempo UTC de registro de la alerta. |

#### Restricciones e Índices en `ticket_notices`
- **`pk_ticket_notices`**: Clave primaria sobre `id`.
- **`uq_ticket_notices_discord_channel_id`**: Restricción única sobre `discord_channel_id`. Garantiza que cada canal de ticket mantenga un único registro de auditoría idempotente.

---

## 4. Guía de Migraciones con Alembic

El proyecto utiliza **Alembic** configurado para entornos asíncronos (`alembic/env.py`). Todas las operaciones se ejecutan mediante el CLI estándar con el entorno virtual gestionado por `uv`.

### Estructura de Alembic
```
alembic/
├── env.py                  # Script de configuración que conecta async_engine con Alembic
├── script.py.mako          # Plantilla para generación de nuevas revisiones
└── versions/               # Revisiones de migración versionadas
    └── 001_initial_schema.py
```

### Comandos de Operación

#### 1. Aplicar migraciones pendientes
Aplica todas las revisiones hasta la cabecera actual del repositorio:
```bash
uv run alembic upgrade head
```

#### 2. Consultar el historial y estado de migraciones
Muestra la lista de revisiones aplicadas y pendientes:
```bash
uv run alembic history --verbose
uv run alembic current
```

#### 3. Revertir una migración (*Rollback*)
Revierte la última revisión aplicada:
```bash
uv run alembic downgrade -1
```
O para revertir completamente la base de datos hasta el estado inicial vacío:
```bash
uv run alembic downgrade base
```

#### 4. Crear una nueva migración autogenerada
Cuando se modifique un modelo en `src/liga_bot/models/`, genera una migración comparando los modelos con la base de datos actual:
```bash
uv run alembic revision --autogenerate -m "añadir_campo_x_a_teams"
```

*Nota: Revisa siempre el script generado en `alembic/versions/` antes de aplicarlo para verificar restricciones, tipos ENUM nativos y claves foráneas.*

---

## 5. Configuración y Concurrencia de Motores Duales

LigaBot implementa una factoría de conexiones en `src/liga_bot/database.py` que soporta dos modos de operación transparentes:

### 5.1 Especificación de Cadenas de Conexión (`DATABASE_URL`)

| Motor | Formato de URL | Entorno Típico | Notas |
| :--- | :--- | :--- | :--- |
| **PGlite (En Memoria)** | `pglite:///:memory:` | Tests automatizados y CI | Efímero. Se destruye al finalizar el proceso. Ultrarrápido. |
| **PGlite (En Disco)** | `pglite:///./.data/pglite_db` | Desarrollo local sin Docker | Persiste los datos en una carpeta del sistema de archivos local. |
| **PostgreSQL (Asyncpg)**| `postgresql+asyncpg://user:pass@host:5432/dbname` | Producción y Staging | Concurrente, con pool de conexiones real y soporte multi-cliente. |

*Normalización automática: Si se introduce una URL con esquema `postgres://` o `postgresql://` sin el driver explícito, `Settings.async_database_url` la normaliza de inmediato a `postgresql+asyncpg://`.*

### 5.2 Manejo de Concurrencia y Serialización de Locks

Existe una diferencia arquitectónica fundamental entre un servidor PostgreSQL independiente y una base de datos embebida como PGlite:

1. **PostgreSQL en Producción**:
   - Utiliza `AsyncAdaptedQueuePool` con múltiples conexiones concurrentes.
   - Varias corrutinas de `asyncio` pueden abrir sesiones simultáneamente y realizar consultas o escrituras en paralelo, gestionadas por los mecanismos de aislamiento y control de concurrencia multiversión (MVCC) de PostgreSQL.
2. **PGlite y StaticPool (Desarrollo y Pruebas)**:
   - PGlite corre un único proceso nativo de PostgreSQL mediante Node.js/WASM y opera sobre una **única conexión subyacente compartida**.
   - Si dos corrutinas asíncronas intentan abrir transacciones simultáneas (`BEGIN`) sobre la misma conexión, el driver emite un error de colisión de estado transaccional.
   - **Solución implementada**: `database.py` incluye la función `_requires_serialization(engine)`. Si el motor es PGlite o utiliza `StaticPool`, el context manager `transactional_session` adquiere automáticamente un `asyncio.Lock` específico del motor (`_get_engine_lock(engine)`). Esto serializa el acceso a nivel de aplicación sin penalizar el rendimiento de las pruebas y garantizando transacciones 100% seguras.
3. **Cierre Limpio del Motor (`close_engine`)**:
   - En PostgreSQL, invoca `await engine.dispose()`.
   - En PGlite, localiza la instancia de `SQLAlchemyAsyncPGliteManager` asociada y ejecuta `await manager.stop()`, deteniendo de manera ordenada el subproceso Node.js subyacente y eliminando sockets temporales.
