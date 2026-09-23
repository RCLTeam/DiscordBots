# Comandos de Roles e Incorporación (Roles & Onboarding)

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
  3. Construye un `discord.Embed` con título `"Solicitud de Rol de Jugador"` y estilo informativo azul (`discord.Color.blue()`).
  4. Envía el mensaje con la vista adjunta: `await target_channel.send(embed=embed, view=PanelPedirRolView())`.
  5. Emite confirmación efímera al invocador: `f"Panel publicado en {target_channel.mention}."`.

---

### 3.3 `/asignar-rol`

Comando administrativo de asignación manual e inmediata de roles oficiales o de agente libre, sin necesidad de apertura de ticket previo.

- **Ubicación**: `src/liga_bot/cogs/roles.py:79-210`.
- **Permisos por Defecto**: `@app_commands.default_permissions(manage_guild=True)`.
- **Control de Acceso en Runtime**:
  - Comprobación obligatoria vía `is_staff(interaction.user, self.settings)`.
  - Respuesta efímera de bloqueo para usuarios no autorizados: `"Solo el staff puede asignar roles."`.
- **Parámetros**:

| Parámetro | Tipo | Obligatorio | Descripción |
|---|---|:---:|---|
| `usuario` | `discord.Member` | Sí | Miembro del servidor al que se asignará el rol y actualizará el apodo. |
| `equipo` | `str` | Sí | Nombre del equipo oficial de la liga (ej. `"Vanguard Gaming"`) o el identificador de agente libre (`"Libre"`). |
| `nombre_lol` | `str` | Sí | Nombre del jugador en League of Legends. |
| `riot_tag` | `str` | Sí | Riot Tag del jugador (sin incluir el carácter `#`). |

- **Lógica de Bifurcación**:

#### Flujo 1: Asignación de Agente Libre (`equipo == settings.free_role_name`)
1. Verifica la presencia de `role_service`.
2. Delega en `role_service.assign_free_role(usuario, nombre_lol, riot_tag)`.
3. Retorna directamente la respuesta con el estado emitido por el servicio en formato efímero.

#### Flujo 2: Asignación de Equipo Oficial
1. **Validación de Contexto Guild**: Verifica que `interaction.guild` no sea nulo (bloquea su invocación por mensajes directos).
2. **Búsqueda del Rol**: Localiza el rol en el servidor mediante `discord.utils.get(interaction.guild.roles, name=equipo)`. Si no existe, aborta con mensaje efímero informativo.
3. **Retirada de 'Sin Verificar'**: Si `settings.sin_verificar_role_id > 0`, busca dicho rol en la guild. Si el usuario lo tiene asignado, ejecuta `await usuario.remove_roles(sin_verificar)`. Cualquier fallo de jerarquía se captura registrando una advertencia en log sin detener la asignación principal.
4. **Asignación del Rol de Equipo**:
   - Ejecuta `await usuario.add_roles(team_role)`.
   - Captura `discord.Forbidden` (falta de permisos o rol del bot inferior en la jerarquía) y `discord.HTTPException` (errores de API de Discord), informando con mensajes efímeros específicos.
5. **Normalización y Truncado de Apodo**:
   - Construye el apodo con el formato: `f"{nombre_lol} #{riot_tag}"[:32]`.
   - El truncado a 32 caracteres garantiza el cumplimiento del límite de longitud de apodos impuesto por la API de Discord.
   - Aplica el cambio con `await usuario.edit(nick=nick)`. Si se produce una excepción de jerarquía (por ejemplo, si el miembro es el propietario del servidor), se registra un aviso en log sin abortar la operación.
6. **Auditoría y Persistencia Transaccional**:
   - Abre una sesión mediante `async with transactional_session(role_service.session_factory) as session:`.
   - Registra la solicitud mediante `repo.create_request(..., canal_id=None)`.
   - Transiciona inmediatamente la solicitud a `RoleRequestStatus.APPROVED` vinculando el identificador del staff responsable (`staff_id = interaction.user.id`).
   - El bloque está aislado en `try/except Exception` para asegurar que un fallo en la base de datos no anule la confirmación si Discord ya procesó los roles con éxito.
7. **Confirmación Final**:
   - Emite confirmación efímera: `f"Rol {equipo} asignado a {usuario.display_name} correctamente."`.

---

## 4. Matriz de Errores y Excepciones en Comandos

| Comando | Excepción / Condición | Manejo / Mitigación | Efecto en Usuario |
|---|---|---|---|
| `/pedir-rol` | Invocación en canal restringido | Gestionado por la configuración de canales de Discord | El modal se abre en el cliente local |
| `/publicar-panel-rol` | Usuario sin rol de staff | Verificación `is_staff()` | Mensaje efímero de denegación |
| `/publicar-panel-rol` | Canal no soporta `.send` | Comprobación de atributo y tipo | Mensaje efímero de error de canal |
| `/asignar-rol` | Invocación fuera de servidor (DM) | Comprobación `interaction.guild is None` | Mensaje efímero `"❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord."` |
| `/asignar-rol` | Rol de equipo inexistente | Búsqueda por nombre en `guild.roles` | Mensaje efímero `"El rol '{equipo}' no existe en el servidor."` |
| `/asignar-rol` | Jerarquía insuficiente del Bot | Captura de `discord.Forbidden` en `add_roles` | Mensaje efímero `"Permisos insuficientes para asignar el rol..."` |
| `/asignar-rol` | Apodo superior a 32 caracteres | Truncado estricto `[:32]` | Apodo ajustado sin lanzar `HTTPException 400` |
| `/asignar-rol` | Fallo de conexión a BD | Captura de `Exception` en bloque transaccional | Advertencia en log; roles asignados en Discord |
