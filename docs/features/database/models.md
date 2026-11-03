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

Las tablas compartidas las crea la migración `alembic/versions/0000_initial_shared_tables.py` en las bases de datos limpias locales y de tests (en producción ya existen y la migración las omite), reproduciendo el esquema de RCL-Next, mientras que `0002_match_round.py` y `0004_match_mvp_and_editor_role.py` aseguran de forma defensiva e idempotente `id_round`, `mvp_player_id`, `mvp_score`, sus restricciones/índice parcial y el valor `'editor'` en `app_role` sin sobrescribir los objetos ya creados por RCL-Next. Los modelos describen esas tablas con las mismas columnas, tipos y nulabilidad: `tests/test_shared_schema_alignment.py` comprueba tabla a tabla la paridad de columnas, tipos y nulabilidad por reflexión, las restricciones declaradas y migradas, y la coexistencia completa de migraciones `RCL-Next` (`MergeBranch`) -> `DiscordBots` sobre PGlite.

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
| `role` | `Enum(AppRole)` | `app_role` | No | `AppRole.VIEWER` / `'viewer'` | Nivel de privilegio (`viewer`, `editor`, `admin` u `owner`). |
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
| `season_name` | `String(120)` | `varchar(120)` | No | — | Temporada del equipo. Debe coincidir con la de su `seasons_divisions`: la clave foránea compuesta lo exige. Sin `DEFAULT` ni trigger, así que el bot la envía siempre. |
| `discord_role_id` | `BigInteger` | `bigint` | No | — (Unique) | Snowflake del rol de Discord representativo del equipo. |
| `logo_url` | `Text` | `text` | Sí | `None` | Logo del equipo. Lo gestiona la web; el bot no lo usa. |
| `color` | `String(7)` | `varchar(7)` | Sí | `None` | Color del equipo. Lo gestiona la web; el bot no lo usa. |
| `is_active` | `Boolean` | `boolean` | No | `True` / `true` | Equipo activo. Lo gestiona la web; el bot no lo usa. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (Timestamp Mixin) | Fecha de registro del club. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`, UpdatedAt Mixin) | Fecha de última modificación. |

**Restricciones e Índices:**
- `UniqueConstraint` sobre `name` y sobre `discord_role_id`.
- `UniqueConstraint("id", "season_name", name="teams_id_season_name_key")`: destino de la clave foránea compuesta de `team_memberships`.
- `ForeignKeyConstraint(["season_division_id", "season_name"], ["seasons_divisions.id", "seasons_divisions.season_name"], name="teams_season_division_season_fkey", onupdate="CASCADE", ondelete="CASCADE")`: ata el equipo a la temporada de su división. El `ON UPDATE CASCADE` propaga el renombrado de la temporada.
- `Index("ix_teams_season_division_id", "season_division_id")`: Filtrado indexado por división de temporada.

**Relaciones ORM (`src/liga_bot/models/team.py:66-86` y `src/liga_bot/models/roster.py:517-529`):**
- `season_division: Mapped[SeasonDivision]`: División de temporada del equipo (`lazy="joined"`, `foreign_keys=[season_division_id]`).
- `home_matches: Mapped[list[Match]]`: Partidos en los que compite como local (`team1`), con `foreign_keys="[Match.team1_id]"`, `back_populates="team1"`, `passive_deletes=True` y `lazy="select"`.
- `away_matches: Mapped[list[Match]]`: Partidos en los que compite como visitante (`team2`), con `foreign_keys="[Match.team2_id]"`, `back_populates="team2"`, `passive_deletes=True` y `lazy="select"`.
- `memberships: Mapped[list[TeamMembership]]`: Plantilla activa de jugadores y cuerpo técnico (`back_populates="team"`, `cascade="all, delete-orphan"`, `passive_deletes=True`, `foreign_keys="[TeamMembership.team_id]"`).
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
| `season_name` | `String(120)` | `varchar(120)` | No | — | Temporada de la membresía, igual a la de su equipo por clave foránea compuesta. Es la columna sobre la que se evalúa la exclusividad competitiva. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` | Fecha de incorporación a la plantilla. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`) | Fecha de último cambio de rol/estado. |

**Restricciones DDL e Índices:**
- `PrimaryKeyConstraint("team_id", "discord_user_id", name="team_memberships_pkey")`: Clave primaria compuesta.
- `CheckConstraint("is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support')", name="team_memberships_captain_role_check")`: Solo los 5 roles titulares pueden ser designados capitanes.
- `Index("team_memberships_unique_captain", "team_id", unique=True, postgresql_where=sa.text("is_captain = true"))`: Índice único parcial que prohíbe tener más de un capitán activo simultáneo por equipo.
- `Index("team_memberships_team_id_idx", "team_id")`: Optimización de consultas de plantilla por club.
- `Index("team_memberships_discord_user_id_idx", "discord_user_id")`: Optimización de consultas por usuario.
- `ForeignKeyConstraint(["team_id", "season_name"], ["teams.id", "teams.season_name"], name="team_memberships_team_season_fkey", onupdate="CASCADE", ondelete="CASCADE")`: la membresía hereda la temporada del equipo y no puede divergir de ella.
- **Solo en producción**, `one_competitive_team_per_player_and_season`: `EXCLUDE USING gist (season_name WITH =, discord_user_id WITH =) WHERE (role IN ('top', 'jungle', 'mid', 'adc', 'support', 'substitute'))`. Impone como máximo una posición competitiva por jugador y temporada. No se crea en la suite normal porque PGlite no empaqueta `btree_gist`; ver [límites del entorno](../../testing/environment-limits.md) y la [suite de PostgreSQL](../../testing/postgres-suite.md).

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
*Gobernanza:* Compartida con RCL-Next (creada en local y en tests por `0000_initial_shared_tables.py`; `0002_match_round.py` asegura `id_round` cuando falta y `0004_match_mvp_and_editor_role.py` asegura `mvp_player_id`, `mvp_score`, su clave foránea, restricciones `CHECK` e índice parcial).

Representa un enfrentamiento competitivo programado entre dos clubes dentro de una jornada.

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Default / Server Default | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid` | No | `uuid.uuid4` (PK Mixin) | Clave primaria universal v4. |
| `jornada` | `Integer` | `integer` | No | — | Número ordinal de la jornada competitiva. |
| `id_round` | `SmallInteger` | `smallint` | Sí | `None` | Identificador de jornada del lado web (`rounds.id`). Gobernada por RCL-Next; el bot solo la enlaza cuando existe. |
| `id_season_division` | `Uuid` | `uuid` | No | — | FK a `seasons_divisions.id` (`ON DELETE CASCADE`, `fk_matches_id_season_division`). La propiedad `Match.division` deduce de ella la división en la que se disputa el partido; ver [conversión del nombre](./enums.md#11-division). |
| `team1_id` | `Uuid` | `uuid` | No | — | FK a `teams.id` (`ON DELETE CASCADE`, `fk_matches_team1_id`) del equipo local. |
| `team2_id` | `Uuid` | `uuid` | No | — | FK a `teams.id` (`ON DELETE CASCADE`, `fk_matches_team2_id`) del visitante. |
| `discord_channel_id` | `BigInteger` | `bigint` | Sí | `None` (Unique) | Canal de texto privado asignado al partido. |
| `scheduled_at` | `DateTime(timezone=True)` | `timestamptz` | Sí | `None` | Fecha y hora programada de la partida. |
| `status` | `Enum(MatchStatus)` | `match_status` | No | `SCHEDULED` / `'scheduled'` | Estado operativo (`SCHEDULED`, `LIVE`, `COMPLETED`, `CANCELLED`, `FORFEIT`). |
| `stream_url` | `Text` | `text` | Sí | `None` | URL de la retransmisión grabada o VOD del partido. |
| `stream_url_live` | `String(255)` | `varchar(255)` | Sí | `None` | URL del directo o retransmisión en vivo del partido (Twitch, YouTube Live, etc.). |
| `mvp_player_id` | `Uuid` | `uuid` | Sí | `None` | FK a `players.id` (`ON DELETE SET NULL`, `matches_mvp_player_id_fkey`). Jugador MVP de la serie gestionado por RCL-Next. |
| `mvp_score` | `Numeric(5, 1)` | `numeric(5, 1)` | Sí | `None` | Puntuación del MVP de la serie gestionada por RCL-Next. |
| `created_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (Timestamp Mixin) | Fecha de creación del emparejamiento. |
| `updated_at` | `DateTime(timezone=True)` | `timestamptz` | No | `func.now()` (`onupdate=func.now()`, UpdatedAt Mixin) | Fecha de última modificación; en producción la actualiza el trigger `set_updated_at` de RCL-Next. |

**Restricciones DDL e Índices:**
- `ForeignKey("players.id", name="matches_mvp_player_id_fkey", ondelete="SET NULL")` (en `Match.mvp_player_id`, `src/liga_bot/models/match.py:103-111`, y `sa.ForeignKeyConstraint` en `0000_initial_shared_tables.py:578-583`): Vincula el jugador MVP de la serie con `players.id`; si el jugador se elimina, conserva el partido poniendo `mvp_player_id` a `NULL`.
- `UniqueConstraint("jornada", "team1_id", "team2_id", name="uq_matches_jornada_teams")`: Impide duplicar el mismo enfrentamiento en una misma jornada.
- `CheckConstraint("team1_id != team2_id", name="ck_matches_distinct_teams")`: Valida a nivel de base de datos que un equipo no pueda enfrentarse a sí mismo.
- `CheckConstraint("mvp_score IS NULL OR mvp_score >= 0", name=conv("matches_mvp_score_nonnegative_check"))`: Impide puntuaciones negativas de MVP.
- `CheckConstraint("mvp_player_id IS NULL OR mvp_score IS NOT NULL", name=conv("matches_mvp_player_score_check"))`: Exige que todo partido con `mvp_player_id` asignado tenga una `mvp_score` no nula.
- `Index("ix_matches_jornada", "jornada")`: Optimiza listados por fecha competitiva.
- `Index("ix_matches_status", "status")`: Acelera la búsqueda de partidos pendientes o con canales activos.
- `Index("ix_matches_id_season_division", "id_season_division")`: Optimiza el filtrado de partidos por división de temporada.
- `Index("matches_round_mvp_score_idx", "id_season_division", "id_round", mvp_score.desc().nulls_last(), postgresql_where=text("status = 'completed' AND mvp_player_id IS NOT NULL"))`: Índice parcial para rankings de MVP por división y jornada en partidos completados.

**Relaciones ORM (`src/liga_bot/models/match.py:117-153`):**
- `season_division: Mapped[SeasonDivision]`: División de temporada asociada (`foreign_keys=[id_season_division]`, `lazy="joined"`).
- `team1: Mapped[Team]`: Equipo local (`foreign_keys=[team1_id]`, `back_populates="home_matches"`, `lazy="selectin"`).
- `team2: Mapped[Team]`: Equipo visitante (`foreign_keys=[team2_id]`, `back_populates="away_matches"`, `lazy="selectin"`).
- `casters: Mapped[list[MatchCaster]]`: Asignaciones de casters y streamers del partido (`back_populates="match"`, `cascade="all, delete-orphan"`, `passive_deletes=True`, `lazy="selectin"`).
- `caster_cards: Mapped[list[MatchCasterCard]]`: Tarjetas publicadas en el panel de casters (`back_populates="match"`, `cascade="all, delete-orphan"`, `passive_deletes=True`, `lazy="selectin"`).

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
| `Team` | `teams` | `Uuid` | `seasons_divisions.id` (CASCADE), `(season_division_id, season_name)` → `seasons_divisions(id, season_name)` (CASCADE/CASCADE) | Unique `name`, Unique `discord_role_id`, Unique `(id, season_name)` (`teams_id_season_name_key`) |
| `TeamMembership` | `team_memberships` | Compuesta `(team_id, discord_user_id)` | `teams.id` (CASCADE), `discord_users.discord_id` (CASCADE), `(team_id, season_name)` → `teams(id, season_name)` (CASCADE/CASCADE) | Check capitanía para 5 titulares, Unique parcial (`is_captain = true`), exclusión competitiva por temporada en producción |
| `RosterMovement` | `roster_movements` | `Uuid` | `teams.id` (CASCADE), `discord_users.discord_id` (CASCADE), `actor_id` (SET NULL) | Ledger append-only |
| `RoleRequest` | `role_requests` | `Integer` (Autoincrement) | Ninguna | Snowflakes en `user_id`, `canal_id`, `staff_id` |
| `TicketNotice` | `ticket_notices` | `Uuid` | Ninguna | Unique `discord_channel_id` |
| `Match` | `matches` | `Uuid` | `id_season_division -> seasons_divisions.id` (CASCADE), `team1_id -> teams.id` (CASCADE), `team2_id -> teams.id` (CASCADE), `mvp_player_id -> players.id` (SET NULL) | Check `team1_id != team2_id`, Checks `matches_mvp_score_nonnegative_check` y `matches_mvp_player_score_check`, Unique (`jornada`, `team1_id`, `team2_id`), Unique `discord_channel_id`, Índice parcial `matches_round_mvp_score_idx` |
| `AuditLog` | `audit_logs` | `Uuid` | `actor_discord_user_id -> discord_users.discord_id` (SET NULL) | Snapshots `JSONB` (`before`, `after`) |
