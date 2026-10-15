# Persistencia Relacional: Modelos SQLAlchemy y Repositorios de Plantilla

[⬅️ Volver a Gestión de Plantillas](./README.md)

Este documento describe la arquitectura de persistencia, esquemas relacionales, restricciones DDL y repositorios de datos que dan soporte al subsistema de gestión de plantillas.

- **Ubicación en código:** 
  - Modelos: `src/liga_bot/models/roster.py` y `src/liga_bot/models/enums.py`
  - Repositorios y Utilidades: `src/liga_bot/repositories/roster_repo.py`

---

## 1. Esquema Relacional y Restricciones DDL

El subsistema utiliza tres tablas principales en PostgreSQL / PGlite:

```
┌─────────────────────────────────┐       ┌─────────────────────────────────┐
│              teams              │       │          discord_users          │
│  id: UUID (PK)                  │       │  discord_id: String(32) (PK)    │
└───────────────┬─────────────────┘       └────────────────┬────────────────┘
                │                                          │
                │ 1:N (CASCADE)                            │ 1:N (CASCADE)
                ▼                                          ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                             team_memberships                              │
│  team_id: UUID (PK, FK)                                                   │
│  discord_user_id: String(32) (PK, FK)                                     │
│  season_name: String(120) (NOT NULL, FK compuesta a teams)                │
│  role: roster_role (Enum)                                                 │
│  is_captain: Boolean (Default: False)                                     │
│  created_at / updated_at: DateTime(tz=True)                               │
│                                                                           │
│  [FK] (team_id, season_name) -> teams(id, season_name) ON UPDATE/DELETE   │
│  [CHECK] is_captain = false OR role IN ('top','jungle','mid','adc','support')│
│  [UNIQUE] team_id WHERE (is_captain = true)                               │
│  [EXCLUDE GiST prod] (season_name =, discord_user_id =) WHERE competitivo │
└───────────────────────────────────────────────────────────────────────────┘
                │                                          │
                │ 1:N (CASCADE)                            │ 1:N (CASCADE)
                ▼                                          ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                             roster_movements                              │
│  id: UUID (PK)                                                            │
│  team_id: UUID (FK CASCADE)                                               │
│  discord_user_id: String(32) (FK CASCADE)                                 │
│  actor_id: String(32) (FK SET NULL)                                       │
│  action: roster_movement_action (Enum)                                    │
│  role: roster_role (Enum)                                                 │
│  created_at: DateTime(tz=True)                                            │
└───────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Tabla `team_memberships` (`src/liga_bot/models/roster.py:168-252`)

Modela la alineación y pertenencia actual de un jugador a un equipo.

- **Clave Primaria:** Compuesta por `(team_id, discord_user_id)`.
- **Columna de Temporada:** `season_name: String(120) NOT NULL`, sincronizada con la temporada del equipo (`teams.season_name`).
- **Claves Foráneas:**
  - `team_id` referencia `teams.id` con `ondelete="CASCADE"`.
  - `discord_user_id` referencia `discord_users.discord_id` con `ondelete="CASCADE"`.
  - `team_memberships_team_season_fkey`: clave foránea compuesta `(team_id, season_name) -> teams(id, season_name)` con `onupdate="CASCADE"` y `ondelete="CASCADE"`.
- **Restricción de Capitanía Titular (`L224–L227`):**
  ```sql
  CONSTRAINT team_memberships_captain_role_check
  CHECK (is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support'))
  ```
  Impide a nivel motor que cualquier jugador con rol de suplente (`substitute`), entrenador (`coach`), staff (`staff`) o socio (`partners`) sea marcado como capitán oficial.
- **Índice Parcial Único de Capitán (`L230–L235`):**
  ```sql
  CREATE UNIQUE INDEX team_memberships_unique_captain
  ON team_memberships (team_id)
  WHERE (is_captain = true);
  ```
  Garantiza a nivel de base de datos que ningún equipo pueda tener más de un capitán simultáneamente, previniendo condiciones de carrera concurrentes en la asignación de capitanía.
- **Restricción de Exclusión Competitiva por Temporada (PostgreSQL en producción):**
  ```sql
  CONSTRAINT one_competitive_team_per_player_and_season
  EXCLUDE USING gist ("season_name" WITH =, "discord_user_id" WITH =)
  WHERE ("role" IN ('top', 'jungle', 'mid', 'adc', 'support', 'substitute'))
  ```
  Garantiza en PostgreSQL (mediante `btree_gist`) que un jugador solo pueda ocupar una plaza competitiva por temporada (en PGlite se valida desde `RosterSyncService` y se verifica contra PostgreSQL real en `tests/test_postgres_competitive_season.py`).
- **Índices de Búsqueda:**
  - `team_memberships_team_id_idx` sobre `team_id`.
  - `team_memberships_discord_user_id_idx` sobre `discord_user_id`.

---

### 1.2 Tabla `roster_movements` (`src/liga_bot/models/roster.py:243-334`)

Registra el historial inmutable de movimientos deportivos (altas, bajas, promociones y cambios de rol).

- **Clave Primaria:** `id: UUID` (autogenerada con `uuid.uuid4`).
- **Claves Foráneas:**
  - `team_id` referencia `teams.id` con `ondelete="CASCADE"`.
  - `discord_user_id` referencia `discord_users.discord_id` con `ondelete="CASCADE"`.
  - `actor_id` referencia `discord_users.discord_id` con `ondelete="SET NULL"`.
- **Acciones Registradas (`RosterMovementAction`):**
  - `joined`: Incorporación al equipo.
  - `left`: Salida o baja del equipo.
  - `promoted_to_captain`: Designación como capitán.
  - `demoted_from_captain`: Retirada de capitanía.
  - `role_changed`: Modificación de posición de juego o función.
- **Índices de Trazabilidad:**
  - `roster_movements_team_id_idx`
  - `roster_movements_discord_user_id_idx`
  - `roster_movements_actor_id_idx`
  - `roster_movements_created_at_idx`

---

### 1.3 Tabla `audit_logs` (`src/liga_bot/models/roster.py:336-390`)

Registra las operaciones administrativas y cambios de estado con snapshots de datos estructurados en formato `JSONB`.

- **Clave Primaria:** `id: UUID`.
- **Columnas de Snapshot:**
  - `before`: Diccionario JSONB con el estado previo a la mutación.
  - `after`: Diccionario JSONB con el estado resultante.
- **Claves Foráneas:**
  - `actor_discord_user_id` referencia `discord_users.discord_id` con `ondelete="SET NULL"`.
- **Índices:**
  - `audit_logs_entity_idx` compuesto sobre `(entity_type, entity_id)`.
  - `audit_logs_actor_discord_user_id_idx` sobre `actor_discord_user_id`.
  - `audit_logs_created_at_idx` sobre `created_at`.

---

## 2. Utilidades de Serialización y Saneamiento

Para asegurar la interoperabilidad estricta entre Python y los motores de base de datos relacionales (PostgreSQL / PGlite), los repositorios de plantillas normalizan los identificadores con las funciones comunes de `src/liga_bot/utils/ids.py` y serializan los datos de auditoría con `_to_json_safe` (`src/liga_bot/repositories/roster_repo.py`):

### 2.1 Conversión Segura de UUID: `clean_uuid`
Convierte de forma defensiva argumentos a instancias `UUID`: acepta instancias de `UUID`, cadenas (recortando espacios) y objetos cuya representación `str` sea un UUID. Si el valor es inválido o malformado, devuelve `None` en lugar de dejar propagar un `ValueError` que abortaría la transacción activa.

### 2.2 Normalización de Snowflakes: `clean_user_id_str`
Normaliza identificadores de usuario de Discord a `str` eliminando espacios en blanco; devuelve `None` si el valor es `None` o queda vacío. No valida que el texto sea numérico. Las columnas `discord_user_id` de plantillas y auditoría son de texto; los casters usan `clean_user_id_int`, que devuelve un entero positivo.

### 2.3 Serialización Recursiva JSONB: `_to_json_safe` (`L30–L61`)
Normaliza estructuras de datos complejas para su almacenamiento seguro en columnas `JSONB`:
- `UUID` → `str(value)` (36 caracteres canónicos).
- `datetime` / `date` → formato ISO 8601 (`value.isoformat()`).
- `Enum` → valor escalar (`value.value`).
- `Decimal` → `float(value)`.
- `Mapping` (dict) → diccionario recursivo con claves `str`.
- `Sequence` / `Set` → listas recursivas.

---

## 3. Repositorios de Acceso a Datos

### 3.1 `TeamMembershipRepository` (`src/liga_bot/repositories/roster_repo.py:64-356`)

Hereda de `BaseRepository[TeamMembership]`:

- **`get(team_id, discord_user_id, with_team=False, with_user=False) -> TeamMembership | None`:**
  Busca por clave compuesta. Soporta `selectinload` condicional para `team` y `discord_user`. Retorna `None` si los identificadores son inválidos.
- **`list_by_user(discord_user_id, with_team=False) -> Sequence[TeamMembership]`:**
  Recupera todas las membresías del usuario ordenadas cronológicamente por `created_at.asc()`.
- **`list_by_team(team_id, with_user=False) -> Sequence[TeamMembership]`:**
  Recupera la plantilla completa de un club, ordenada prioritariamente por `is_captain.desc()`, luego `role.asc()` y `created_at.asc()`.
- **`get_competitive_membership(discord_user_id, with_team=False, with_user=False, season_name=None) -> TeamMembership | None`:**
  ```python
  competitive_roles = [r for r in RosterRole if r.is_competitive()]
  stmt = select(TeamMembership).where(
      TeamMembership.discord_user_id == clean_user_id,
      TeamMembership.role.in_(competitive_roles),
  )
  ```
  Con `season_name` acota la búsqueda a esa temporada, que es el alcance real de la invariante. Sin él ordena priorizando la temporada vigente y, a igualdad, `created_at.desc()` y `team_id.asc()`: sin ese orden la consulta podría devolver una plaza de una temporada anterior.
- **`create(team_id, discord_user_id, role, is_captain=False, season_name=None) -> TeamMembership`:**
  Inserta la membresía, ejecuta `await session.flush()` y `await session.refresh()`. Si no se indica `season_name`, la resuelve consultando el equipo (`_resolve_season_name`).
- **`update_role(team_id, discord_user_id, new_role, is_captain=False) -> TeamMembership`:**
  Actualiza los campos `role` e `is_captain`. Lanza `ValueError` si el registro no existe.
- **`delete(team_id, discord_user_id=None) -> bool`:**
  Elimina la membresía por clave compuesta o entidad. Retorna `True` si existía y fue eliminada.

- **`insert_if_not_exists(...)` y `upsert(...)`:** aceptan `season_name` opcional. Cuando no se indica, el valor viaja como subconsulta escalar sobre `teams` (`_season_name_value`), de modo que la columna `NOT NULL` se rellena sin un viaje extra a la base de datos. El `DO UPDATE` del `upsert` también la refresca.

**Temporada de los equipos (`TeamRepository`).** `teams.season_name` debe coincidir con la de su fila de `seasons_divisions` por clave foránea compuesta, así que el repositorio la resuelve **antes** del `flush`: `create` la toma de la división indicada y `update` normaliza `season_division`, `season_division_id` y `season_name` juntos, dando precedencia a los argumentos sobre la relación ya cargada (`lazy="joined"` conserva la división anterior).

**Temporada vigente (`repositories/season_repo.py`).** `seasons` no tiene ninguna columna que marque la temporada activa, así que se deduce de las fechas: vigente es la que cumple `starts_on <= hoy` y `ends_on` nulo o `>= hoy`; con varias manda la de `starts_on` más reciente y, si no hay ninguna vigente, la de `created_at` más reciente. Vive en un único módulo para que el repositorio de equipos y el de plantillas no discrepen.

---

### 3.2 `RosterMovementRepository` (`src/liga_bot/repositories/roster_repo.py:359-475`)

- **`record_movement(team_id, discord_user_id, action, role, actor_id=None) -> RosterMovement`:**
  Normaliza identificadores y enums, persiste el registro de movimiento con `flush()` y retorna la entidad creada.
- **`list_by_team(team_id, limit=None) -> Sequence[RosterMovement]`:**
  Consulta el historial del equipo ordenado descendente por `created_at.desc()`.
- **`list_by_user(discord_user_id, limit=None) -> Sequence[RosterMovement]`:**
  Consulta el historial del jugador ordenado descendente por `created_at.desc()`.

---

### 3.3 `AuditLogRepository` (`src/liga_bot/repositories/roster_repo.py:478-604`)

- **`log(action, entity_type, entity_id, actor_discord_user_id=None, before=None, after=None) -> AuditLog`:**
  Aplica `_to_json_safe` sobre los payloads `before` y `after`, crea el registro y lo persiste mediante `flush()`.
- **`list_by_entity(entity_type, entity_id, limit=None) -> Sequence[AuditLog]`:**
  Consulta bitácoras por entidad y UUID ordenadas descendente por fecha.
- **`list_by_actor(actor_discord_user_id, limit=None) -> Sequence[AuditLog]`:**
  Consulta bitácoras filtradas por el usuario que ejecutó la acción.
