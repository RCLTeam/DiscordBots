# Componentes de Interfaz de Usuario (UI): Roles & Onboarding

[⬅️ Volver a Roles e Incorporación](./README.md)

Los componentes interactivos de Discord para la solicitud y gestión de roles residen en `src/liga_bot/ui/roles.py`. Utilizan las primitivas modernas de la API de Discord: modales nativos (`Modal`), selectores (`Select`), vistas efímeras y persistentes (`View`), y elementos dinámicos deserializables tras reinicio (`DynamicItem`).

---

## 1. Inventario de Componentes UI

| Clase | Tipo de Componente | Persistencia / Timeout | Rol Funcional |
|---|---|---|---|
| `SolicitudRolModal` | `discord.ui.Modal` | N/A (modal síncrono del cliente) | Captura nombre de invocador de LoL y Riot Tag. |
| `EquipoSelect` | `discord.ui.Select` | Montado en `EquipoSelectView` | Desplegable con los equipos registrados (o `TEAMS_ALL` como respaldo, máximo 24) + Libre. |
| `PosicionSelect` | `discord.ui.Select` | Montado en `PosicionSelectView` (`timeout=180.0` s) | Desplegable de posición; crea el ticket y lo deshace si no se puede publicar su mensaje. |
| `EquipoSelectView` | `discord.ui.View` | Efímera (`timeout=180.0` s) | Contenedor interactivo efímero del selector de equipos. |
| `PanelPedirRolView` | `discord.ui.View` | Persistente (`timeout=None`) | Vista con botón para abrir el modal en canales públicos. |
| `ConfirmarRolButton` | `discord.ui.DynamicItem` | Persistente (`custom_id` regex) | Botón de staff con auto-reconstrucción post-reinicio y borrado diferido. |
| `TicketView` | `discord.ui.View` | Persistente (`timeout=None`) | Vista del canal privado de ticket con botón de denegación. |

---

## 2. Modal de Entrada de Datos: `SolicitudRolModal`

Permite la captura estructurada de las credenciales de juego del miembro.

- **Ubicación**: `src/liga_bot/ui/roles.py:53-87`.
- **Título**: `"Solicitud de Rol de Jugador"`.
- **Campos de Texto (`TextInput`)**:

| Campo | Atributo | Longitud Mín / Máx | Requerido | Placeholder |
|---|---|:---:|:---:|---|
| **Nombre LoL** | `nombre_lol` | 1 / 100 | Sí | `"Ej: Invocador123"` |
| **Riot Tag** | `riot_tag` | 1 / 20 | Sí | `"Ej: EUW o 1234"` |

- **Callback `on_submit`**:
  Al completar el modal, el bot responde con un mensaje efímero privado que despliega la vista del selector `EquipoSelectView`:
  ```python
  await interaction.response.send_message(
      "Selecciona tu equipo:",
      view=EquipoSelectView(
          nombre_lol=self.nombre_lol.value,
          riot_tag=self.riot_tag.value,
      ),
      ephemeral=True,
  )
  ```

---

## 3. Selector de Equipos: `EquipoSelect` & `EquipoSelectView`

- **Ubicación**: `src/liga_bot/ui/roles.py:107-262` (selector de equipo) y `:269-400` (`PosicionSelect` y `PosicionSelectView`).
- **Límite de Opciones de Discord**: La API de Discord admite un máximo de 25 opciones por selector. `EquipoSelect` muestra hasta 24 equipos y la opción de agente libre:
  - **Equipos registrados en base de datos** (`role_service.list_team_names()`). Si la consulta falla o no devuelve ninguno, usa como respaldo los 20 equipos de la constante `TEAMS_ALL`:
    - *División Premier (10)*: Vanguard Gaming, Nexus Esports, Aegis Club, Eclipse Gaming, Apex Predators, Storm Legion, Titan Gaming, Ironclad Esports, Shadow Guard, Valiant Esports.
    - *División Ascend (10)*: Frostbite Esports, Infernal Gaming, Thunder Squad, Venomous Club, Quantum Gaming, Zephyr Esports, Nova Core, Crimson Tide, Spectre Gaming, Blaze Syndicate.
  - **1 Opción de Agente Libre**:
    - `label`: Nombre de rol libre resuelto (por defecto `"Libre"`).
    - `emoji`: `"🕊️"`.
    - `description`: `"Agente libre sin equipo asignado"`.
- **Configuración del Componente**:
  - `placeholder="Selecciona tu equipo o agente libre..."`
  - `min_values=1`, `max_values=1`
- **Límites de longitud de Discord (100 caracteres por `label`, `value` y `description`)**:
  - `label` y `description` (`"Solicitar rol para {equipo}"`) se recortan a 100 caracteres con `_truncate`, que marca el corte con `…`.
  - Si el nombre del equipo cabe en 100 caracteres, se usa como `value`. Si no, el `value` es una clave corta (`equipo-largo:{índice}`) y el selector guarda en `_team_by_value` su nombre completo, que el callback recupera antes de seguir. Así el ticket se crea con el nombre completo del equipo.

### Comportamiento del Callback (`EquipoSelect.callback`)

1. **Selección de Agente Libre**:
   - Invoca directamente `role_service.assign_free_role(...)`.
   - Asigna el rol, normaliza el apodo y registra la solicitud aprobada sin crear canales de soporte.
   - Emite confirmación efímera al solicitante.
2. **Selección de Equipo Oficial**: responde con `PosicionSelectView` para que el jugador elija su posición (opciones de `POSICION_DESCRIPCIONES`, compartidas con el parámetro `posicion` de `/asignar-rol`).
3. **Selección de Posición (`PosicionSelect.callback`)**:
   - Ejecuta `await interaction.response.defer(ephemeral=True)` para extender el plazo de respuesta ante la creación del canal en Discord y la persistencia en base de datos.
   - Invoca `role_service.create_role_request_ticket(...)` con el equipo y la posición.
   - Si se genera el canal exitosamente:
     - Publica en el canal del ticket un embed con los datos del jugador, el equipo y la posición, con `TicketView(user_id=member.id, equipo=equipo)`.
     - Notifica al usuario en el mensaje efímero con el enlace directo al canal: `f"Ticket creado en {channel.mention}."`.
   - **Rollback si el mensaje del ticket no se publica** (límites de Discord, permisos del canal, error HTTP): llama a `role_service.discard_role_request_ticket(channel)`, que elimina la solicitud `PENDING` del canal y el propio canal, y avisa al jugador con un mensaje efímero («No se pudo publicar la solicitud en el canal del ticket y se ha cancelado…»). No queda canal huérfano y el jugador puede volver a pedir rol; la cuenta de juego registrada al abrir el ticket se conserva.

### Vista Contenedora (`EquipoSelectView`)

- `discord.ui.View` configurada con `timeout=180.0` segundos.
- Se envía siempre en respuestas efímeras para que solo el solicitante interactúe con el selector.

---

## 4. Panel Persistente: `PanelPedirRolView`

- **Ubicación**: `src/liga_bot/ui/roles.py:473-494`.
- **Persistencia**: `timeout=None`.
- **Botón `pedir_rol`**:
  - `label="Solicitar mi rol"`, `style=discord.ButtonStyle.primary`, `emoji="🎮"`.
  - `custom_id="solicitud_rol:panel_pedir_rol"`.
- **Callback**:
  - Al pulsarse, ejecuta `await interaction.response.send_modal(SolicitudRolModal())`.
  - Puede ser utilizado concurrentemente por cualquier número de miembros sin interferencias.

---

## 5. Botón Dinámico de Confirmación: `ConfirmarRolButton`

Implementa el patrón de elementos dinámicos (`discord.ui.DynamicItem`) para permitir que los botones de confirmación en tickets sigan funcionando tras un reinicio del bot sin requerir el re-escaneo de canales.

- **Ubicación**: `src/liga_bot/ui/roles.py:500-609`.
- **Template Regex de Registro**:
  ```python
  class ConfirmarRolButton(
      discord.ui.DynamicItem[discord.ui.Button[Any]],
      template=r"confirmar_rol:(?P<user_id>\d+)(?::(?P<equipo>.+))?$",
  ):
  ```
- **Constructor y Custom ID**:
  - Genera `custom_id=f"confirmar_rol:{user_id}"`. El nombre del equipo no va en el `custom_id`, que Discord limita a 100 caracteres: con un ID de usuario de hasta 20 dígitos el `custom_id` nunca pasa de 34 caracteres, sea cual sea el nombre del equipo.
  - `equipo` es opcional y se conserva como atributo informativo; el callback localiza la solicitud por el canal del ticket, no por el equipo.
  - Estilo: `discord.ButtonStyle.success`, `label="Confirmar Rol"`, `emoji="✅"`.
- **Compatibilidad con tickets anteriores**: la plantilla también acepta el formato anterior `confirmar_rol:{user_id}:{equipo}`, así que los botones de los tickets abiertos antes del cambio siguen funcionando.
- **Deserialización Dinámica (`from_custom_id`)**:
  - Cuando un botón persistente es presionado tras un reinicio, `discord.py` ejecuta el método de clase `from_custom_id`, que conserva el `custom_id` recibido (nuevo o anterior):
    ```python
    @classmethod
    async def from_custom_id(cls, interaction, item, match, /) -> ConfirmarRolButton:
        button = cls(user_id=int(match["user_id"]), equipo=match["equipo"])
        button.custom_id = match.group(0)
        return button
    ```

### Callback y Auto-Eliminación en 5 Segundos

1. **Filtro de Seguridad**: Valida `is_staff(interaction.user)`. Si no es miembro del staff, emite un aviso efímero y rechaza la interacción.
2. **Confirmación en Dominio**: Invoca `await role_service.confirm_role_request(...)`. Si el equipo no está registrado, su rol no existe en el servidor o la plantilla no se puede registrar, el staff recibe la causa en un mensaje efímero y la solicitud sigue pendiente (ver [`services.md`](./services.md#51-aprobación-y-confirmación-confirm_role_request)).
3. **Respuesta Visual y Programación**:
   - Si la asignación se completa, envía un mensaje público al canal del ticket:
     `"✅ Rol {equipo} confirmado para {usuario}...\nEste canal se eliminará en 5 segundos."`
   - Programa la tarea de eliminación diferida: `self._schedule_deletion(interaction.channel, delay=5.0)`.
4. **Mecanismo de Borrado Asíncrono**:
   - Crea una corrutina desacoplada con `asyncio.create_task(self._delete_channel_later(channel, delay))`.
   - Espera `await asyncio.sleep(5.0)`.
   - Ejecuta `await channel.delete(reason="Solicitud de rol confirmada")` capturando posibles excepciones si el canal fue eliminado manualmente antes de expirar el temporizador.

---

## 6. Vista del Canal de Ticket: `TicketView`

- **Ubicación**: `src/liga_bot/ui/roles.py:616-718`.
- **Persistencia**: `timeout=None`.
- **Modos de Inicialización**:
  - *Modo Dinámico con Datos* (`TicketView(user_id=123, equipo="Apex")`): Instancia y añade internamente `ConfirmarRolButton(user_id, equipo)` (basta con `user_id`).
  - *Modo Persistente Global* (`TicketView()`): Utilizado en `cog_load()` para registrar el botón estático de denegación.
- **Botón `denegar_rol`**:
  - `label="Denegar Rol"`, `style=discord.ButtonStyle.danger`, `emoji="❌"`.
  - `custom_id="solicitud_rol:ticket_view:denegar"`.
- **Callback de Denegación**:
  - Verifica privilegios de staff mediante `is_staff(interaction.user)`.
  - Invoca `await role_service.deny_role_request(...)` para transicionar el estado en la base de datos a `RoleRequestStatus.DENIED`.
  - Envía al canal: `"❌ Solicitud de rol denegada.\nEste canal se eliminará en 5 segundos."`.
  - Programa la tarea de auto-eliminación en 5 segundos con motivo `"Solicitud de rol denegada"`.
