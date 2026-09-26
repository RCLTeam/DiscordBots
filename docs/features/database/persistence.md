# Persistencia Base y Repositorio Genérico

[⬅️ Volver a Base de Datos](./README.md)

Este documento describe la capa de persistencia base implementada en `src/liga_bot/repositories/base.py`, el patrón `BaseRepository[ModelT]`, la protección defensiva contra errores transaccionales en PostgreSQL y las reglas de demarcación de transacciones en `DiscordBots`.

---

## 1. Patrón `BaseRepository[ModelT]`

La clase `BaseRepository` provee una implementación genérica y asíncrona de operaciones CRUD comunes sobre cualquier entidad declarativa de SQLAlchemy 2.0.

```python
ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    def __init__(self, session: AsyncSession, model_cls: type[ModelT]) -> None:
        self._session = session
        self._model_cls = model_cls
        mapper = inspect(model_cls)
        self._pk_columns = mapper.primary_key
        self._is_single_uuid_pk = len(self._pk_columns) == 1 and isinstance(
            self._pk_columns[0].type, Uuid
        )
```

### 1.1 Introspección Reflexiva Temprana en Constructor
Al instanciar un repositorio especializado (o el repositorio base), se realiza una inspección sobre el mapeador relacional del modelo (`inspect(model_cls)`):
- Extrae la tupla de columnas de clave primaria (`self._pk_columns`).
- Evalúa y almacena en el booleano `self._is_single_uuid_pk` si la clave primaria es atómica (longitud 1) y de tipo `sqlalchemy.Uuid`.

**Ventaja de Rendimiento:** Esta comprobación se ejecuta **una sola vez** durante la construcción del repositorio. Todas las consultas posteriores leen el valor booleano cacheado sin incurrir en coste de introspección ni reflexión en tiempo de ejecución.

### 1.2 Propiedades Públicas de Solo Lectura
- `session -> AsyncSession`: Devuelve la sesión asíncrona activa asociada al repositorio.
- `model_cls -> type[ModelT]`: Retorna la clase de entidad gestionada por el repositorio.

---

## 2. Recuperación Defensiva de Identificadores (`get_by_id`)

El método `get_by_id` implementa una salvaguarda transaccional crítica para evitar abortos de sesión en PostgreSQL:

```python
async def get_by_id(self, entity_id: UUID | int | str) -> ModelT | None:
    target_id: Any = entity_id
    if self._is_single_uuid_pk:
        if isinstance(entity_id, UUID):
            target_id = entity_id
        elif isinstance(entity_id, str):
            try:
                target_id = UUID(entity_id)
            except ValueError:
                return None
        else:
            return None
    elif isinstance(entity_id, str):
        try:
            target_id = UUID(entity_id)
        except ValueError:
            target_id = entity_id

    return await self._session.get(self._model_cls, target_id, populate_existing=True)
```

### 2.1 El Problema Transaccional en PostgreSQL
En PostgreSQL, cuando se emite una consulta SQL filtrando por una columna de tipo nativo `uuid` pasando una cadena con formato inválido (por ejemplo: `SELECT * FROM teams WHERE id = 'texto-invalido'`), el servidor de base de datos interrumpe la ejecución con un error de sintaxis:
```
DataError: invalid input syntax for type uuid: "texto-invalido"
```

Bajo el protocolo de transacciones de PostgreSQL, cualquier error DML/DQL aborta de inmediato la transacción activa:
```
InFailedSqlTransaction: current transaction is aborted, commands ignored until end of transaction block
```
A partir de ese instante, **cualquier consulta posterior en esa misma sesión fallará indefectiblemente**, forzando a emitir un `ROLLBACK` y descartando todo el trabajo acumulado.

### 2.2 Blindaje en Espacio Python
`BaseRepository.get_by_id` intercepta el identificador en memoria en Python antes de interactuar con la base de datos:
1. Si el modelo posee una clave primaria UUID única (`_is_single_uuid_pk is True`):
   - Si `entity_id` ya es una instancia de `uuid.UUID`, se utiliza directamente.
   - Si `entity_id` es un `str`, intenta parsearlo con `UUID(entity_id)`. Si se produce un `ValueError`, **retorna `None` inmediatamente sin emitir ninguna sentencia SQL**.
   - Si `entity_id` es de otro tipo incompatible (ej. entero para una entidad UUID), retorna `None`.
2. Si el modelo utiliza claves de otro tipo (ej. `Integer` o `BigInteger` en `RoleRequest` o `DiscordUser`), se mantiene la compatibilidad tipada.

**Resultado:** La transacción de base de datos permanece limpia y operativa para ejecutar consultas sucesivas sin abortos involuntarios.

### 2.3 Sincronización con `populate_existing=True`
Al invocar `session.get(..., populate_existing=True)`, SQLAlchemy actualiza los atributos de la instancia en memoria directamente desde la base de datos, ignorando posibles valores obsoletos en el Identity Map de la sesión y asegurando que el llamador reciba el estado más reciente de la fila.

---

## 3. Invariante de Frontera Transaccional

Para respetar el principio de responsabilidad única y el patrón Unit of Work, `BaseRepository` implementa una estricta directriz de aislamiento:

> **Regla de Persistencia:** Los métodos de repositorio NUNCA ejecutan `commit()`. La demarcación de transacciones atómicas (`commit` / `rollback`) recae de manera absoluta en el llamador a través de los context managers transaccionales.

### 3.1 Ciclo de Sincronización con `flush()` y `refresh()`
En operaciones de escritura (`create`, `update`, `delete`):
1. **`session.add(entity)` o mutación de atributos:** Se registra el cambio en el estado de la sesión.
2. **`await session.flush()`:** Envía las sentencias DML (`INSERT`, `UPDATE`, `DELETE`) pendientes a la base de datos dentro de la transacción activa. En este punto:
   - PostgreSQL evalúa restricciones relacionales (`FOREIGN KEY`, `UNIQUE`, `CHECK`).
   - Se disparan generadores de secuencias e identificadores por defecto a nivel de servidor (`server_default`).
   - Si se vulnera una restricción de integridad, `flush()` eleva la excepción de forma inmediata.
3. **`await session.refresh(entity)`:** Vuelve a hidratar la instancia en memoria con los valores finales asignados por PostgreSQL (ej. columnas calculadas o marcas de tiempo).

### 3.2 Ventajas para la Capa de Servicio
- **Atomicidad Multirrepositorio:** Un servicio puede orquestar operaciones sobre múltiples repositorios (`team_repo`, `membership_repo`, `audit_repo`) dentro de un único context manager `transactional_session`.
- Si cualquiera de los repositorios falla, la transacción completa ejecuta un `ROLLBACK` atómico sin dejar registros huérfanos ni inconsistencias parciales.

---

## 4. Contrato de la API CRUD

A continuación se detallan las firmas y comportamientos de los métodos públicos de `BaseRepository`:

### 4.1 Lectura
- **`get_by_id(entity_id: UUID | int | str) -> ModelT | None`**
  - Obtiene la entidad por clave primaria con validación sintáctica defensiva.
- **`list_all() -> Sequence[ModelT]`**
  - Ejecuta `select(self._model_cls)` y retorna todas las instancias registradas como una secuencia inmutable.
- **`count() -> int`**
  - Emite `select(func.count()).select_from(self._model_cls)` y retorna el total escalar de filas sin hidratar entidades en memoria.

### 4.2 Creación
- **`create(entity: ModelT | None = None, **kwargs: Any) -> ModelT`**
  - Persiste una nueva entidad en la base de datos.
  - Admite tanto una instancia ya instanciada (`repo.create(instance)`) como parámetros por palabra clave (`repo.create(**kwargs)`).
  - Ejecuta `flush()` y `refresh()`. Retorna la instancia sincronizada.

### 4.3 Actualización
- **`update(entity: ModelT, **kwargs: Any) -> ModelT`**
  - Actualiza de forma segura los atributos de una entidad existente.
  - Implementa filtrado con `hasattr(entity, key)`: los argumentos clave que no correspondan a columnas o propiedades reales del modelo son ignorados de forma segura sin provocar `AttributeError`.
  - Ejecuta `flush()` y `refresh()`.

### 4.4 Borrado
- **`delete(entity: ModelT) -> None`**
  - Marca la entidad para eliminación mediante `session.delete(entity)` y sincroniza con `flush()`.
- **`delete_by_id(entity_id: UUID | int | str) -> bool`**
  - Reutiliza `get_by_id` de forma segura.
  - Si la entidad no existe o el ID tiene sintaxis corrupta, retorna `False` de inmediato sin error de base de datos.
  - Si existe, ejecuta `delete(entity)` y retorna `True`.
