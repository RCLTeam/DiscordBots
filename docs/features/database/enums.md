# Tipos Enumerados del Dominio (Enums)

Este documento describe las 6 enumeraciones del dominio implementadas en `src/liga_bot/models/enums.py` para tipar estados operativos, categorías competitivas, roles de usuario y movimientos de plantilla en `DiscordBots`.

Todas las enumeraciones heredan de `(str, enum.Enum)`, lo que permite comparaciones directas por igualdad con literales de cadena, serialización transparente a JSON y compatibilidad estricta con los tipos ENUM nativos de PostgreSQL.

---

## 1. Catálogo de Enumeraciones

### 1.1 `Division`
Define las categorías competitivas oficiales en las que se divide la liga.

```python
class Division(str, enum.Enum):
    PREMIER = "PREMIER"
    ASCEND = "ASCEND"
```

- **Tipo PostgreSQL Subyacente:** `division` (creado en la migración inicial de Alembic).
- **Valores Permitidos:**
  - `PREMIER`: Primera división competitiva de mayor nivel.
  - `ASCEND`: División de ascenso y desarrollo de nuevos clubes.
- **Modelos que lo utilizan:** `Team.division`, `Match.division`.

---

### 1.2 `MatchStatus`
Gestiona el ciclo de vida operativo de un enfrentamiento entre dos clubes.

```python
class MatchStatus(str, enum.Enum):
    PENDIENTE = "PENDIENTE"
    CANAL_CREADO = "CANAL_CREADO"
    JUGADO = "JUGADO"
    CANCELADO = "CANCELADO"
```

- **Tipo PostgreSQL Subyacente:** `matchstatus`.
- **Transición de Estados:**
  1. `PENDIENTE`: Estado inicial al programar la jornada (`default=MatchStatus.PENDIENTE`, `server_default="PENDIENTE"`).
  2. `CANAL_CREADO`: Se ha generado el canal de texto privado en Discord para la coordinación arbitral y de capitanes.
  3. `JUGADO`: El partido se ha disputado y el resultado ha sido registrado formalmente.
  4. `CANCELADO`: El partido ha sido anulado por causas reglamentarias o incomparecencia.
- **Modelos que lo utilizan:** `Match.status`.

---

### 1.3 `RoleRequestStatus`
Modela el estado del flujo de aprobación para solicitudes de asignación de rol de equipo tramitadas desde Discord.

```python
class RoleRequestStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
```

- **Tipo PostgreSQL Subyacente:** `rolerequeststatus`.
- **Valores Permitidos:**
  - `PENDING`: La solicitud ha sido registrada por el usuario y espera revisión por parte del staff.
  - `APPROVED`: El staff ha verificado la identidad y asignado el rol en Discord.
  - `DENIED`: La solicitud ha sido rechazada (por datos incorrectos o no pertenencia al club).
- **Modelos que lo utilizan:** `RoleRequest.estado`.

---

### 1.4 `AppRole`
Define el nivel de autorización y privilegios de un usuario en el ecosistema (alineado de forma idéntica con el enum `app_role` en RCL-Next).

```python
class AppRole(str, enum.Enum):
    VIEWER = "viewer"
    ADMIN = "admin"
```

- **Tipo PostgreSQL Subyacente:** `app_role` (creado y gobernado por RCL-Next).
- **Valores Permitidos:**
  - `viewer`: Usuario estándar con permisos de lectura y visualización (`default=AppRole.VIEWER`, `server_default="viewer"`).
  - `admin`: Administrador de la plataforma con privilegios de gestión y moderación.
- **Modelos que lo utilizan:** `DiscordUser.role`.

---

### 1.5 `RosterRole`
Define la demarcación deportiva o cargo técnico de un miembro dentro de la plantilla de un equipo (alineado con `roster_role` en RCL-Next).

```python
class RosterRole(str, enum.Enum):
    TOP = "top"
    JUNGLE = "jungle"
    MID = "mid"
    ADC = "adc"
    SUPPORT = "support"
    SUBSTITUTE = "substitute"
    COACH = "coach"
    STAFF = "staff"
    PARTNERS = "partners"
```

- **Tipo PostgreSQL Subyacente:** `roster_role` (gobernado por RCL-Next).
- **Clasificación de Roles:**
  - **Titulares (5):** `TOP`, `JUNGLE`, `MID`, `ADC`, `SUPPORT`.
  - **Suplente Competitivo (1):** `SUBSTITUTE`.
  - **Cuerpo Técnico y Administrativo (3):** `COACH`, `STAFF`, `PARTNERS`.
- **Modelos que lo utilizan:** `TeamMembership.role`, `RosterMovement.role`.

#### Métodos de Dominio y Reglas Invariantes

La clase `RosterRole` encapsula métodos auxiliares para validar las reglas deportivas del reglamento:

1. **`is_competitive(self) -> bool`**
   ```python
   def is_competitive(self) -> bool:
       return self in (
           RosterRole.TOP,
           RosterRole.JUNGLE,
           RosterRole.MID,
           RosterRole.ADC,
           RosterRole.SUPPORT,
           RosterRole.SUBSTITUTE,
       )
   ```
   - **Regla Invariante #6:** Un usuario de Discord únicamente puede ostentar un rol competitivo (`top`, `jungle`, `mid`, `adc`, `support`, `substitute`) en **como máximo un equipo** en toda la competición.
   - En contraste, los roles no competitivos (`coach`, `staff`, `partners`) devuelven `False`, permitiendo que un mismo profesional ejerza labores técnicas o de patrocinio en múltiples clubes concurrentemente.

2. **`is_starter(self) -> bool`**
   ```python
   def is_starter(self) -> bool:
       return self in (
           RosterRole.TOP,
           RosterRole.JUNGLE,
           RosterRole.MID,
           RosterRole.ADC,
           RosterRole.SUPPORT,
       )
   ```
   - **Regla de Capitanía:** Solo las 5 posiciones titulares pueden recibir la designación de capitán (`is_captain = True`). Los suplentes y el cuerpo técnico devuelven `False`. Esta condición se valida en el código Python y está respaldada por la restricción DDL `team_memberships_captain_role_check` en PostgreSQL.

---

### 1.6 `RosterMovementAction`
Audita de forma tipada las operaciones y transiciones registradas en el historial de plantilla (`roster_movements`).

```python
class RosterMovementAction(str, enum.Enum):
    JOINED = "joined"
    LEFT = "left"
    PROMOTED_TO_CAPTAIN = "promoted_to_captain"
    DEMOTED_FROM_CAPTAIN = "demoted_from_captain"
    ROLE_CHANGED = "role_changed"
```

- **Tipo PostgreSQL Subyacente:** `roster_movement_action` (gobernado por RCL-Next).
- **Semántica de Operaciones:**
  - `joined`: Incorporación formal de un usuario a la plantilla del club.
  - `left`: Salida, baja o expulsión voluntaria/involuntaria de la plantilla.
  - `promoted_to_captain`: Nombramiento oficial de un titular como capitán del equipo.
  - `demoted_from_captain`: Revocación del estatus de capitán.
  - `role_changed`: Reasignación de posición deportiva (ej. de `substitute` a `mid`).
- **Modelos que lo utilizan:** `RosterMovement.action`.

---

## 2. Serialización y Mapeo en PostgreSQL (`values_callable`)

En los modelos compartidos con RCL-Next (`DiscordUser`, `TeamMembership`, `RosterMovement`), los valores del enum en PostgreSQL están definidos en minúsculas (`'top'`, `'viewer'`, `'joined'`), mientras que en Python las constantes simbólicas son mayúsculas (`RosterRole.TOP`, `AppRole.VIEWER`).

Para evitar discrepancias de serialización en el driver `asyncpg` y en el compilador de tipos de SQLAlchemy, las definiciones de columnas utilizan la directiva `values_callable`:

```python
Enum(
    RosterRole,
    name="roster_role",
    native_enum=True,
    values_callable=lambda obj: [e.value for e in obj],
)
```

- **Beneficio Técnico:** Garantiza que SQLAlchemy envíe a PostgreSQL la cadena en minúsculas correspondiente al atributo `.value`, preservando la paridad con el esquema de base de datos generado por Drizzle ORM en la plataforma web RCL-Next.
