# Comandos de Roles e Incorporación (Roles & Onboarding)

[⬅️ Volver a Roles e Incorporación](./README.md)

El subsistema de comandos de roles está implementado en `src/liga_bot/cogs/roles.py` a través de la clase `RolesCog`. Gestiona el flujo interactivo de solicitud de roles para nuevos jugadores, la asignación directa por parte del equipo de administración (Staff) y la publicación de paneles interactivos persistentes en los canales del servidor de Discord.

---

## 1. Estructura y Ciclo de Vida (`RolesCog`)

`RolesCog` extiende `discord.ext.commands.Cog` y centraliza los controladores de comandos slash (`app_commands`), los oyentes de eventos del gateway (`Cog.listener`) y el registro de componentes persistentes.

### Registro en el Ciclo de Carga (`cog_load`)

Para garantizar que los botones y paneles interactivos continúen funcionando tras un reinicio del bot sin requerir el reenvío de mensajes, `cog_load()` registra tres elementos en el gestor de vistas de `discord.py`:

```python
async def cog_load(self) -> None:
    self.bot.add_view(PanelPedirRolView())
    self.bot.add_view(TicketView())
    self.bot.add_dynamic_items(ConfirmarRolButton)
```

1. **`PanelPedirRolView`**: Registra el callback persistente para el botón del panel público (`custom_id="solicitud_rol:panel_pedir_rol"`).
2. **`TicketView`**: Registra la vista estática con el botón de denegación (`custom_id="solicitud_rol:ticket_view:denegar"`).
3. **`ConfirmarRolButton`**: Registra el manejador dinámico basado en expresiones regulares (`DynamicItem`) para reconstruir botones de confirmación específicos por usuario y equipo (`confirmar_rol:{user_id}:{equipo}`).

---

## 2. Eventos del Gateway (`on_member_join`)

`RolesCog` escucha el evento `on_member_join` para iniciar el protocolo de bienvenida y control de acceso inicial.

- **Ubicación**: `src/liga_bot/cogs/roles.py:64-70`.
- **Firma**: `async def on_member_join(self, member: discord.Member) -> None`
- **Flujo de Ejecución**:
  1. Resuelve la instancia inyectada de `RoleService` desde `self.bot.role_service`.
  2. Si el servicio se encuentra disponible, delega en `await role_service.handle_member_join(member)`.
  3. Si `role_service` es `None`, el evento finaliza de manera silenciosa sin interrumpir el funcionamiento del bot.

---

## 3. Catálogo de Comandos Slash

### 3.1 `/pedir-rol`

Abre directamente el modal interactivo nativo de Discord para que un miembro inicie su solicitud de vinculación deportiva.

- **Ubicación**: `src/liga_bot/cogs/roles.py:71-78`.
- **Ámbito y Restricciones**:
  - Comando público ejecutable por cualquier miembro de la guild.
  - Sin anotación `@app_commands.default_permissions` (acceso universal).
- **Parámetros**: Ninguno.
- **Comportamiento**:
  - Ejecuta `await interaction.response.send_modal(SolicitudRolModal())`.
  - Muestra en pantalla el formulario modal que solicita el nombre de invocador en League of Legends y el Riot Tag.

---

### 3.2 `/publicar-panel-rol`

Publica el mensaje visual incrustado (*embed*) con el botón interactivo persistente que permite a los usuarios abrir el modal de solicitud de rol.

- **Ubicación**: `src/liga_bot/cogs/roles.py:211-252`.
- **Permisos por Defecto**: `@app_commands.default_permissions(manage_guild=True)`.
- **Control de Acceso en Runtime**:
  - Valida la autorización ejecutando `is_staff(interaction.user, self.settings)`.
  - Si el usuario no pertenece a los roles configurados como Staff (`staff_role_id`) o directiva (`ceo_role_id`), rechaza la ejecución con un mensaje efímero: `"Solo el staff puede publicar el panel."`.
- **Parámetros**:

| Parámetro | Tipo | Obligatorio | Descripción |
|---|---|:---:|---|
| `canal` | `discord.TextChannel \| None` | No | Canal destino donde se enviará el panel. Si se omite, se utiliza el canal donde se ejecutó el comando (`interaction.channel`). |

- **Flujo de Operación**:
  1. Verifica los privilegios del invocador.
  2. Valida que el canal destino admita el método `send`. Si no es un canal de texto válido, emite una advertencia efímera.
  3. Construye el embed del panel con `build_panel_rol_embed()`: título `"🔥 Únete a la Rebel Crown Legacy"`, instrucciones de solicitud y estilo morado (`discord.Color.purple()`).
  4. Envía el mensaje con la vista adjunta: `await target_channel.send(embed=embed, view=PanelPedirRolView())`.
  5. Emite confirmación efímera al invocador: `f"Panel publicado en {target_channel.mention}."`.

---

### 3.3 `/asignar-rol`

Comando administrativo para asignar directamente un equipo registrado o el rol de agente libre, sin abrir un ticket. Para un equipo deja en Discord y en base de datos lo mismo que confirmar un ticket con ese equipo y posición.

- **Ubicación**: `src/liga_bot/cogs/roles.py:89-178`.
- **Permisos por Defecto**: `@app_commands.default_permissions(manage_guild=True)`.
- **Control de Acceso en Runtime**:
  - Comprobación obligatoria vía `is_staff(interaction.user, self.settings)`.
  - Respuesta efímera de bloqueo para usuarios no autorizados: `"Solo el staff puede asignar roles."`.
  - **No autoasignación**: si `usuario` es quien ejecuta el comando, responde `"No puedes asignarte un rol a ti mismo."` sin tocar nada (igual que la regla de los tickets, tanto para equipos como para Libre).
- **Parámetros**:

| Parámetro | Tipo | Obligatorio | Descripción |
|---|---|:---:|---|
| `usuario` | `discord.Member` | Sí | Miembro del servidor al que se asignará el rol y actualizará el apodo. |
| `equipo` | `str` | Sí | Nombre de un equipo registrado en base de datos (sin distinguir mayúsculas) o el nombre de agente libre (`settings.free_role_name`, por defecto `"Libre"`). Con autocompletado de los equipos registrados y Libre (máximo 25 sugerencias; se omiten nombres de más de 100 caracteres). |
| `nombre_lol` | `str` | Sí | Nombre del jugador en League of Legends. |
| `riot_tag` | `str` | Sí | Riot Tag del jugador (sin incluir el carácter `#`). |
| `posicion` | `str` (opciones) | Para equipos | Posición en la plantilla, con las mismas opciones que el desplegable del ticket (`top`, `jungle`, `mid`, `adc`, `support`, `substitute`, `coach`, `staff`, `partners`). Se ignora para Libre. |

- **Flujo de Operación**:
  1. Verifica los privilegios del invocador, que se ejecute dentro de un servidor, la regla de no autoasignación y que `role_service` esté disponible. Estos rechazos se responden con `interaction.response.send_message(..., ephemeral=True)`.
  2. Difiere la interacción con `await interaction.response.defer(ephemeral=True)` antes de cualquier cambio en Discord o en base de datos, de modo que el comando responde aunque las llamadas tarden más de 3 segundos.
  3. Si `equipo` es el nombre de agente libre, delega en `role_service.assign_free_role(usuario, nombre_lol, riot_tag)` (comportamiento sin cambios: rol Libre, apodo con el nombre de invocador, cuenta de juego y solicitud `APPROVED`).
  4. En otro caso, delega en `role_service.assign_team_role(...)` (ver [`services.md`](./services.md#53-asignación-directa-de-equipo-assign_team_role)): solo acepta equipos registrados, toma el rol por `discord_role_id` y nunca busca un rol del servidor por nombre.
  5. Envía el mensaje del servicio con `interaction.followup.send(msg, ephemeral=True)`.

---

## 4. Matriz de Errores y Excepciones en Comandos

| Comando | Excepción / Condición | Manejo / Mitigación | Efecto en Usuario |
|---|---|---|---|
| `/pedir-rol` | Invocación en canal restringido | Gestionado por la configuración de canales de Discord | El modal se abre en el cliente local |
| `/publicar-panel-rol` | Usuario sin rol de staff | Verificación `is_staff()` | Mensaje efímero de denegación |
| `/publicar-panel-rol` | Canal no soporta `.send` | Comprobación de atributo y tipo | Mensaje efímero de error de canal |
| `/asignar-rol` | Invocación fuera de servidor (DM) | Comprobación `interaction.guild is None` | Mensaje efímero `"❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord."` |
| `/asignar-rol` | El invocador es el destinatario | Comparación de IDs antes de diferir | Mensaje efímero `"No puedes asignarte un rol a ti mismo."` |
| `/asignar-rol` | Equipo no registrado (aunque exista un rol con ese nombre) | `TeamRepository.get_by_name` en `assign_team_role` | Mensaje efímero con la causa; no se asigna ningún rol |
| `/asignar-rol` | `discord_role_id` del equipo inexistente en el servidor | `guild.get_role` en `assign_team_role` | Mensaje efímero con el ID; no se asigna ningún rol |
| `/asignar-rol` | Posición ausente o no válida para un equipo | Validación con `RosterRole` | Mensaje efímero; no se toca Discord ni la base de datos |
| `/asignar-rol` | Fallo de base de datos o `RosterSyncError` | Transacción revertida antes de tocar Discord | Mensaje efímero con la causa; no se aplica ningún cambio |
| `/asignar-rol` | Jerarquía insuficiente del bot al asignar el rol | Captura de `discord.Forbidden`/`HTTPException` tras el commit | Aviso «asígnalo a mano» añadido al mensaje |
| `/asignar-rol` | Apodo superior a 32 caracteres | Truncado estricto `[:32]` | Apodo ajustado sin lanzar `HTTPException 400` |
