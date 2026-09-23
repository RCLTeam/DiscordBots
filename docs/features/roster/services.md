# Servicios de Dominio: RosterSyncService e Invariantes Deportivas

Este documento detalla la lógica de negocio y las reglas deportivas implementadas en el servicio central de plantillas, `RosterSyncService`, responsable de coordinar las altas, bajas, cambios de posición y auditoría de los miembros de los equipos de la liga.

- **Ubicación en código:** `src/liga_bot/services/roster_sync_service.py`

---

## 1. Jerarquía de Excepciones de Dominio

El subsistema define una jerarquía orientada a desacoplar los errores de negocio de los fallos de transporte e infraestructura:

```
RosterSyncError (Base)
├── CompetitivePositionConflictError
├── PlayerNotTeamMemberError
└── InvalidCaptainRoleError
```

### 1.1 `RosterSyncError` (`L45–L46`)
Clase base que hereda de `Exception`. Permite a controladores e interfaces atrapar cualquier anomalía operativa generada por el subsistema de plantillas.

### 1.2 `CompetitivePositionConflictError` (`L49–L85`)
Se lanza cuando se intenta asignar una posición competitiva a un jugador que ya ostenta un rol competitivo en otro equipo de la liga.

- **Atributos:**
  - `discord_user_id` (`str | None`): Snowflake del jugador.
  - `existing_team_id` (`UUID | str | None`): UUID del equipo actual donde ya compite.
  - `new_team_id` (`UUID | str | None`): UUID del nuevo equipo objetivo.
  - `existing_role` (`str | None`): Valor del rol competitivo que ocupa en el equipo actual.
  - `attempted_role` (`str | None`): Valor del rol competitivo que se intentó asignar.
- **Mensaje Generado:**
  ```text
  Conflicto de posición competitiva: El usuario '<id>' ya ostenta el rol competitivo '<rol_actual>' en el equipo '<equipo_actual>'. No puede ocupar la posición competitiva '<rol_intentado>' en el equipo '<equipo_nuevo>'.
  ```

### 1.3 `PlayerNotTeamMemberError` (`L87–L106`)
Se lanza cuando se intenta consultar o modificar la posición de un usuario en un club en el que no tiene membresía registrada en la tabla `team_memberships`.

- **Atributos:**
  - `discord_user_id` (`str | None`): Snowflake del jugador.
  - `team_id` (`UUID | str | None`): Identificador del equipo consultado.

### 1.4 `InvalidCaptainRoleError` (`L109–L132`)
Se lanza cuando se intenta designar como capitán (`is_captain=True`) a un jugador cuyo rol no corresponde a una de las 5 posiciones titulares activas de juego.

- **Atributos:**
  - `role` (`str | None`): Rol que se intentó asociar a la capitanía.

---

## 2. Invariantes Deportivas y de Integridad

El servicio garantiza el cumplimiento de cuatro reglas deportivas fundamentales de la competición:

### Regla 1: Invariante de Rol Inicial No Competitivo (`L158–L163`)
Al inicializar `RosterSyncService`, se comprueba el rol por defecto de ingreso (`default_join_role`, por defecto `RosterRole.STAFF`).
- Si se configura un rol cuyo método `is_competitive()` devuelva `True` (`top`, `jungle`, `mid`, `adc`, `support`, `substitute`), el constructor aborta inmediatamente lanzando `ValueError`:
  ```python
  if default_join_role.is_competitive():
      raise ValueError(
          f"default_join_role debe ser un rol no competitivo, recibido: {default_join_role.value}"
      )
  ```
- **Propósito:** Previene que un usuario adquiera una posición de juego competitiva simplemente al recibir el rol del club en Discord, evitando descalificaciones o bloqueos involuntarios.

### Regla 2: Preservación de Membresías en Otros Clubes
La recepción de un rol de equipo o la adición a una plantilla en Discord **no revoca ni elimina roles de otros clubes**.
- Un miembro puede pertenecer libremente a varios clubes con roles no competitivos (por ejemplo, ser analista/staff en el Equipo A y entrenador en el Equipo B).

### Regla 3: Posición Competitiva Única a Nivel Liga (`L439–L448`)
Un jugador solo puede ostentar una posición competitiva en **como máximo un equipo** en toda la competición.
- Los roles competitivos corresponden a: `TOP`, `JUNGLE`, `MID`, `ADC`, `SUPPORT` y `SUBSTITUTE`.
- Si un miembro ya posee un rol competitivo en el Equipo A, cualquier intento de asignarle un rol competitivo en el Equipo B dispara `CompetitivePositionConflictError`.
- **Excepción interna:** El cambio de posición dentro del *mismo equipo* (por ejemplo, de `SUBSTITUTE` a `MID`) está permitido.
- **Roles no competitivos:** Los roles `COACH`, `STAFF` y `PARTNERS` están exentos de esta restricción y permiten la multipresencia entre clubes.

### Regla 4: Capitanía Exclusiva para Posiciones Titulares (`L416–L417`)
Únicamente los jugadores con rol de titular activo pueden ser designados capitanes oficiales del equipo.
- Posiciones habilitadas para capitanía (`is_starter() == True`): `TOP`, `JUNGLE`, `MID`, `ADC`, `SUPPORT`.
- Posiciones denegadas (`InvalidCaptainRoleError`): `SUBSTITUTE`, `COACH`, `STAFF`, `PARTNERS`.
- Esta invariante de software se encuentra blindada a nivel motor en la base de datos mediante el check constraint `team_memberships_captain_role_check`.

---

## 3. Métodos del Servicio

### 3.1 `_ensure_discord_user` (`L165–L198`)

Garantiza la existencia previa del usuario en la tabla `discord_users` antes de establecer relaciones de clave foránea.

- **Flujo:**
  1. Consulta `session.get(DiscordUser, discord_id)`.
  2. Si no existe: instancia `DiscordUser` con `role=AppRole.VIEWER`, nombres y avatar suministrados, y ejecuta `await session.flush()`.
  3. Si ya existe: compara `username`, `global_name` y `avatar_hash`. Si alguno cambió en Discord, actualiza los campos correspondientes y ejecuta `await session.flush()`.

---

### 3.2 `handle_role_added` (`L200–L298`)

Procesa de forma idempotente la asignación de un rol de Discord a un miembro.

```python
async def handle_role_added(
    self,
    member: discord.Member,
    role: discord.Role,
    actor_id: str | int | None = None,
    session: AsyncSession | None = None,
) -> TeamMembership | None:
```

- **Paso a Paso:**
  1. **Comprobación de Club:** Consulta `team_repo.get_by_role_id(role.id)`. Si el rol no corresponde a ningún club registrado en la tabla `teams`, retorna `None` sin efectos colaterales.
  2. **Normalización de IDs:** Limpia identificadores con `_clean_user_id`.
  3. **Asegurar Usuario:** Llama a `_ensure_discord_user` para el miembro y para el actor (si fue provisto).
  4. **Idempotencia:** Consulta `membership_repo.get(team.id, user_id_str)`. Si el registro ya existe, lo retorna directamente sin duplicar movimientos ni bitácoras.
  5. **Creación:** Crea el registro en `team_memberships` con `role=self.default_join_role` e `is_captain=False`.
  6. **Registro en Historial (`roster_movements`):** Registra el movimiento con acción `RosterMovementAction.JOINED`.
  7. **Registro en Bitácora (`audit_logs`):** Registra la acción `"roster.member_joined"` con `before=None` y `after` conteniendo el snapshot de la nueva membresía.
  8. **Manejo de Transacciones:** Si se suministra una sesión externa, la reutiliza; en caso contrario, abre una transacción autónoma mediante `transactional_session(self.session_factory)`.

---

### 3.3 `handle_role_removed` (`L299–L382`)

Procesa la desasignación de un rol de Discord y da de baja la membresía en la base de datos.

```python
async def handle_role_removed(
    self,
    member: discord.Member,
    role: discord.Role,
    actor_id: str | int | None = None,
    session: AsyncSession | None = None,
) -> bool:
```

- **Paso a Paso:**
  1. Verifica si el rol corresponde a un equipo. Si `team is None`, retorna `False`.
  2. Verifica si el usuario poseía membresía activa en dicho equipo. Si `membership is None`, retorna `False`.
  3. Si se especificó `actor_id`, asegura su registro en `discord_users`.
  4. Construye `before_payload` con el estado previo (`team_id`, `discord_user_id`, `role`, `is_captain`).
  5. Elimina físicamente el registro invocando `await membership_repo.delete(team.id, user_id_str)`.
  6. Registra en `roster_movements` con acción `RosterMovementAction.LEFT` conservando el rol previo.
  7. Registra en `audit_logs` la acción `"roster.member_left"` con el snapshot previo en `before` y `after=None`.
  8. Retorna `True`.

---

### 3.4 `change_player_position` (`L383–L520`)

Actualiza la posición deportiva y/o el estado de capitanía de un miembro en un club específico.

```python
async def change_player_position(
    self,
    discord_user_id: str | int,
    team_id: UUID | str,
    new_role: RosterRole | str,
    actor_id: str | int,
    is_captain: bool = False,
    session: AsyncSession | None = None,
) -> TeamMembership:
```

- **Flujo de Ejecución y Validaciones:**
  1. **Validación de Identificadores:** Verifica que `discord_user_id`, `actor_id` y `team_id` no sean nulos ni vacíos (`ValueError`).
  2. **Conversión y Parseo de Rol:** Convierte `new_role` a `RosterRole`. Lanza `ValueError` si el rol no es un valor reconocido.
  3. **Verificación de Capitanía Titular:**
     ```python
     if is_captain and not role_enum.is_starter():
         raise InvalidCaptainRoleError(role=role_enum)
     ```
  4. **Pertenencia al Club:** Valida que exista la membresía previa. Si no existe, lanza `PlayerNotTeamMemberError`.
  5. **Verificación de Posición Competitiva Única:**
     Si `role_enum.is_competitive()`, consulta `membership_repo.get_competitive_membership(clean_user_id)`. Si el jugador ya compite en otro equipo (`comp_membership.team_id != clean_team_id`), lanza `CompetitivePositionConflictError`.
  6. **Captura de Snapshot Previo:** Almacena `previous_role`, `was_captain` y genera el diccionario `before_payload`.
  7. **Actualización Relacional:** Ejecuta `await membership_repo.update_role(clean_team_id, clean_user_id, role_enum, is_captain=is_captain)`.
  8. **Clasificación de Movimiento:**
     - Si el rol no varió pero cambió la capitanía:
       - De `False` a `True`: `RosterMovementAction.PROMOTED_TO_CAPTAIN`.
       - De `True` a `False`: `RosterMovementAction.DEMOTED_FROM_CAPTAIN`.
     - Si cambió el rol (o hubo cambio simultáneo de rol y capitanía): `RosterMovementAction.ROLE_CHANGED`.
  9. **Trazabilidad en Bitácoras:**
     - Inserta en `roster_movements` la acción clasificada, el nuevo rol y el actor.
     - Inserta en `audit_logs` con `action="roster.role_changed"`, adjuntando `before_payload` y `after_payload` con serialización segura para JSONB.
  10. Retorna la entidad `TeamMembership` actualizada.

---

### 3.5 `get_user_teams` (`L522–L545`)

Recupera todas las membresías activas de un usuario junto con la entidad de su equipo precargada.

```python
async def get_user_teams(
    self,
    discord_user_id: str | int,
) -> list[tuple[Team, TeamMembership]]:
```

- Limpia el identificador del usuario.
- Consulta a través de `TeamMembershipRepository.list_by_user(clean_user_id, with_team=True)`.
- **Eager Loading Estratégico:** Utiliza `selectinload(TeamMembership.team)` en la consulta SQLAlchemy, asegurando que los atributos de `Team` (`name`, `tag`, `logo_url`) permanezcan accesibles en memoria fuera del ciclo de vida de la sesión asíncrona, evitando excepciones `DetachedInstanceError`.
- Devuelve una lista de tuplas `[(m.team, m) for m in memberships if m.team is not None]`.
