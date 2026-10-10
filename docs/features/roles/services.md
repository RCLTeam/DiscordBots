# Servicios de Dominio: RoleService

[⬅️ Volver a Roles e Incorporación](./README.md)

El subsistema de lógica de negocio para la gestión de roles se implementa en `src/liga_bot/services/role_service.py` a través de la clase `RoleService`. Esta capa orquestadora aísla las reglas del negocio de los controladores de Discord y de los repositorios de persistencia relacional.

---

## 1. Arquitectura e Inyección de Dependencias

`RoleService` centraliza las operaciones del ciclo de vida de los miembros en la liga, administrando la interacción con la API de Discord y la base de datos PostgreSQL mediante sesiones asíncronas de SQLAlchemy.

### Constructor y Parámetros

```python
def __init__(
    self,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
    bot: commands.Bot | discord.Client | None = None,
) -> None:
    self.session_factory: async_sessionmaker[AsyncSession] = (
        session_factory or get_session_factory()
    )
    self.settings: Settings = settings or get_settings()
    self.bot: commands.Bot | discord.Client | None = bot
```

- **`session_factory`**: Fábrica de sesiones asíncronas para el acceso a datos. Si no se provee, recurre al gestor global `get_session_factory()`.
- **`settings`**: Configuración de entorno de la aplicación (`Settings`), resolviendo IDs de roles (`staff_role_id`, `ceo_role_id`, `sin_verificar_role_id`, `ticket_rol_category_id`) y nombres predeterminados (`free_role_name`).
- **`bot`**: Instancia cliente de Discord para interactuar con la jerarquía del servidor o ejecutar tareas programadas.

---

## 2. Flujo de Incorporación: `handle_member_join`

Se invoca automáticamente cuando un nuevo usuario ingresa al servidor de Discord de la liga.

- **Firma**: `async def handle_member_join(self, member: discord.Member) -> bool`
- **Ubicación**: `src/liga_bot/services/role_service.py:64-105`.

### Pasos de Ejecución

1. **Verificación de Rol No Verificado**:
   - Comprueba si `self.settings.sin_verificar_role_id > 0`.
   - Busca el rol en `member.guild.roles` o mediante `member.guild.get_role(role_id)`.
   - Si no se encuentra el rol en el servidor, emite una advertencia en logs y retorna `False`.
2. **Asignación del Rol**:
   - Ejecuta `await member.add_roles(role)`.
   - Captura excepciones `discord.Forbidden` (falta de permisos de gestión de roles en el bot) y `discord.HTTPException`, registrando el error y retornando `False`.
3. **Instrucciones por Mensaje Directo (DM)**:
   - Envía un mensaje de bienvenida informando sobre el uso del canal de solicitudes o el comando `/pedir-rol`.
   - **Tolerancia a DMs Bloqueados**: Si el usuario tiene desactivados los mensajes directos de miembros del servidor (`discord.Forbidden`), el error es capturado a nivel `logger.info` y **no anula el flujo ni marca la operación como fallida**.
4. **Retorno**: Devuelve `True` al completar el proceso de bienvenida.

---

## 3. Asignación de Agente Libre: `assign_free_role`

Permite la vinculación directa de un usuario como agente libre sin abrir un canal de ticket de soporte.

- **Firma**: `async def assign_free_role(self, member: discord.Member, nombre_lol: str, riot_tag: str) -> tuple[bool, str]`
- **Ubicación**: `src/liga_bot/services/role_service.py:264-350`.

### Pasos de Ejecución

1. **Normalización Defensiva de Riot Tag**:
   - Ejecuta `riot_tag = normalize_riot_tag(riot_tag)` al inicio de la función, eliminando cualquier carácter `#` y espacios residuales, y truncando a un máximo de 5 caracteres alfanuméricos según el estándar de Riot Games.
2. **Resolución del Rol de Agente Libre**:
   - Localiza el rol configurado en `self.settings.free_role_name` (por defecto `"Libre"`) en la colección `member.guild.roles`.
   - Si no existe el rol en el servidor, retorna `(False, "El rol 'Libre' no existe en el servidor.")`.
3. **Remoción de Rol 'Sin Verificar'**:
   - Si `self.settings.sin_verificar_role_id > 0` y el usuario lo tiene asignado, ejecuta `await member.remove_roles(sin_verificar)`.
   - Fallos de jerarquía en este paso no interrumpen la ejecución y se registran como advertencias.
4. **Asignación del Rol y Actualización de Apodo**:
   - Añade el rol de agente libre: `await member.add_roles(free_role)`.
   - Modifica el apodo del miembro con su nombre de invocador, truncado a 32 caracteres:
     ```python
     nick = nombre_lol[:32]
     await member.edit(nick=nick)
     ```
5. **Persistencia Transaccional**:
   - Abre un bloque `async with transactional_session(self.session_factory) as session:`.
   - Registra la cuenta de juego en la tabla `players` llamando a `await self._ensure_player(member, nombre_lol, riot_tag, session)` con el Riot Tag saneado.
   - Registra la solicitud en base de datos con `canal_id=None` y el Riot Tag saneado:
     ```python
     req = await repo.create_request(
         user_id=member.id,
         nombre_lol=nombre_lol,
         riot_tag=riot_tag,
         equipo=self.settings.free_role_name,
         canal_id=None,
     )
     await repo.update_status(req.id, RoleRequestStatus.APPROVED)
     ```
   - Si ocurre una excepción de persistencia, se retorna `(False, "Error al registrar la asignación en la base de datos.")`.
6. **Retorno**: `(True, f"Rol {self.settings.free_role_name} asignado correctamente.")`.

---

## 4. Creación de Tickets y Aislamiento Atómico: `create_role_request_ticket`

Crea un canal privado de texto exclusivo para tramitar la solicitud de incorporación a un equipo oficial de la liga.

- **Firma**:
  ```python
  async def create_role_request_ticket(
      self,
      guild: discord.Guild,
      member: discord.Member,
      nombre_lol: str,
      riot_tag: str,
      equipo: str,
      posicion: str | None = None,
  ) -> tuple[bool, str, discord.TextChannel | None]
  ```
- **Ubicación**: `src/liga_bot/services/role_service.py:352-471`.

### 4.1 Saneamiento de Tag y Prevención de Solicitudes Duplicadas

1. **Normalización Defensiva**: Al inicio de la invocación ejecuta `riot_tag = normalize_riot_tag(riot_tag)` para garantizar que el tag no posea caracteres `#` ni espacios y respete el tope de 5 caracteres.
2. **Prevención de Duplicados**: Antes de crear cualquier canal en Discord, el servicio abre una sesión y consulta:

```python
active = await repo.get_active_by_user(member.id)
if active is not None:
    return False, "Ya tienes una solicitud de rol pendiente.", None
```

Si el usuario ya tiene un registro en estado `RoleRequestStatus.PENDING`, la operación se rechaza inmediatamente, evitando la proliferación de tickets redundantes.

3. **Registro Previo de Cuenta de Juego**: Ejecuta `await self._ensure_player(member=member, nombre_lol=nombre_lol, riot_tag=riot_tag)` antes de interactuar con la API de Discord para evitar solicitudes o canales huérfanos si la base de datos no está disponible.

### 4.2 Matriz de Sobreescritura de Permisos (`PermissionOverwrite`)

El canal se crea con aislamiento estricto de visibilidad para salvaguardar la privacidad de los datos del jugador:

| Entidad / Rol | Permisos Otorgados | Permisos Denegados | Justificación Técnica |
|---|---|---|---|
| `@everyone` (`guild.default_role`) | — | `read_messages=False`, `send_messages=False` | Canal totalmente invisible y bloqueado para miembros generales. |
| **Solicitante** (`member`) | `read_messages=True`, `send_messages=True`, `attach_files=True` | — | Permite al jugador interactuar y subir capturas de verificación si fuera necesario. |
| **Bot** (`guild.me`) | `read_messages=True`, `send_messages=True`, `manage_channels=True` | — | Necesario para enviar embeds interactivos y eliminar el canal tras su resolución. |
| **Staff** (`staff_role_id`) | `read_messages=True`, `send_messages=True` | — | Habilita a los moderadores a revisar la solicitud y usar los botones interactivos. |
| **CEO / Directiva** (`ceo_role_id`) | `read_messages=True`, `send_messages=True` | — | Supervisión directiva de las solicitudes de ingreso. |

### 4.3 Creación del Canal y Categoría

- **Categoría**: Si `settings.ticket_rol_category_id > 0`, resuelve el canal de categoría en la guild.
- **Normalización del Nombre**:
  ```python
  channel_name = f"rol-{member.name}".lower()[:32]
  channel = await guild.create_text_channel(
      name=channel_name,
      overwrites=overwrites,
      category=category,
  )
  ```

### 4.4 Garantía de Rollback Atómico (Cero Canales Huérfanos)

Una vez creado el canal en Discord, se procede a la inserción en la base de datos dentro de una transacción. Si la base de datos falla o se produce cualquier error inesperado, el canal recién creado se destruye inmediatamente para impedir que queden canales huérfanos en el servidor:

```python
try:
    async with transactional_session(self.session_factory) as session:
        repo = RoleRequestRepository(session)
        await repo.create_request(
            user_id=member.id,
            nombre_lol=nombre_lol,
            riot_tag=riot_tag,
            equipo=equipo,
            canal_id=channel.id,
        )
except Exception as exc:
    logger.error(
        "Error al registrar solicitud de rol en base de datos para canal %s: %s", channel.id, exc
    )
    try:
        await channel.delete(reason="Error al registrar solicitud de rol en base de datos")
    except Exception as del_exc:
        logger.warning("No se pudo eliminar canal huérfano %s: %s", channel.id, del_exc)
    return False, "Error al registrar la solicitud en base de datos.", None
```

### 4.5 Rollback si no se publica el mensaje del ticket: `discard_role_request_ticket`

- **Firma**: `async def discard_role_request_ticket(self, channel: discord.abc.GuildChannel) -> None`
- **Ubicación**: `src/liga_bot/services/role_service.py:473-498`.
- La invoca `PosicionSelect.callback` cuando `channel.send` falla al publicar el mensaje con los botones del ticket.
- Elimina la solicitud `PENDING` asociada al canal (para que `get_active_by_user` deje de bloquear una nueva solicitud) y después el canal. Es defensiva: registra los fallos en el log sin propagarlos. La cuenta de juego registrada al abrir el ticket se conserva.

---

## 5. Resolución de Solicitudes

### 5.1 Aprobación y Confirmación: `confirm_role_request`

- **Firma**: `async def confirm_role_request(self, guild: discord.Guild, channel_id: int, staff_member: discord.Member) -> tuple[bool, str]`
- **Ubicación**: `src/liga_bot/services/role_service.py:609-723` (resolución del equipo en `_resolve_team_role`, `:500-539`; cambios en Discord en `_apply_team_role_in_discord`, `:541-607`).

#### Flujo Operativo:
1. **Validación de Estado**: Consulta en la base de datos `req = await repo.get_by_channel_id(channel_id)`. Si no existe o su estado es distinto de `RoleRequestStatus.PENDING`, aborta indicando que no hay solicitud pendiente asociada.
2. **Regla de no autoaprobación**: Si `staff_member.id == req.user_id`, rechaza con «No puedes confirmar ni denegar tu propia solicitud de rol.».
3. **Resolución del Miembro**:
   - Intenta resolver el usuario en la caché local (`guild.get_member(req.user_id)`).
   - Si no está en memoria, invoca `await guild.fetch_member(req.user_id)`.
   - Si el miembro abandonó el servidor, cancela la operación informando que el solicitante ya no se encuentra en la guild.
4. **Resolución del Equipo y su Rol** (`_resolve_team_role`):
   - Busca el equipo por nombre en base de datos (`TeamRepository.get_by_name(req.equipo)`) y toma el rol de su `discord_role_id` (`guild.get_role`). No se busca ningún rol por nombre entre los roles del servidor.
   - Equipo no registrado → `(False, "El equipo '…' no está registrado en la base de datos. … La solicitud sigue pendiente.")`.
   - `discord_role_id` sin rol en el servidor → `(False, "El rol del equipo '…' (discord_role_id …) no existe en el servidor. … La solicitud sigue pendiente.")`.
   - En ambos casos se registra un aviso en el log con el nombre del equipo (y el `discord_role_id` si existe), no se toca Discord y la solicitud sigue `PENDING`. Tras corregir el registro (por ejemplo, con `liga-cli seed-teams`), el mismo botón «Confirmar Rol» funciona sin abrir un ticket nuevo.
5. **Persistencia Atómica (Fase 1)**: en la misma transacción, `transfer_player` crea la membresía con `req.posicion` (si la solicitud la tiene y `RosterSyncService` está disponible) y la solicitud pasa a `APPROVED` con `staff_id=staff_member.id`.
   - Un `RosterSyncError` (por ejemplo, un conflicto de posición competitiva) revierte la transacción y su mensaje llega al staff seguido de «La solicitud sigue pendiente.».
   - Cualquier otra excepción revierte la transacción y devuelve «No se pudo registrar la confirmación en base de datos. No se ha aplicado ningún cambio.».
6. **Cambios en Discord (Fase 2, `_apply_team_role_in_discord`)**: tras el commit, asigna el rol del equipo, remueve el rol no verificado si está presente y pone el apodo `<TAG> <NombreLoL>` con `apply_team_tag`, truncado a 32 caracteres. Ningún fallo interrumpe los pasos siguientes; si Discord rechaza el rol del equipo, el aviso «No se pudo asignar el rol '…' en Discord (…); asígnalo a mano.» se añade al mensaje de retorno.
7. **Retorno**: `(True, f"Rol {req.equipo} confirmado para {member.display_name}.")`, más los avisos de la fase 2 si los hay.

---

### 5.3 Asignación Directa de Equipo: `assign_team_role`

Usado por `/asignar-rol` cuando el destino es un equipo. Deja lo mismo que confirmar un ticket con ese equipo y posición.

- **Firma**:
  ```python
  async def assign_team_role(
      self,
      guild: discord.Guild,
      member: discord.Member,
      staff_member: discord.Member,
      equipo: str,
      nombre_lol: str,
      riot_tag: str,
      posicion: str | None,
  ) -> tuple[bool, str]
  ```
- **Ubicación**: `src/liga_bot/services/role_service.py:725-840`.

#### Flujo Operativo:
1. **Normalización Defensiva**: Ejecuta `riot_tag = normalize_riot_tag(riot_tag)` al inicio de la función purgando caracteres `#` y espacios residuales, truncando a un máximo de 5 caracteres.
2. **No autoasignación**: si `staff_member.id == member.id`, retorna `(False, "No puedes asignarte un rol a ti mismo.")`.
3. **Posición**: obligatoria y válida como `RosterRole`; si no, retorna un error sin tocar nada.
4. **Servicio de plantillas**: si `bot.roster_sync_service` no está disponible, retorna un error sin tocar nada (a diferencia del ticket, aquí la cuenta y la membresía son obligatorias).
5. **Fase 1 (una transacción)**:
   - `_resolve_team_role`: equipo registrado y rol por `discord_role_id`, con los mismos errores que en la confirmación del ticket. Un rol del servidor que no es de un equipo registrado se rechaza.
   - `_ensure_player`: cuenta de juego en `players` con `riot_tag` normalizado.
   - `transfer_player`: membresía con la posición elegida, movimientos y auditoría.
   - `create_request(equipo=team.name, canal_id=None, riot_tag=riot_tag, posicion=...)` + `update_status(APPROVED, staff_id=staff_member.id)`.
   - `TeamRoleNotResolvedError` y `RosterSyncError` revierten la transacción y devuelven su mensaje; cualquier otra excepción devuelve «No se pudo registrar la asignación en base de datos. No se ha aplicado ningún cambio.». En ambos casos no se toca Discord.
6. **Fase 2 (`_apply_team_role_in_discord`)**: rol del equipo, retirada de `Sin Verificar` y apodo `<TAG> <NombreLoL>`, igual que la confirmación del ticket.
7. **Retorno**: `(True, f"Rol {team.name} asignado a {member.display_name} ({posicion}).")`, más los avisos de la fase 2 si los hay.

---

### 5.2 Denegación de Solicitud: `deny_role_request`

- **Firma**: `async def deny_role_request(self, guild: discord.Guild, channel_id: int, staff_member: discord.Member) -> tuple[bool, str]`
- **Ubicación**: `src/liga_bot/services/role_service.py:842-877`.

#### Flujo Operativo:
1. Registra en el sistema de auditoría la acción del staff: `logger.info("Denegando solicitud de rol en canal %s... por staff %s", channel_id, staff_member.id)`.
2. Recupera la solicitud asociada al canal en estado `RoleRequestStatus.PENDING`. Si no existe, retorna error.
3. Actualiza el registro a `RoleRequestStatus.DENIED`, asociando `staff_id = staff_member.id`.
4. Retorna `(True, "Solicitud de rol denegada.")`.
5. *(Nota: La interfaz de usuario es la responsable de programar el borrado del canal tras 5 segundos al recibir esta confirmación).*
