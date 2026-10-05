# Modelos Relacionales de Base de Datos

[⬅️ Volver a Base de Datos](./README.md)

Este documento detalla los 9 modelos relacionales declarativos implementados con SQLAlchemy 2.0 en `DiscordBots`. Los modelos definen tanto las entidades operativas del bot de Discord como las entidades compartidas con la plataforma central RCL-Next.

---

## 1. Arquitectura Declarativa y Mixins

Todos los modelos heredan de la clase base declarativa común definida en `src/liga_bot/models/base.py`, la cual estandariza convenciones de nombres, marcas temporales y claves primarias.

### 1.1 Convención Canónica de Nombres (`NAMING_CONVENTION`)

Para garantizar que todas las restricciones DDL e índices generados en PostgreSQL posean nombres deterministas compatibles entre Alembic y Drizzle ORM (RCL-Next), la base declarativa aplica el siguiente mapa de nomenclatura:

```python
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

### 1.2 Base Declarativa Asíncrona Segura (`Base`)

La clase `Base(DeclarativeBase)` implementa un método `__repr__` diseñado para evitar excepciones `MissingGreenlet`:

```python
class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        cols: list[str] = []
        for col in self.__table__.columns:
            if col.name in self.__dict__:
                cols.append(f"{col.name}={self.__dict__[col.name]!r}")
            else:
                cols.append(f"{col.name}=<unloaded>")
        return f"<{self.__class__.__name__}({', '.join(cols)})>"
```

- **Mecánica de Seguridad:** Inspecciona `self.__dict__` directamente. Si un atributo relacional o una columna diferida no ha sido cargada en memoria, escribe `<unloaded>` en lugar de invocar `getattr()`, evitando que SQLAlchemy intente emitir un `SELECT` síncrono fuera de un contexto greenlet.

### 1.3 Mixins Reutilizables

- **`UUIDPrimaryKeyMixin`:**
  - `id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)`
  - Utilizado en `Team`, `Match` y `TicketNotice`. Proporciona un identificador universal v4 generado en el cliente Python.
- **`TimestampMixin`:**
  - `created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)`
  - Registra la fecha y hora de inserción en zona horaria UTC delegada al servidor PostgreSQL (`func.now()`).
  - Solo define `created_at`: lo usan también tablas propias del bot (`ticket_notices`) que no tienen `updated_at`.
- **`UpdatedAtMixin`:**
  - `updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)`
  - Utilizado en `Team` y `Match`, cuyas tablas compartidas tienen `updated_at`. En producción la columna la mantiene el trigger `set_updated_at` de RCL-Next (`BEFORE UPDATE`), que sobrescribe el valor que envíe el bot; `onupdate` cubre las bases de datos locales y de tests, que no tienen ese trigger.

### 1.4 Alineación con las tablas compartidas

Las tablas compartidas las crea la migración `alembic/versions/0000_initial_shared_tables.py` en las bases de datos locales y de tests (en producción ya existen y la migración no hace nada), reproduciendo el esquema de RCL-Next. Los modelos describen esas tablas con las mismas columnas, tipos y nulabilidad: `tests/test_shared_schema_alignment.py` lo comprueba tabla a tabla por reflexión sobre la base de datos migrada. No compara valores por defecto ni nombres de índices o restricciones.

`Season` (`seasons`, en `src/liga_bot/models/roster.py`) declara `starts_on` y `ends_on` como `Date` (`Mapped[date | None]`), igual que la tabla.

---

## 2. Catálogo Detallado de los 9 Modelos Relacionales

### 2.1 `DiscordUser` (`discord_users`)
*Archivo fuente:* `src/liga_bot/models/roster.py`  
*Gobernanza:* Compartida con RCL-Next.

Representa la identidad base de un usuario de Discord autenticado en el ecosistema.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `discord_id` | `String(32)` | `varchar(32)` | No | PK | Snowflake de Discord como cadena. |
| `username` | `String(64)` | `varchar(64)` | No | — | Nombre de usuario en Discord. |
| `global_name` | `String(64)` | `varchar(64)` | Sí | `None` | Nombre global o apodo visible. |
| `avatar_hash` | `String(128)` | `varchar(128)` | Sí | `None` | Hash del avatar en CDN de Discord. |
| `role` | `Enum(AppRole)` | `app_role` | No | `AppRole.VIEWER` / `'viewer'` | Nivel de privilegio (`viewer`, `admin` u `owner`). |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` | Fecha de creación del registro. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`) | Fecha de última modificación. |

**Relaciones ORM:**
- `players: Mapped[list[Player]]`: Cuentas de League of Legends asociadas (`back_populates="discord_user"`, `passive_deletes=True`).
- `memberships: Mapped[list[TeamMembership]]`: Afiliaciones a equipos (`back_populates="discord_user"`, `cascade="all, delete-orphan"`, `passive_deletes=True`).
- `movements: Mapped[list[RosterMovement]]`: Historial de altas y bajas del usuario (`back_populates="discord_user"`, `passive_deletes=True`).
- `audit_logs: Mapped[list[AuditLog]]`: Acciones de auditoría ejecutadas por este usuario (`back_populates="actor"`, `passive_deletes=True`).

---

### 2.2 `Player` (`players`)
*Archivo fuente:* `src/liga_bot/models/roster.py`  
*Gobernanza:* Compartida con RCL-Next.

Representa una cuenta de invocador de League of Legends (Riot Account) vinculada a un usuario de Discord.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid` | No | `uuid.uuid4` / `gen_random_uuid()` | Clave primaria universal v4. |
| `discord_user_id` | `String(32)` | `varchar(32)` | Sí | `None` | FK a `discord_users.discord_id` (`ON DELETE SET NULL`). |
| `game_name` | `String(64)` | `varchar(64)` | No | — | Nombre de invocador Riot. |
| `riot_tag` | `String(16)` | `varchar(16)` | Sí | `None` | Tagline de Riot (ej. `EUW`, `001`). |
| `puuid` | `String(128)` | `varchar(128)` | Sí | `None` | Identificador universal único de Riot Games. |
| `country_code` | `String(2)` | `varchar(2)` | Sí | `None` | Código de país ISO 3166-1 alpha-2. |
| `is_main` | `Boolean` | `boolean` | No | `False` / `false` | Indica si es la cuenta principal del jugador. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` | Fecha de registro. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`) | Fecha de actualización. |

**Restricciones e Índices:**
- `UniqueConstraint("game_name", "riot_tag", name="players_game_name_riot_tag_key")`: Evita duplicar combinaciones de Riot ID.
- `Index("players_discord_user_id_idx", "discord_user_id")`: Optimiza la búsqueda de cuentas asociadas a un usuario.

**Relaciones ORM:**
- `discord_user: Mapped[DiscordUser | None]`: Usuario de Discord propietario de la cuenta (`back_populates="players"`).

---

### 2.3 `Team` (`teams`)
*Archivo fuente:* `src/liga_bot/models/team.py`  
*Gobernanza:* Compartida con RCL-Next (re-exportada en `roster.py`).

Representa un club o equipo participante en las competiciones de la liga.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid` | No | `uuid.uuid4` (PK Mixin) | Clave primaria universal v4. |
| `name` | `String(120)` | `varchar(120)` | No | — (Unique) | Nombre completo oficial del equipo. |
| `tag` (columna `short_name`) | `String(16)` | `varchar(16)` | No | — | Siglas o acrónimo del club. `slug` es una propiedad Python derivada de `tag`, no una columna. |
| `season_division_id` | `Uuid` | `uuid` | No | — | FK a `seasons_divisions.id` (`ON DELETE CASCADE`). La propiedad `Team.division` deduce de ella la división (`PREMIER` o `ASCEND`); ver [conversión del nombre](./enums.md#11-division). |
| `discord_role_id` | `BigInteger` | `bigint` | No | — (Unique) | Snowflake del rol de Discord representativo del equipo. |
| `logo_url` | `Text` | `text` | Sí | `None` | Logo del equipo. Lo gestiona la web; el bot no lo usa. |
| `color` | `String(7)` | `varchar(7)` | Sí | `None` | Color del equipo. Lo gestiona la web; el bot no lo usa. |
| `is_active` | `Boolean` | `boolean` | No | `True` / `true` | Equipo activo. Lo gestiona la web; el bot no lo usa. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (Timestamp Mixin) | Fecha de registro del club. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`, UpdatedAt Mixin) | Fecha de última modificación. |

**Restricciones e Índices:**
- `UniqueConstraint` sobre `name` y sobre `discord_role_id`.
- `Index("ix_teams_season_division_id", "season_division_id")`: Filtrado indexado por división de temporada.

**Relaciones ORM:**
- `home_matches: Mapped[list[Match]]`: Partidos en los que compite como local (`team1`), con `cascade="all, delete-orphan"`, `passive_deletes=True` y `lazy="selectin"`.
- `away_matches: Mapped[list[Match]]`: Partidos en los que compite como visitante (`team2`), con `cascade="all, delete-orphan"`, `passive_deletes=True` y `lazy="selectin"`.
- `memberships: Mapped[list[TeamMembership]]`: Plantilla activa de jugadores y cuerpo técnico (`back_populates="team"`, `cascade="all, delete-orphan"`, `passive_deletes=True`).
- `movements: Mapped[list[RosterMovement]]`: Historial cronológico de cambios de plantilla (`back_populates="team"`, `cascade="all, delete-orphan"`, `passive_deletes=True`).

---

### 2.4 `TeamMembership` (`team_memberships`)
*Archivo fuente:* `src/liga_bot/models/roster.py`  
*Gobernanza:* Compartida con RCL-Next.

Representa la asignación y posición activa de un usuario dentro de la plantilla de un equipo.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `team_id` | `Uuid` | `uuid` | No | PK Compuesta | FK a `teams.id` (`ON DELETE CASCADE`). |
| `discord_user_id` | `String(32)` | `varchar(32)` | No | PK Compuesta | FK a `discord_users.discord_id` (`ON DELETE CASCADE`). |
| `role` | `Enum(RosterRole)` | `roster_role` | No | — | Posición deportiva o rol técnico (`top`, `jungle`, etc.). |
| `is_captain` | `Boolean` | `boolean` | No | `False` / `false` | Indica si ostenta la capitanía oficial del club. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` | Fecha de incorporación a la plantilla. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`) | Fecha de último cambio de rol/estado. |

**Restricciones DDL e Índices:**
- `PrimaryKeyConstraint("team_id", "discord_user_id", name="team_memberships_pkey")`: Clave primaria compuesta.
- `CheckConstraint("is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support')", name="team_memberships_captain_role_check")`: Solo los 5 roles titulares pueden ser designados capitanes.
- `Index("team_memberships_unique_captain", "team_id", unique=True, postgresql_where=sa.text("is_captain = true"))`: Índice único parcial que prohíbe tener más de un capitán activo simultáneo por equipo.
- `Index("team_memberships_team_id_idx", "team_id")`: Optimización de consultas de plantilla por club.
- `Index("team_memberships_discord_user_id_idx", "discord_user_id")`: Optimización de consultas por usuario.

**Relaciones ORM:**
- `team: Mapped[Team]`: Entidad del equipo asociado (`back_populates="memberships"`).
- `discord_user: Mapped[DiscordUser]`: Entidad del usuario miembro (`back_populates="memberships"`).

---

### 2.5 `RosterMovement` (`roster_movements`)
*Archivo fuente:* `src/liga_bot/models/roster.py`  
*Gobernanza:* Compartida con RCL-Next.

Libro mayor inmutable (*append-only ledger*) que audita todas las modificaciones de plantilla en la liga.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid` | No | `uuid.uuid4` / `gen_random_uuid()` | Clave primaria universal v4. |
| `team_id` | `Uuid` | `uuid` | No | — | FK a `teams.id` (`ON DELETE CASCADE`). |
| `discord_user_id` | `String(32)` | `varchar(32)` | No | — | FK a `discord_users.discord_id` (`ON DELETE CASCADE`). |
| `action` | `Enum(RosterMovementAction)` | `roster_movement_action` | No | — | Tipo de acción (`joined`, `left`, etc.). |
| `role` | `Enum(RosterRole)` | `roster_role` | Sí | `None` | Rol implicado en el movimiento. |
| `actor_id` | `String(32)` | `varchar(32)` | Sí | `None` | FK a `discord_users.discord_id` (`ON DELETE SET NULL`) del moderador/capitán ejecutor. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` | Marca temporal de la operación. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`) | Marca temporal de actualización. |

**Índices:**
- `Index("roster_movements_team_id_idx", "team_id")`
- `Index("roster_movements_discord_user_id_idx", "discord_user_id")`
- `Index("roster_movements_actor_id_idx", "actor_id")`
- `Index("roster_movements_created_at_idx", "created_at")`

**Relaciones ORM:**
- `team: Mapped[Team]`: Equipo sobre el que recayó el movimiento.
- `discord_user: Mapped[DiscordUser]`: Usuario sujeto del movimiento.
- `actor: Mapped[DiscordUser | None]`: Usuario que autorizó o ejecutó la acción.

---

### 2.6 `RoleRequest` (`role_requests`)
*Archivo fuente:* `src/liga_bot/models/role_request.py`  
*Gobernanza:* Propietaria de DiscordBots (Migración Alembic `002_role_requests.py`).

Gestiona las solicitudes de alta y vinculación de roles de equipo generadas por los usuarios en Discord.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Integer` | `integer` | No | PK Autoincrement | Identificador secuencial de la solicitud. |
| `user_id` | `BigInteger` | `bigint` | No | — | Snowflake de Discord del solicitante (64 bits). |
| `nombre_lol` | `String(100)` | `varchar(100)` | No | — | Nombre de invocador declarado. |
| `riot_tag` | `String(20)` | `varchar(20)` | No | — | Tagline de Riot declarado. |
| `equipo` | `String(100)` | `varchar(100)` | No | — | Nombre del club solicitado. |
| `canal_id` | `BigInteger` | `bigint` | Sí | `None` | Snowflake del canal temporal de tramitación. |
| `estado` | `Enum(RoleRequestStatus)` | `rolerequeststatus` | No | `PENDING` / `'PENDING'` | Estado (`PENDING`, `APPROVED`, `DENIED`). |
| `staff_id` | `BigInteger` | `bigint` | Sí | `None` | Snowflake del moderador que resolvió la petición. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` | Fecha y hora de solicitud. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`) | Fecha y hora de resolución. |

**Índices:**
- `Index("ix_role_requests_user_id", "user_id")`: Búsqueda rápida por solicitante.
- `Index("ix_role_requests_canal_id", "canal_id")`: Localización por canal de interacción.
- `Index("ix_role_requests_estado", "estado")`: Filtrado por estado de tramitación.

**Comportamiento de Inicialización:**
El constructor `__init__` aplica `kwargs.setdefault("estado", RoleRequestStatus.PENDING)` para garantizar que ninguna instancia pueda crearse en memoria sin estado inicial.

---

### 2.7 `TicketNotice` (`ticket_notices`)
*Archivo fuente:* `src/liga_bot/models/ticket_notice.py`  
*Gobernanza:* Propietaria de DiscordBots (Migración Alembic `001_initial_schema.py`).

Monitorea la actividad de los canales de soporte (tickets) para disparar avisos automáticos de inactividad.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid` | No | `uuid.uuid4` (PK Mixin) | Clave primaria universal v4. |
| `discord_channel_id` | `BigInteger` | `bigint` | No | — (Unique) | Snowflake de Discord del canal de ticket. |
| `category_name` | `String(100)` | `varchar(100)` | Sí | `None` | Nombre de la categoría o motivo del ticket. |
| `last_staff_message_at` | `DateTime(timezone=True)` | `timestamptz` | Sí | `None` | Marca temporal del último mensaje del staff. |
| `last_alert_sent_at` | `DateTime(timezone=True)` | `timestamptz` | Sí | `None` | Marca temporal de la última alerta emitida. |
| `is_pending_staff` | `Boolean` | `boolean` | No | `False` / `false` | Indica si el ticket aguarda respuesta del staff. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (Timestamp Mixin) | Fecha de apertura del ticket. |

---

### 2.8 `Match` (`matches`)
*Archivo fuente:* `src/liga_bot/models/match.py`  
*Gobernanza:* Compartida con RCL-Next (creada en local y en tests por `0000_initial_shared_tables.py`; `0002_match_round.py` solo añade `id_round` cuando falta).

Representa un enfrentamiento competitivo programado entre dos clubes dentro de una jornada.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid` | No | `uuid.uuid4` (PK Mixin) | Clave primaria universal v4. |
| `jornada` | `Integer` | `integer` | No | — | Número ordinal de la jornada competitiva. |
| `id_season_division` | `Uuid` | `uuid` | No | — | FK a `seasons_divisions.id` (`ON DELETE CASCADE`). La propiedad `Match.division` deduce de ella la división en la que se disputa el partido; ver [conversión del nombre](./enums.md#11-division). |
| `team1_id` | `Uuid` | `uuid` | No | — | FK a `teams.id` (`ON DELETE CASCADE`) del equipo local. |
| `team2_id` | `Uuid` | `uuid` | No | — | FK a `teams.id` (`ON DELETE CASCADE`) del visitante. |
| `discord_channel_id` | `BigInteger` | `bigint` | Sí | `None` (Unique) | Canal de texto privado asignado al partido. |
| `scheduled_at` | `DateTime(timezone=True)` | `timestamptz` | Sí | `None` | Fecha y hora programada de la partida. |
| `status` | `Enum(MatchStatus)` | `matchstatus` | No | `PENDIENTE` / `'PENDIENTE'` | Estado operativo (`PENDIENTE`, `CANAL_CREADO`, `JUGADO`, `CANCELADO`). |
| `stream_url` | `Text` | `text` | Sí | `None` | URL de la retransmisión grabada o VOD del partido. |
| `stream_url_live` | `String(255)` | `varchar(255)` | Sí | `None` | URL del directo o retransmisión en vivo del partido (Twitch, YouTube Live, etc.). |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (Timestamp Mixin) | Fecha de creación del emparejamiento. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`, UpdatedAt Mixin) | Fecha de última modificación; en producción la actualiza el trigger `set_updated_at` de RCL-Next. |

**Restricciones DDL e Índices:**
- `UniqueConstraint("jornada", "team1_id", "team2_id", name="uq_matches_jornada_teams")`: Impide duplicar el mismo enfrentamiento en una misma jornada.
- `CheckConstraint("team1_id != team2_id", name="ck_matches_distinct_teams")`: Valida a nivel de base de datos que un equipo no pueda enfrentarse a sí mismo.
- `Index("ix_matches_jornada", "jornada")`: Optimiza listados por fecha competitiva.
- `Index("ix_matches_status", "status")`: Acelera la búsqueda de partidos pendientes o con canales activos.

**Relaciones ORM:**
- `team1: Mapped[Team]`: Equipo local (`back_populates="home_matches"`, `lazy="selectin"`).
- `team2: Mapped[Team]`: Equipo visitante (`back_populates="away_matches"`, `lazy="selectin"`).

---

### 2.9 `AuditLog` (`audit_logs`)
*Archivo fuente:* `src/liga_bot/models/roster.py`  
*Gobernanza:* Compartida con RCL-Next.

Registro de auditoría transaccional para operaciones críticas y cambios de estado en toda la plataforma.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid` | No | `uuid.uuid4` / `gen_random_uuid()` | Clave primaria universal v4. |
| `actor_discord_user_id` | `String(32)` | `varchar(32)` | Sí | `None` | FK a `discord_users.discord_id` (`ON DELETE SET NULL`). |
| `action` | `String(120)` | `varchar(120)` | No | — | Identificador de la acción (ej. `team.create`, `roster.captain_promoted`). |
| `entity_type` | `String(64)` | `varchar(64)` | No | — | Tipo de entidad afectada (ej. `team`, `membership`). |
| `entity_id` | `Uuid` | `uuid` | Sí | `None` | UUID de la entidad mutada. |
| `before` | `JSONB` | `jsonb` | Sí | `None` | Instantánea del estado previo en formato JSON estructurado. |
| `after` | `JSONB` | `jsonb` | Sí | `None` | Instantánea del estado resultante en formato JSON estructurado. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` | Fecha y hora del registro. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`) | Fecha de modificación. |

**Índices:**
- `Index("audit_logs_entity_idx", "entity_type", "entity_id")`: Búsqueda histórica por entidad.
- `Index("audit_logs_actor_discord_user_id_idx", "actor_discord_user_id")`: Auditoría de acciones por moderador.

**Relaciones ORM:**
- `actor: Mapped[DiscordUser | None]`: Usuario de Discord que ejecutó la mutación (`back_populates="audit_logs"`).

---

## 3. Matriz Sintética de Claves y Restricciones

| Modelo | Tabla | Tipo Clave Primaria | Claves Foráneas | Restricciones Notables |
|---|---|---|---|---|
| `DiscordUser` | `discord_users` | `String(32)` (`discord_id`) | Ninguna | Snowflake canónico |
| `Player` | `players` | `Uuid` | `discord_users.discord_id` (SET NULL) | Unique (`game_name`, `riot_tag`) |
| `Team` | `teams` | `Uuid` | `seasons_divisions.id` (CASCADE) | Unique `name`, Unique `discord_role_id` |
| `TeamMembership` | `team_memberships` | Compuesta `(team_id, discord_user_id)` | `teams.id` (CASCADE), `discord_users.discord_id` (CASCADE) | Check capitanía para 5 titulares, Unique parcial (`is_captain = true`) |
| `RosterMovement` | `roster_movements` | `Uuid` | `teams.id` (CASCADE), `discord_users.discord_id` (CASCADE), `actor_id` (SET NULL) | Ledger append-only |
| `RoleRequest` | `role_requests` | `Integer` (Autoincrement) | Ninguna | Snowflakes en `user_id`, `canal_id`, `staff_id` |
| `TicketNotice` | `ticket_notices` | `Uuid` | Ninguna | Unique `discord_channel_id` |
| `Match` | `matches` | `Uuid` | `team1_id -> teams.id` (CASCADE), `team2_id -> teams.id` (CASCADE) | Check `team1_id != team2_id`, Unique (`jornada`, `team1_id`, `team2_id`), Unique `discord_channel_id` |
| `AuditLog` | `audit_logs` | `Uuid` | `actor_discord_user_id -> discord_users.discord_id` (SET NULL) | Snapshots `JSONB` (`before`, `after`) |
