# Persistencia y Modelo Relacional: RoleRequest

[⬅️ Volver a Roles e Incorporación](./README.md)

La capa de persistencia para el subsistema de solicitud y asignación de roles está estructurada mediante SQLAlchemy 2.0 asíncrono y migraciones gestionadas por Alembic. Comprende el modelo declarativo `RoleRequest` (`src/liga_bot/models/role_request.py`), la enumeración canónica `RoleRequestStatus` (`src/liga_bot/models/enums.py`) y el repositorio especializado `RoleRequestRepository` (`src/liga_bot/repositories/role_request_repo.py`).

---

## 1. Ciclo de Vida y Estados: `RoleRequestStatus`

El estado de cada solicitud se rige por la enumeración `RoleRequestStatus`, reflejada a nivel de motor de base de datos como un tipo `ENUM` nativo de PostgreSQL denominado `rolerequeststatus`.

- **Ubicación**: `src/liga_bot/models/enums.py:22-28`.
- **Definición de Clase**:
  ```python
  class RoleRequestStatus(str, enum.Enum):
      """Ciclo de vida y estado operativo de una solicitud de rol de equipo."""

      PENDING = "PENDING"
      APPROVED = "APPROVED"
      DENIED = "DENIED"
  ```

### Matriz de Transiciones de Estado

```
                 [ Solicitud Creada ]
                          │
                          ▼
                     ┌─────────┐
                     │ PENDING │
                     └────┬────┘
                          │
          ┌───────────────┴───────────────┐
          │ (Confirmación por Staff)      │ (Denegación por Staff)
          ▼                               ▼
    ┌──────────┐                    ┌──────────┐
    │ APPROVED │                    │  DENIED  │
    └──────────┘                    └──────────┘
```

> ⚠️ **NOTACIÓN CRÍTICA DE DOMINIO (`DENIED` vs `REJECTED`):**  
> El valor formal y canónico para el rechazo de una solicitud en el código fuente de la aplicación y en el catálogo DDL de PostgreSQL es **`DENIED`**.  
> El valor `REJECTED` no existe en la definición de la enumeración ni en la base de datos; su uso genera un error inmediato de coerción e integridad relacional (`LookupError` / `InvalidTextRepresentation`).

---

## 2. Modelo Declarativo: `RoleRequest`

La entidad `RoleRequest` hereda de la clase base declarativa `Base` (`src/liga_bot/models/base.py`) y mapea la tabla relacional `role_requests`.

- **Ubicación**: `src/liga_bot/models/role_request.py:14-70`.
- **Nombre de Tabla**: `role_requests`.

### 2.1 Esquema de Columnas

| Columna | Tipo SQLAlchemy | Tipo PostgreSQL | Nulo | Valor por Defecto | Descripción |
|---|---|---|:---:|---|---|
| `id` | `Integer` | `SERIAL` | No | Autoincremental | Clave primaria de la solicitud. |
| `user_id` | `BigInteger` | `BIGINT` | No | — | Snowflake de Discord de 64 bits del solicitante. |
| `nombre_lol` | `String(100)` | `VARCHAR(100)` | No | — | Nombre del jugador en League of Legends. |
| `riot_tag` | `String(20)` | `VARCHAR(20)` | No | — | Riot Tag del jugador. |
| `equipo` | `String(100)` | `VARCHAR(100)` | No | — | Nombre del equipo oficial o rol asignado. |
| `canal_id` | `BigInteger` | `BIGINT` | Sí | `NULL` | Snowflake de Discord de 64 bits del canal de ticket. Es `NULL` para asignaciones directas o de rol libre. |
| `estado` | `Enum(RoleRequestStatus)` | `rolerequeststatus` | No | `PENDING` | Estado actual dentro del ciclo de vida. |
| `staff_id` | `BigInteger` | `BIGINT` | Sí | `NULL` | Snowflake de Discord de 64 bits del miembro del staff que resolvió la solicitud. |
| `created_at` | `DateTime(timezone=True)` | `TIMESTAMPTZ` | No | `func.now()` | Marca temporal de creación en UTC. |
| `updated_at` | `DateTime(timezone=True)` | `TIMESTAMPTZ` | No | `func.now()` / `onupdate` | Marca temporal de última modificación en UTC. |

### 2.2 Índices B-Tree

Para optimizar las consultas frecuentes y garantizar respuestas de baja latencia en Discord, la tabla define tres índices explícitos:

```python
__table_args__ = (
    Index("ix_role_requests_user_id", "user_id"),
    Index("ix_role_requests_canal_id", "canal_id"),
    Index("ix_role_requests_estado", "estado"),
)
```

1. **`ix_role_requests_user_id`**: Optimiza la verificación de solicitudes pendientes activas de un miembro (`get_active_by_user`).
2. **`ix_role_requests_canal_id`**: Acelera la localización de la solicitud vinculada a un canal de ticket interactivo (`get_by_channel_id`).
3. **`ix_role_requests_estado`**: Permite auditorías y filtrados rápidos por estado de tramitación.

### 2.3 Resiliencia Asíncrona (`__repr__`)

Para evitar errores de greenlet de SQLAlchemy (`DetachedInstanceError` / `GreenletExit`) cuando se inspeccionan instancias fuera de la sesión asíncrona en registros de log o depuración, `RoleRequest` implementa un `__repr__` seguro que lee de `self.__dict__`:

```python
def __repr__(self) -> str:
    d = self.__dict__
    req_id = d.get("id", getattr(self, "id", None))
    user_id = d.get("user_id", "<unloaded>")
    nombre_lol = d.get("nombre_lol", "<unloaded>")
    riot_tag = d.get("riot_tag", "<unloaded>")
    equipo = d.get("equipo", "<unloaded>")
    estado = d.get("estado", "<unloaded>")
    return (
        f"<RoleRequest(id={req_id!r}, user_id={user_id!r}, "
        f"nombre_lol={nombre_lol!r}, riot_tag={riot_tag!r}, "
        f"equipo={equipo!r}, estado={estado!r})>"
    )
```

---

## 3. Repositorio de Acceso a Datos: `RoleRequestRepository`

Encapsula todas las operaciones de lectura y modificación de la tabla `role_requests`, heredando de `BaseRepository[RoleRequest]`.

- **Ubicación**: `src/liga_bot/repositories/role_request_repo.py:13-98`.

### Métodos del Repositorio

#### 3.1 `create_request`
Crea una solicitud persistida en la base de datos con estado inicial `RoleRequestStatus.PENDING`. Ejecuta `flush` y `refresh` dentro de la sesión activa para sincronizar los identificadores autoincrementales y las marcas temporales.

```python
async def create_request(
    self,
    user_id: int,
    nombre_lol: str,
    riot_tag: str,
    equipo: str,
    canal_id: int | None = None,
) -> RoleRequest:
    req = RoleRequest(
        user_id=user_id,
        nombre_lol=nombre_lol,
        riot_tag=riot_tag,
        equipo=equipo,
        canal_id=canal_id,
        estado=RoleRequestStatus.PENDING,
    )
    self._session.add(req)
    await self._session.flush()
    await self._session.refresh(req)
    return req
```

#### 3.2 `get_by_channel_id`
Recupera la solicitud más reciente asociada a un canal de texto de Discord.

```python
async def get_by_channel_id(self, canal_id: int) -> RoleRequest | None:
    stmt = (
        select(RoleRequest).where(RoleRequest.canal_id == canal_id).order_by(RoleRequest.id.desc())
    )
    result = await self._session.execute(stmt)
    return result.scalars().first()
```

#### 3.3 `get_active_by_user`
Busca si un miembro posee actualmente alguna solicitud abierta con estado `PENDING`. Si existe, se retorna para impedir solicitudes concurrentes duplicadas.

```python
async def get_active_by_user(self, user_id: int) -> RoleRequest | None:
    stmt = (
        select(RoleRequest)
        .where(
            RoleRequest.user_id == user_id,
            RoleRequest.estado == RoleRequestStatus.PENDING,
        )
        .order_by(RoleRequest.id.desc())
    )
    result = await self._session.execute(stmt)
    return result.scalars().first()
```

#### 3.4 `update_status`
Actualiza el estado de la solicitud (`APPROVED` o `DENIED`), vinculando opcionalmente el identificador del staff responsable. Incluye normalización defensiva si el estado es provisto como cadena de texto.

```python
async def update_status(
    self,
    request_id: int,
    estado: RoleRequestStatus,
    staff_id: int | None = None,
) -> RoleRequest | None:
    req = await self.get_by_id(request_id)
    if req is None:
        return None

    if isinstance(estado, str) and not isinstance(estado, RoleRequestStatus):
        estado = RoleRequestStatus(estado)

    req.estado = estado
    if staff_id is not None:
        req.staff_id = staff_id

    await self._session.flush()
    await self._session.refresh(req)
    return req
```

---

## 4. Migración Alembic: `002_role_requests.py`

- **Ubicación**: `alembic/versions/002_role_requests.py`.
- **Revisión**: `002` (depende de `001`).

### Operaciones de `upgrade()`
1. **Creación de Tipo Nativo**:
   ```python
   rolerequeststatus_enum = postgresql.ENUM("PENDING", "APPROVED", "DENIED", name="rolerequeststatus")
   rolerequeststatus_enum.create(op.get_bind(), checkfirst=True)
   ```
2. **Creación de Tabla**:
   - Crea `role_requests` con clave primaria `pk_role_requests`.
   - Utiliza `create_type=False` en la columna `estado` para referenciar el enum nativo creado en el paso anterior.
3. **Creación de Índices**:
   - Genera `ix_role_requests_user_id`, `ix_role_requests_canal_id` y `ix_role_requests_estado`.

### Operaciones de `downgrade()`
- Elimina los 3 índices B-Tree.
- Elimina la tabla `role_requests`.
- Elimina el tipo enum `rolerequeststatus` de PostgreSQL de forma idempotente con `checkfirst=True`.
