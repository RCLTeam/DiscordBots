# Capa de Persistencia: Seguimiento de Tickets y Registro de Auditoría

Este documento define la capa de acceso a datos y persistencia relacional para el seguimiento de tickets y la auditoría de eventos administrativos en `DiscordBots`. Detalla los modelos declarativos de SQLAlchemy 2.0 y los repositorios asíncronos que implementan operaciones atómicas, protección contra tipos no serializables en PostgreSQL y consultas optimizadas mediante índices compuestos.

---

## 1. Modelo Declarativo: `TicketNotice`

El modelo `TicketNotice` almacena el estado temporal de los canales de tickets en Discord para controlar la frecuencia de alertas, registrar las respuestas del personal de soporte y señalar canales que requieren intervención.

- **Ubicación en código:** `src/liga_bot/models/ticket_notice.py:14-35`
- **Tabla:** `ticket_notices`
- **Mixins heredados:** `UUIDPrimaryKeyMixin`, `TimestampMixin` (`src/liga_bot/models/base.py`)

### 1.1 Esquema de Columnas

| Columna | Tipo SQLAlchemy | Nulo | Por Defecto / Generación | Descripción |
|---|---|:---:|---|---|
| `id` | `UUID` (PostgreSQL / Uuid) | No | `uuid.uuid4()`, `server_default=func.gen_random_uuid()` | Clave primaria única generada mediante UUID v4. |
| `discord_channel_id` | `BigInteger` | No | — | Snowflake ID numérico de 64 bits del canal de texto en Discord (`unique=True`). |
| `category_name` | `String(100)` | Sí | `None` | Nombre de la categoría de Discord a la que pertenece el canal en el momento de la auditoría. |
| `last_staff_message_at`| `DateTime(timezone=True)` | Sí | `None` | Marca de tiempo UTC del último mensaje enviado por un miembro del Staff en este ticket. |
| `last_alert_sent_at` | `DateTime(timezone=True)` | Sí | `None` | Marca de tiempo UTC de la última alerta automática despachada por el bot. |
| `is_pending_staff` | `Boolean` | No | `default=False`, `server_default=sa.text("false")` | Indicador booleano que señala si el ticket está desatendido y requiere respuesta del staff. |
| `created_at` | `DateTime(timezone=True)` | No | `server_default=func.now()` | Fecha y hora UTC de creación del registro. |
| `updated_at` | `DateTime(timezone=True)` | No | `server_default=func.now()`, `onupdate=func.now()` | Fecha y hora UTC de la última modificación del registro. |

---

## 2. Repositorio: `TicketNoticeRepository`

El repositorio `TicketNoticeRepository` gestiona el ciclo de vida de los avisos de inactividad encapsulando las consultas sobre sesiones asíncronas de SQLAlchemy 2.0.

- **Ubicación en código:** `src/liga_bot/repositories/ticket_repo.py:13-99`
- **Clase Base:** `BaseRepository[TicketNotice]` (`src/liga_bot/repositories/base.py`)
- **Alias Exportado:** `TicketRepository = TicketNoticeRepository` (`L98`)

### 2.1 Métodos del Repositorio

#### `get_by_channel_id(channel_id: int) -> TicketNotice | None` (`L19-23`)
Recupera la entidad asociada a un canal de Discord específico mediante una consulta directa sobre la columna indexada `discord_channel_id`.
```python
stmt = select(TicketNotice).where(TicketNotice.discord_channel_id == channel_id)
result = await self._session.execute(stmt)
return result.scalars().first()
```

#### `record_alert(...) -> TicketNotice` (`L25-58`) — *Patrón Upsert Idempotente*
Registra el envío de una alerta asegurando que las marcas temporales tengan zona horaria UTC explícita (`timezone.utc`).
1. Comprueba si ya existe un registro para `channel_id`.
2. **Si existe:** Actualiza `last_alert_sent_at = now`, actualiza `category_name` (si se suministra) y marca `is_pending_staff = is_pending_staff` (por defecto `True`).
3. **Si no existe:** Instancia una nueva entidad `TicketNotice` con los valores provistos y la añade a la sesión (`self._session.add(notice)`).
4. Ejecuta `await self._session.flush()` y `await self._session.refresh(notice)` para sincronizar los identificadores generados por la base de datos sin comprometer la transacción externa prematuramente.

#### `record_staff_response(...) -> TicketNotice | None` (`L60-76`)
Registra que un miembro del Staff ha respondido en el ticket:
1. Localiza el registro mediante `get_by_channel_id(channel_id)`. Si no existe, retorna `None`.
2. Asigna `last_staff_message_at = now` (normalizado a UTC).
3. Resetea `is_pending_staff = False`.
4. Ejecuta `flush()` y `refresh(notice)`.

#### `list_pending_staff() -> Sequence[TicketNotice]` (`L78-86`)
Devuelve la lista de todos los tickets con intervención pendiente (`is_pending_staff == True`), ordenados de forma descendente por la fecha de la última alerta enviada (`order_by(TicketNotice.last_alert_sent_at.desc().nulls_last())`).

#### `delete_by_channel_id(channel_id: int) -> bool` (`L88-94`)
Elimina el registro de seguimiento cuando un canal de ticket es cerrado o eliminado en Discord, manteniendo la tabla libre de registros residuales:
```python
notice = await self.get_by_channel_id(channel_id)
if notice is None:
    return False
await self.delete(notice)
return True
```

---

## 3. Modelo Declarativo: `AuditLog`

El modelo `AuditLog` proporciona una pista de auditoría inmutable y transaccional para cambios críticos en plantillas de equipos, roles y acciones administrativas.

- **Ubicación en código:** `src/liga_bot/models/roster.py:336-390`
- **Tabla:** `audit_logs`

### 3.1 Esquema de Columnas

| Columna | Tipo SQLAlchemy | Nulo | Restricciones / Claves Foráneas | Descripción |
|---|---|:---:|---|---|
| `id` | `Uuid` | No | Clave Primaria (`primary_key=True`), default `uuid.uuid4`, `server_default=func.gen_random_uuid()` | Identificador universal único de la entrada de auditoría. |
| `actor_discord_user_id` | `String(32)` | Sí | `ForeignKey("discord_users.discord_id", ondelete="SET NULL")` | Snowflake ID del usuario que ejecutó la acción. Si el usuario es purgado, se fija en `NULL`. |
| `action` | `String(120)` | No | — | Identificador del evento o acción realizada (ej. `"roster.role_changed"`, `"tickets.alert"`). |
| `entity_type` | `String(64)` | No | — | Tipo o dominio de la entidad afectada (ej. `"team_membership"`, `"team"`, `"ticket"`). |
| `entity_id` | `Uuid` | Sí | — | UUID de la entidad modificada (nulo si la entidad utiliza clave primaria compuesta). |
| `before` | `JSONB` | Sí | — | Estado anterior del objeto en formato JSON binario estructurado. |
| `after` | `JSONB` | Sí | — | Estado posterior del objeto tras la mutación en formato JSONB. |
| `created_at` | `DateTime(timezone=True)` | No | `server_default=func.now()` | Marca temporal inmutable del momento del evento. |
| `updated_at` | `DateTime(timezone=True)` | No | `server_default=func.now()`, `onupdate=func.now()` | Marca temporal de actualización. |

### 3.2 Índices y Relaciones Relacionales

- **Índice Compuesto de Entidad (`audit_logs_entity_idx`, L381):**
  Acelera las consultas históricas sobre una entidad específica filtrando por `(entity_type, entity_id)`.
- **Índice de Actor (`audit_logs_actor_discord_user_id_idx`, L382):**
  Optimiza la búsqueda de todas las acciones ejecutadas por un moderador o usuario determinado sobre `actor_discord_user_id`.
- **Relación Relacional (`L374-378`):**
  Vincula con `DiscordUser` a través de la clave foránea `actor_discord_user_id`, con `back_populates="audit_logs"`.

---

## 4. Repositorio: `AuditLogRepository` y Serialización Segura

El repositorio `AuditLogRepository` gestiona la inserción y consulta de registros de auditoría garantizando que los datos complejos sean compatibles con el tipo de datos `JSONB` de PostgreSQL.

- **Ubicación en código:** `src/liga_bot/repositories/roster_repo.py:416-537`
- **Clase Base:** `BaseRepository[AuditLog]`

### 4.1 Normalización Recursiva `_to_json_safe` (`src/liga_bot/repositories/roster_repo.py:50-81`)

El motor de persistencia interactúa con PostgreSQL y motores de prueba como PGlite, los cuales rechazan tipos nativos de Python no serializables en columnas JSONB (como instancias de `UUID`, `datetime`, `Decimal` o `Enum`). La función auxiliar `_to_json_safe` aplica una transformación recursiva determinista:

```python
def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, Sequence)) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_to_json_safe(v) for v in value]
    return str(value)
```

### 4.2 Métodos de `AuditLogRepository`

#### `log(...) -> AuditLog` (`L425-478`)
Registra una mutación transaccional:
1. Sanitiza el identificador del usuario mediante `_clean_user_id(actor_discord_user_id)`.
2. Valida `entity_id` con `_clean_uuid(entity_id)`. Si se suministra un identificador no convertible a UUID válido, lanza `ValueError("entity_id must be a valid UUID...")`.
3. Procesa los diccionarios `before` y `after` a través de `_to_json_safe`.
4. Instancia `AuditLog`, lo agrega a la sesión y ejecuta `flush()` + `refresh(entry)`.

#### `list_by_entity(entity_type: str, entity_id: UUID | str, limit: int | None = None) -> Sequence[AuditLog]` (`L479-509`)
Recupera el historial de cambios de una entidad concreta ordenado cronológicamente de forma descendente (`order_by(AuditLog.created_at.desc())`). Si `entity_id` no es un UUID válido, la función intercepta el valor y devuelve una lista vacía (`[]`) de forma segura sin generar una excepción SQL. Aprovecha el índice `audit_logs_entity_idx`.

#### `list_by_actor(actor_discord_user_id: str | int, limit: int | None = None) -> Sequence[AuditLog]` (`L510-536`)
Recupera las acciones ejecutadas por un actor de Discord ordenadas de más reciente a más antigua. Si `actor_discord_user_id` es nulo o vacío tras la sanitización, retorna `[]` inmediatamente sin consultar a la base de datos. Aprovecha el índice `audit_logs_actor_discord_user_id_idx`.
