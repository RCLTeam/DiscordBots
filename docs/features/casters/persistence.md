# Persistencia y Modelo Relacional (`MatchCaster`, `MatchCasterCard` y Migración `0003`)

[⬅️ Volver a Cartelera y Casters](./README.md)

El subsistema de persistencia para la cartelera de casters está implementado en `src/liga_bot/models/caster.py` (113 líneas brutas, 93 SLOC), integrado con la entidad `Match` en `src/liga_bot/models/match.py` y versionado mediante la migración incremental de Alembic `alembic/versions/0003_match_casters.py` (148 líneas brutas, 122 SLOC).

---

## 1. Tipo Enumerado de Dominio: `CasterRole`

Definido en `src/liga_bot/models/caster.py:28-33`:

```python
class CasterRole(str, enum.Enum):
    """Rol de casteo y retransmisión para los partidos de la liga."""

    CASTER = "CASTER"
    STREAMER = "STREAMER"
    BOTH = "BOTH"
```

- **Mapeo en Base de Datos**: Respaldado por el tipo enumerado nativo PostgreSQL `caster_role`, creado en la migración `0003_match_casters.py`.
- **Semántica**:
  - `CASTER`: Participación exclusiva como narrador o analista en el canal de voz. No emite señal de vídeo.
  - `STREAMER`: Retransmisión técnica de la partida desde el cliente de League of Legends (Solo PC).
  - `BOTH`: Concurrencia de narración y emisión técnica desde el mismo equipo (Caster + PC).

---

## 2. Modelos Declarativos SQLAlchemy 2.0

### 2.1 Modelo `MatchCaster`

Representa la postulación o asignación de un usuario de Discord a un rol específico en un partido de la liga.

- **Ubicación**: `src/liga_bot/models/caster.py:36-76`.
- **Tabla**: `match_casters`.
- **Mixins**:
  - `UUIDPrimaryKeyMixin`: Proporciona la clave primaria `id` de tipo `uuid.UUID` (PostgreSQL `UUID`).
  - `TimestampMixin`: Gestiona automáticamente `created_at` y `updated_at` en UTC con zona horaria (`DateTime(timezone=True)`).

```python
class MatchCaster(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "match_casters"

    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    caster_role: Mapped[CasterRole] = mapped_column(
        Enum(CasterRole, name="caster_role", native_enum=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    match: Mapped[Match] = relationship(
        "Match",
        back_populates="casters",
    )
```

#### Columnas y Restricciones de `match_casters`

| Columna | Tipo SQLAlchemy | Tipo Python | Nulo | Por Defecto | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid.UUID` | No | `uuid.uuid4()` | Clave primaria única de la asignación. |
| `match_id` | `Uuid` (FK `matches.id`) | `uuid.UUID` | No | — | Identificador del partido. Borrado en cascada (`CASCADE`). |
| `discord_user_id` | `BigInteger` | `int` | No | — | Snowflake de 64 bits del usuario de Discord en la asignación. |
| `caster_role` | `Enum(CasterRole)` | `CasterRole` | No | — | Rol asignado (`CASTER`, `STREAMER` o `BOTH`). |
| `created_at` | `DateTime(timezone=True)` | `datetime` | No | `func.now()` | Marca temporal de creación en UTC. |
| `updated_at` | `DateTime(timezone=True)` | `datetime` | No | `func.now()` | Marca temporal de última modificación en UTC. |

#### Índices y Restricciones DDL (`__table_args__`)

1. **Unicidad de Usuario por Partido**:
   ```python
   UniqueConstraint("match_id", "discord_user_id", name="uq_match_casters_match_user")
   ```
   Garantiza que un usuario no pueda registrarse más de una vez en el mismo enfrentamiento.
2. **Índices de Consulta Rápida**:
   - `ix_match_casters_match_id` sobre `(match_id)`: acelera la carga de asignaciones al renderizar las tarjetas.
   - `ix_match_casters_discord_user_id` sobre `(discord_user_id)`: optimiza búsquedas de actividad por usuario.
3. **Índice Único Parcial para Exclusividad de Streamer**:
   ```python
   Index(
       "uq_match_casters_single_streamer",
       "match_id",
       unique=True,
       postgresql_where=text("caster_role IN ('STREAMER', 'BOTH')"),
   )
   ```
   **Garantía Física de Exclusividad**: Este índice único parcial en PostgreSQL impide físicamente que exista más de una fila con `match_id` duplicado donde el rol sea `STREAMER` o `BOTH`. Los intentos concurrentes de inserción son abortados a nivel de motor relacional con error `23505` (`unique_violation`).

---

### 2.2 Modelo `MatchCasterCard`

Registra las tarjetas publicadas en canales de Discord para garantizar la idempotencia en las ejecuciones de `/panel-casters`.

- **Ubicación**: `src/liga_bot/models/caster.py:78-114`.
- **Tabla**: `match_caster_cards`.
- **Mixins**: `UUIDPrimaryKeyMixin`, `TimestampMixin`.

```python
class MatchCasterCard(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "match_caster_cards"

    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    match: Mapped[Match] = relationship(
        "Match",
        back_populates="caster_cards",
    )
```

#### Columnas y Restricciones de `match_caster_cards`

| Columna | Tipo SQLAlchemy | Tipo Python | Nulo | Por Defecto | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid.UUID` | No | `uuid.uuid4()` | Clave primaria de la tarjeta registrada. |
| `match_id` | `Uuid` (FK `matches.id`) | `uuid.UUID` | No | — | Identificador del partido vinculado. Borrado en cascada (`CASCADE`). |
| `channel_id` | `BigInteger` | `int` | No | — | Snowflake de Discord del canal de texto de publicación. |
| `message_id` | `BigInteger` | `int` | No | — | Snowflake de Discord del mensaje que contiene el embed y la vista. |
| `created_at` | `DateTime(timezone=True)` | `datetime` | No | `func.now()` | Marca temporal de publicación inicial. |
| `updated_at` | `DateTime(timezone=True)` | `datetime` | No | `func.now()` | Marca temporal de última sincronización in-place. |

#### Restricciones DDL (`__table_args__`)

- **Unicidad de Tarjeta por Canal**:
  ```python
  UniqueConstraint("match_id", "channel_id", name="uq_match_caster_cards_match_channel")
  ```
  Asegura que un partido disponga a lo sumo de un único mensaje interactivo por canal de texto.
- **Índices**: `ix_match_caster_cards_match_id` e `ix_match_caster_cards_channel_id`.

---

## 3. Integración en el Modelo `Match`

En `src/liga_bot/models/match.py:115-129`, el modelo `Match` incorpora las relaciones inversas con precarga asíncrona y cascada completa:

```python
# Relaciones con casters y tarjetas de publicación
casters: Mapped[list[MatchCaster]] = relationship(
    "MatchCaster",
    back_populates="match",
    cascade="all, delete-orphan",
    passive_deletes=True,
    lazy="selectin",
)
caster_cards: Mapped[list[MatchCasterCard]] = relationship(
    "MatchCasterCard",
    back_populates="match",
    cascade="all, delete-orphan",
    passive_deletes=True,
    lazy="selectin",
)
```

- **`cascade="all, delete-orphan"`**: Si un partido es eliminado del calendario deportivo, SQLAlchemy propaga la eliminación a todas sus asignaciones de casters y registros de tarjetas asociadas.
- **`passive_deletes=True`**: Delega el borrado eficiente a la cláusula `ON DELETE CASCADE` de PostgreSQL a nivel de base de datos relacional.
- **`lazy="selectin"`**: Realiza la carga eager mediante una segunda consulta optimizada (`IN (...)`), evitando bloqueos y errores `DetachedInstanceError` en sesiones asíncronas.

---

## 4. Migración de Esquema Alembic: `0003_match_casters.py`

- **Ubicación**: `alembic/versions/0003_match_casters.py:1-149`.
- **Identificador de Revisión**: `revision = "0003"`.
- **Revisión Previa**: `down_revision = "0002"`.

### 4.1 Operaciones de Actualización (`upgrade`)

1. **Creación del Tipo ENUM**:  
   Detecta el dialecto en ejecución. En PostgreSQL crea de forma segura el tipo nativo `caster_role` con valores `'CASTER'`, `'STREAMER'`, `'BOTH'`:
   ```python
   bind = op.get_bind()
   if bind.dialect.name == "postgresql":
       caster_role_enum = postgresql.ENUM("CASTER", "STREAMER", "BOTH", name="caster_role")
       caster_role_enum.create(bind, checkfirst=True)
   ```
2. **Creación de la Tabla `match_casters`**:  
   Define columnas, clave foránea `fk_match_casters_match_id_matches` hacia `matches.id` con `ON DELETE CASCADE`, y restricción de unicidad `uq_match_casters_match_user`.
3. **Creación de Índices de `match_casters`**:  
   Crea los índices `ix_match_casters_match_id`, `ix_match_casters_discord_user_id` y el índice único parcial:
   ```python
   op.create_index(
       "uq_match_casters_single_streamer",
       "match_casters",
       ["match_id"],
       unique=True,
       postgresql_where=sa.text("caster_role IN ('STREAMER', 'BOTH')"),
   )
   ```
4. **Creación de la Tabla `match_caster_cards`**:  
   Define columnas para `match_id`, `channel_id`, `message_id`, clave foránea a `matches.id` con `ON DELETE CASCADE` y restricción de unicidad `uq_match_caster_cards_match_channel`.
5. **Creación de Índices de `match_caster_cards`**:  
   Crea `ix_match_caster_cards_match_id` e `ix_match_caster_cards_channel_id`.

### 4.2 Operaciones de Reversión (`downgrade`)

El procedimiento de reversión desmantela los objetos en orden estricto de dependencias inversas:
1. Elimina índices y tabla `match_caster_cards`.
2. Elimina el índice único parcial `uq_match_casters_single_streamer`.
3. Elimina índices y tabla `match_casters`.
4. Si el dialecto es PostgreSQL, destruye el tipo ENUM `caster_role` mediante `caster_role_enum.drop(bind, checkfirst=True)`.
