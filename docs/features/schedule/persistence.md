# Persistencia y Modelo Relacional de Partidos (`Match` y `MatchRepository`)

[⬅️ Volver a Calendario y Partidos](./README.md)

El subsistema de persistencia para el calendario y seguimiento de partidos está implementado en `src/liga_bot/models/match.py` (modelo declarativo `Match`) y `src/liga_bot/repositories/match_repo.py` (repositorio de acceso a datos `MatchRepository`), apoyado en los enumerados de dominio de `src/liga_bot/models/enums.py`.

---

## 1. Modelo Declarativo `Match`

El modelo `Match` representa un enfrentamiento programado entre dos clubes deportivos durante una jornada específica.

- **Ubicación**: `src/liga_bot/models/match.py:29-84`.
- **Tabla**: `matches`.
- **Mixins**:
  - `UUIDPrimaryKeyMixin`: Genera una clave primaria `id` de tipo UUID v4 (`Uuid`).
  - `TimestampMixin`: Gestiona automáticamente las marcas temporales `created_at` y `updated_at` en UTC (`DateTime(timezone=True)`).

### 1.1 Esquema de Columnas y Tipos Exactos

```python
class Match(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "matches"

    jornada: Mapped[int] = mapped_column(Integer, nullable=False)
    division: Mapped[Division] = mapped_column(
        Enum(Division, name="division", native_enum=True),
        nullable=False,
    )
    team1_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", name="fk_matches_team1_id", ondelete="CASCADE"),
        nullable=False,
    )
    team2_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", name="fk_matches_team2_id", ondelete="CASCADE"),
        nullable=False,
    )
    discord_channel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        unique=True,
        nullable=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="matchstatus", native_enum=True),
        default=MatchStatus.PENDIENTE,
        server_default="PENDIENTE",
        nullable=False,
    )
```

| Columna | Tipo SQLAlchemy | Tipo Python | Nulo | Por Defecto | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Uuid` | `uuid.UUID` | No | `uuid.uuid4()` | Clave primaria única del partido. |
| `jornada` | `Integer` | `int` | No | — | Número de jornada a la que pertenece el partido. |
| `division` | `Enum(Division, native_enum=True)` | `Division` | No | — | División competitiva (`PREMIER` o `ASCEND`). |
| `team1_id` | `Uuid` (FK `teams.id`) | `uuid.UUID` | No | — | Identificador del equipo local. Borrado en cascada (`CASCADE`). |
| `team2_id` | `Uuid` (FK `teams.id`) | `uuid.UUID` | No | — | Identificador del equipo visitante. Borrado en cascada (`CASCADE`). |
| `discord_channel_id` | `BigInteger` | `int \| None` | Sí | `None` | Snowflake de 64 bits del canal de Discord creado. Unicidad estricta para valores no nulos. |
| `scheduled_at` | `DateTime(timezone=True)` | `datetime \| None` | Sí | `None` | Fecha y hora oficial programada en UTC. |
| `status` | `Enum(MatchStatus, native_enum=True)` | `MatchStatus` | No | `PENDIENTE` | Estado del ciclo de vida operativo del enfrentamiento. |
| `created_at` | `DateTime(timezone=True)` | `datetime` | No | `now(UTC)` | Fecha de creación del registro. |
| `updated_at` | `DateTime(timezone=True)` | `datetime` | No | `now(UTC)` | Fecha de última modificación. |

### 1.2 Enumerados de Dominio Asociados

Definidos en `src/liga_bot/models/enums.py`:

- **`Division`** (`src/liga_bot/models/enums.py:6-10`):
  ```python
  class Division(str, enum.Enum):
      PREMIER = "PREMIER"
      ASCEND = "ASCEND"
  ```
- **`MatchStatus`** (`src/liga_bot/models/enums.py:13-19`):
  ```python
  class MatchStatus(str, enum.Enum):
      PENDIENTE = "PENDIENTE"
      CANAL_CREADO = "CANAL_CREADO"
      JUGADO = "JUGADO"
      CANCELADO = "CANCELADO"
  ```

### 1.3 Relaciones y Carga Eager Asíncrona

El modelo `Match` define dos relaciones bidireccionales con la entidad `Team`:

```python
team1: Mapped[Team] = relationship(
    "Team",
    foreign_keys=[team1_id],
    back_populates="home_matches",
    lazy="selectin",
)
team2: Mapped[Team] = relationship(
    "Team",
    foreign_keys=[team2_id],
    back_populates="away_matches",
    lazy="selectin",
)
```

La estrategia de carga `lazy="selectin"` ejecuta una consulta SQL secundaria por lotes mediante el operador `IN`, evitando la emisión de consultas síncronas bajo demanda y previniendo la excepción `MissingGreenlet` característica de SQLAlchemy en modo asíncrono.

### 1.4 Restricciones DDL e Índices

El modelo declara restricciones a nivel de tabla en `__table_args__` (`src/liga_bot/models/match.py:79-84`):

1. **`UniqueConstraint("jornada", "team1_id", "team2_id", name="uq_matches_jornada_teams")`**:  
   Garantiza que no puedan coexistir dos registros con el mismo enfrentamiento directo en una misma jornada.
2. **`CheckConstraint("team1_id != team2_id", name="ck_matches_distinct_teams")`**:  
   Garantiza a nivel de base de datos que un equipo no pueda ser programado contra sí mismo.
3. **`Index("ix_matches_jornada", "jornada")`**:  
   Optimiza las consultas de listado de partidos filtradas por jornada.
4. **`Index("ix_matches_status", "status")`**:  
   Optimiza las consultas de supervisión operativa y estados pendientes.

---

## 2. Repositorio de Acceso a Datos: `MatchRepository`

`MatchRepository` (`src/liga_bot/repositories/match_repo.py:16-144`) hereda de `BaseRepository[Match]` y expone métodos de consulta especializados.

### 2.1 Utilidad de Carga Eager (`_apply_eager_teams`)

Para optimizar lecturas donde se requieran los datos completos de los equipos sin generar consultas N+1:

```python
def _apply_eager_teams(self, stmt: Any, with_teams: bool) -> Any:
    if with_teams:
        return stmt.options(selectinload(Match.team1), selectinload(Match.team2))
    return stmt
```

### 2.2 Catálogo de Métodos de Consulta y Mutación

#### `get_by_id(match_id: UUID | str, with_teams: bool = False) -> Match | None`
Recupera un partido por su clave primaria. Si recibe un `str`, valida la sintaxis UUID; ante un valor inválido retorna `None` sin emitir consulta SQL ni propagar excepción. Si `with_teams=True`, aplica carga eager de ambos equipos.

#### `get_by_jornada_and_teams(jornada: int, team1_id: UUID | str, team2_id: UUID | str, exact_order: bool = False, with_teams: bool = False) -> Match | None`
Recupera el partido programado para una jornada entre dos equipos:
- **`exact_order=False` (por defecto)**: Realiza una comprobación simétrica bidireccional:
  ```python
  ((Match.team1_id == u1) & (Match.team2_id == u2)) | (
      (Match.team1_id == u2) & (Match.team2_id == u1)
  )
  ```
  Permite detectar si los dos equipos ya se enfrentan en esa jornada, independientemente de quién figure como local o visitante.
- **`exact_order=True`**: Aplica comprobación estricta respetando la localía `(Match.team1_id == u1) & (Match.team2_id == u2)`.

#### `list_by_jornada(jornada: int, division: Division | str | None = None, with_teams: bool = False) -> Sequence[Match]`
Obtiene todos los enfrentamientos de una jornada específica con ordenamiento determinista:
- **Filtro de división opcional**: Si se indica `division`, restringe la consulta (`Match.division == div_val`).
- **Criterio de ordenamiento**:
  ```python
  order_by(Match.scheduled_at.asc().nulls_last(), Match.created_at.asc())
  ```
  Sitúa primero los partidos con horario confirmado en orden cronológico ascendente, seguidos de los partidos sin horario definido (`nulls_last`), ordenados secundariamente por fecha de creación.

#### `list_by_status(status: MatchStatus | str, with_teams: bool = False) -> Sequence[Match]`
Obtiene todos los partidos filtrados por su estado operativo (`PENDIENTE`, `CANAL_CREADO`, `JUGADO`, `CANCELADO`), ordenados por jornada y fecha de creación:
```python
order_by(Match.jornada.asc(), Match.created_at.asc())
```

#### `get_by_channel_id(channel_id: int, with_teams: bool = False) -> Match | None`
Localiza un partido a partir del snowflake del canal de texto de Discord (`Match.discord_channel_id == channel_id`). Utilizado para identificar el contexto del partido cuando se interactúa dentro de su canal de coordinación.

#### `update_status(match_or_id: Match | UUID | str, status: MatchStatus | str) -> Match | None`
Actualiza el estado operativo del partido. Resuelve la entidad por ID si se proporciona un identificador, asigna `match.status = stat_val`, y ejecuta `await self._session.flush()` y `await self._session.refresh(match)` para reflejar el estado persistido.

#### `update_channel_id(match_or_id: Match | UUID | str, channel_id: int | None) -> Match | None`
Asocia o desvincula el ID del canal de Discord asociado al partido, ejecutando `flush()` y `refresh()`.

---

## 3. Aclaraciones Fácticas sobre Consultas por Rango de Fechas

Se aclara que `MatchRepository`:
1. **NO implementa** métodos de filtrado por rango de fechas (tales como `list_by_date_range` o consultas entre fechas mínimas y máximas).
2. El atributo `scheduled_at` se emplea exclusivamente para registrar el horario pactado y como criterio de ordenación en `list_by_jornada`. Las consultas y agrupaciones operativas se efectúan siempre en torno al número de `jornada` y el estado operativo `status`.
