# Servicios de Dominio y Repositorios (`CasterService` y `CasterRepository`)

[⬅️ Volver a Cartelera y Casters](./README.md)

La capa de lógica de negocio y persistencia para la cartelera de casters está implementada en `src/liga_bot/services/caster_service.py` (310 líneas brutas, 266 SLOC) mediante la clase `CasterService`, y en `src/liga_bot/repositories/caster_repo.py` (278 líneas brutas, 250 SLOC) mediante `CasterRepository`.

Esta arquitectura desacopla estrictamente las operaciones de Discord (embeds, vistas, interacciones) de las reglas de negocio de la liga, la validación de exclusividad de streamer y las transacciones relacionales en PostgreSQL.

---

## 1. Modelos de Transferencia de Datos (DTOs)

Para comunicar el estado de cobertura y los resultados de las operaciones sin acoplar capas a entidades de ORM vivas fuera de la sesión transaccional, el módulo define dos estructuras de datos inmutables con `slots=True`:

```python
@dataclass(slots=True)
class MatchCastersData:
    """Estructura de datos con el estado de casteo y retransmisión de un partido."""

    has_streamer: bool
    streamer: MatchCaster | None
    casters: list[MatchCaster] = field(default_factory=list)


@dataclass(slots=True)
class CasterAssignmentResult:
    """Resultado de una operación de asignación o desasignación de casters."""

    success: bool
    action: str  # "assign" o "remove"
    data: MatchCastersData | None = None
    error: str | None = None
```

---

## 2. Servicio de Dominio: `CasterService`

- **Ubicación**: `src/liga_bot/services/caster_service.py:44-311`.
- **Inyección de Dependencias**:
  ```python
  def __init__(
      self,
      session_factory: async_sessionmaker[AsyncSession] | None = None,
      settings: Settings | None = None,
      bot: Any | None = None,
  ) -> None:
      self.session_factory = session_factory or get_session_factory()
      self.settings = settings or get_settings()
      self.bot = bot
  ```

### 2.1 Construcción de la Proyección de Cobertura (`_build_casters_data`)

A partir de las asignaciones almacenadas en `match_casters` para un partido, el servicio clasifica los roles y extrae la proyección:

```python
async def _build_casters_data(self, repo: CasterRepository, match_id: UUID) -> MatchCastersData:
    assignments = await repo.get_match_assignments(match_id)
    streamer = next(
        (c for c in assignments if c.caster_role in (CasterRole.STREAMER, CasterRole.BOTH)),
        None,
    )
    has_streamer = streamer is not None
    casters = [c for c in assignments if c.caster_role in (CasterRole.CASTER, CasterRole.BOTH)]
    return MatchCastersData(
        has_streamer=has_streamer,
        streamer=streamer,
        casters=casters,
    )
```

- **Semántica de `BOTH`**: Un usuario asignado a `BOTH` (`Ambas mezcladas`) satisface simultáneamente la presencia de streamer técnico (`has_streamer=True`) y se incluye en la lista de casters (`casters`).

---

### 2.2 Asignación Atómica con Verificación de Exclusividad: `assign_caster`

- **Firma**:
  ```python
  async def assign_caster(
      self, match_id: UUID | str, user_id: Any, role: CasterRole | str
  ) -> CasterAssignmentResult:
  ```

#### Flujo de Validación y Persistencia

1. **Sanitización de Identificadores**:  
   Limpia `match_id` con `_clean_uuid` y `user_id` con `_clean_user_id`. Si son inválidos, rechaza sin tocar la base de datos.
2. **Validación de Rol**:  
   Convierte el rol a `CasterRole`. Si el valor es ilegítimo, rechaza con `"Rol de casteo inválido: {role}"`.
3. **Verificación de Exclusividad en Memoria**:  
   Si el rol solicitado es `STREAMER` o `BOTH`:
   - Consulta `existing_streamer = await repo.get_streamer_assignment(clean_id)`.
   - Si ya existe un streamer y su `discord_user_id` no coincide con el solicitante (`clean_uid`): interrumpe la transacción y retorna `CasterAssignmentResult(success=False, error="Ya hay una persona asignada a la retransmisión de este partido.")`.
   - Si el streamer existente es el mismo usuario (por ejemplo, cambiando de `STREAMER` a `BOTH`), se permite la actualización sin error.
4. **Persistencia Upsert (`repo.assign`)**:  
   Inserta el registro o actualiza el rol si el usuario ya figuraba en otra modalidad para ese partido.
5. **Captura y Discriminación de Excepciones de Concurrencia (`IntegrityError`)**:  
   Si dos solicitudes concurrentes superan la validación en memoria simultáneamente, PostgreSQL resuelve la colisión mediante restricciones a nivel de base de datos. El servicio inspecciona el error (`pgcode` o nombre de restricción):
   - **Restricción `uq_match_casters_single_streamer` o código SQLState `23505` (`unique_violation`)**:  
     Retorna `CasterAssignmentResult(success=False, error="Ya hay una persona asignada a la retransmisión de este partido.")`.
   - **Clave foránea `fk_match_casters_match_id` o código SQLState `23503` (`foreign_key_violation`)**:  
     Retorna `CasterAssignmentResult(success=False, error="El partido especificado no existe.")`.
   - **Cualquier otro fallo de integridad**:  
     Retorna `CasterAssignmentResult(success=False, error="Error de integridad en la asignación.")`.

---

### 2.3 Desasignación Idempotente: `remove_caster`

- **Firma**:
  ```python
  async def remove_caster(
      self, match_id: UUID | str, user_id: Any
  ) -> CasterAssignmentResult:
  ```
- **Comportamiento**: Elimina la fila correspondiente a `(match_id, user_id)`. Si el usuario no estaba asignado, la operación se completa limpiamente sin lanzar excepciones. Retorna siempre el nuevo estado de cobertura (`MatchCastersData`), permitiendo a la interfaz reactivar los botones de stream si quien se retira era el streamer activo.

---

### 2.4 Consultas de Jornada y Partidos

- **`get_active_jornada() -> int | None`**:  
  Ejecuta `SELECT MAX(jornada) FROM matches` dentro de una sesión transaccional. Retorna `None` si la tabla `matches` no tiene registros.
- **`get_matches_for_jornada(jornada: int | None = None) -> Sequence[Match]`**:  
  Si `jornada` es `None`, invoca `get_active_jornada()`. Carga los partidos con precarga relacional obligatoria para evitar consultas N+1 en Discord:
  - `selectinload(Match.team1)`
  - `selectinload(Match.team2)`
  - `joinedload(Match.season_division)`
  - `selectinload(Match.casters)`  
  Ordena los resultados cronológicamente por `scheduled_at.asc().nulls_last(), created_at.asc()`.
- **`get_match(match_id: UUID | str) -> Match | None`**:  
  Recupera un partido individual con todas sus relaciones precargadas para la actualización de embeds tras cada interacción de botón.

---

### 2.5 Idempotencia de Tarjetas Publicadas

Para evitar la duplicación de mensajes en Discord cuando `/panel-casters` se invoca múltiples veces sobre una misma jornada o canal:

- **`record_card(match_id, channel_id, message_id) -> MatchCasterCard`**: Inserta o actualiza el registro en `match_caster_cards`.
- **`get_card(match_id, channel_id) -> MatchCasterCard | None`**: Comprueba si el partido ya dispone de tarjeta en el canal indicado.
- **`delete_card(match_id, channel_id) -> bool`**: Elimina el registro huérfano si el mensaje fue borrado en Discord.
- **`list_cards_for_channel(channel_id) -> Sequence[MatchCasterCard]`**: Lista todas las tarjetas publicadas en un canal de texto.
- **`get_unposted_matches(jornada, channel_id) -> Sequence[Match]`**: Devuelve exclusivamente los enfrentamientos de la jornada que aún no poseen una tarjeta registrada en el canal de destino.

---

## 3. Repositorio de Persistencia: `CasterRepository`

- **Ubicación**: `src/liga_bot/repositories/caster_repo.py:54-279`.
- **Herencia**: `BaseRepository[MatchCaster]`.
- **Sesión**: Asíncrona (`AsyncSession`) con propagación de transacciones atómicas.

### 3.1 Métodos de Acceso a Datos

| Método | Retorno | Operación SQL / Lógica |
|---|---|---|
| `get_match_assignments(match_id)` | `Sequence[MatchCaster]` | `SELECT ... WHERE match_id = :id ORDER BY created_at ASC` con `populate_existing=True`. |
| `get_user_assignment(match_id, user_id)` | `MatchCaster \| None` | `SELECT ... WHERE match_id = :id AND discord_user_id = :uid`. |
| `get_streamer_assignment(match_id)` | `MatchCaster \| None` | `SELECT ... WHERE match_id = :id AND caster_role IN ('STREAMER', 'BOTH')`. |
| `assign(match_id, user_id, role)` | `MatchCaster \| None` | PostgreSQL `INSERT ... ON CONFLICT (match_id, discord_user_id) DO UPDATE SET caster_role = :role, updated_at = now() RETURNING *`. |
| `remove(match_id, user_id)` | `bool` | `DELETE` del registro en la sesión si existe; retorna `True` si fue eliminado. |
| `record_card(match_id, channel_id, message_id)` | `MatchCasterCard` | PostgreSQL `INSERT ... ON CONFLICT (match_id, channel_id) DO UPDATE SET message_id = :msg_id, updated_at = now() RETURNING *`. |
| `get_card(match_id, channel_id)` | `MatchCasterCard \| None` | `SELECT ... WHERE match_id = :id AND channel_id = :cid`. |
| `delete_card(match_id, channel_id)` | `bool` | `DELETE` del registro de tarjeta si existe; retorna `True` si fue eliminado. |
| `list_cards_for_channel(channel_id)` | `Sequence[MatchCasterCard]` | `SELECT ... WHERE channel_id = :cid ORDER BY created_at ASC`. |
| `list_unposted_matches(jornada, channel_id)` | `Sequence[Match]` | `SELECT ... WHERE jornada = :j AND id NOT IN (SELECT match_id FROM match_caster_cards WHERE channel_id = :cid)`. |

### 3.2 Funciones Auxiliares de Sanitización Defensiva

Definidas en `src/liga_bot/repositories/caster_repo.py:20-52`:

- **`_clean_uuid(val)`**: Convierte de forma segura strings o instancias de `UUID`. Rechaza `None`, cadenas malformadas o tipos no compatibles retornando `None`.
- **`_clean_user_id(val)`**: Convierte identificadores de Discord a enteros estrictamente positivos (`> 0`). Descarta de inmediato valores booleanos (`isinstance(val, bool)`), números negativos, ceros y cadenas vacías o compuestas exclusivamente de espacios en blanco.

---

## 4. Invariantes de Negocio y Garantías Concurrenciales

1. **Exclusividad Estricta de Retransmisión**:  
   Solo una persona puede ocupar el rol de `STREAMER` o `BOTH` en un mismo partido. La asignación se bloquea tanto a nivel lógico en `CasterService.assign_caster` como a nivel físico por el índice único parcial `uq_match_casters_single_streamer`.
2. **Casteo Múltiple Ilimitado**:  
   Cualquier número de usuarios puede unirse al rol `CASTER` en un mismo enfrentamiento sin restricción de capacidad.
3. **Independencia Transversal entre Partidos (*Cross-Match Independence*)**:  
   La unicidad y exclusividad de roles aplica estrictamente al ámbito de cada partido individual (`match_id`). Un usuario puede ejercer como streamer en el Partido A y como caster en el Partido B de la misma jornada o semana sin interferencias.
4. **Resiliencia ante Mensajes Borrados en Discord**:  
   Si una tarjeta previamente registrada es eliminada de Discord, la siguiente invocación de `/panel-casters` purga el registro huérfano (`delete_card`) y regenera el mensaje y la tarjeta de forma transparente.
