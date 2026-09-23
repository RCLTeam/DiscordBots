# Comandos Slash: Administración y Vigilancia de Tickets

Este módulo documenta los comandos de barra diagonal (*slash commands*) de administración general y vigilancia de tickets en `DiscordBots`. Detalla los puntos de entrada expuestos en `AdminCog` y `TicketsCog`, sus niveles de autorización, control de límites en la API de Discord y el dictamen fáctico que refuta comandos inexistentes o rutas fuera de la interfaz de chat.

---

## 1. Sincronización del Árbol de Comandos: `/sync` y `/sincronizar`

La sincronización de comandos de aplicación (*Application Commands*) registra el árbol de comandos slash definidos en el bot ante la API de Discord, haciéndolos visibles en los clientes de los usuarios.

- **Ubicación en código:** `src/liga_bot/cogs/admin.py:23-146`
- **Comando Canónico:** `/sync` (`L114-129`)
- **Alias Localizado en Español:** `/sincronizar` (`L131-146`)
- **Firma de los Métodos:**
  ```python
  @app_commands.command(
      name="sync",
      description="Sincroniza el árbol de slash commands del bot",
  )
  @app_commands.describe(
      guild_id="ID de servidor específico a sincronizar (opcional)",
      global_sync="Si es True, sincroniza globalmente todos los comandos (opcional)",
  )
  async def sync(
      self,
      interaction: discord.Interaction,
      guild_id: str | None = None,
      global_sync: bool = False,
  ) -> None:
      await self._sync_impl(interaction, guild_id=guild_id, global_sync=global_sync)
  ```

### 1.1 Parámetros de Entrada

| Parámetro | Tipo | Requerido | Valor por Defecto | Descripción |
|---|---|:---:|:---:|---|
| `guild_id` | `str \| None` | No | `None` | Snowflake ID numérico del servidor de Discord específico cuyo árbol de comandos se desea sincronizar. Si se omite, se utiliza el servidor de la interacción o el configurado en el bot. |
| `global_sync` | `bool` | No | `False` | Si es `True`, registra los comandos a nivel global en todos los servidores donde opera el bot (sujeto a la propagación diferida de la caché de Discord). |

### 1.2 Restricciones de Seguridad y Permisos

La ejecución está protegida por la función `is_staff_or_admin` (`src/liga_bot/cogs/permissions.py:115-133`):

1. **Resolución de Miembro (`permissions.py:24-60`):**
   Obtiene el objeto `discord.Member` mediante la caché local de Discord (`guild.get_member`) o mediante una consulta asíncrona al Gateway (`await guild.fetch_member`).
2. **Permisos de Administrador Nativo:**
   Si el miembro posee el permiso nativo `administrator=True` en sus `guild_permissions`, se concede el acceso de inmediato.
3. **Pertenencia a Roles de Configuración:**
   Si no es administrador nativo, sus roles asignados deben intersectar con al menos uno de los roles autorizados en `Settings`:
   - `settings.staff_role_id`
   - `settings.admin_role_id`
4. **Respuesta ante Fallo de Autorización (`admin.py:41-47`):**
   Si el invocador no cumple ninguna de las condiciones anteriores, la interacción responde de forma efímera y se cancela la ejecución:
   ```text
   ❌ No tienes permisos para sincronizar comandos (se requiere Staff o Administrador).
   ```

### 1.3 Flujo de Ejecución y Diferimiento Efímero

Para evitar que la interacción expire por el límite estricto de 3 segundos de Discord (*Interaction Timeout*), el comando difiere la respuesta de forma efímera antes de invocar la API:

1. **Diferimiento:** `await interaction.response.defer(ephemeral=True)` (`L49`).
2. **Determinación del Alcance (`L52-78`):**
   - **Alcance Global (`global_sync=True`):** Ejecuta `await self.bot.tree.sync(guild=None)`. Alcance reportado: `"Global (todos los servidores)"`.
   - **Servidor Explícito (`guild_id` presente):** Valida que el texto sea un entero válido mediante `int(guild_id.strip())`. Si la conversión lanza `ValueError`, responde con error efímero `"❌ El ID de servidor proporcionado debe ser un número entero válido."` y aborta. El destino se instancia como `discord.Object(id=target_id)`.
   - **Servidor Actual (`interaction.guild is not None`):** Sincroniza directamente sobre la instancia del servidor actual.
   - **Servidor por Defecto:** Utiliza `discord.Object(id=self.settings.guild_id)` configurado en las variables de entorno.
3. **Invocación:** `synced = await self.bot.tree.sync(guild=target_guild)` (`L78`).

### 1.4 Formato de Respuesta y Protección contra Límites de Embed

Discord impone un límite estricto de 1.024 caracteres por campo en los objetos `discord.Embed`. La respuesta formatea la lista de comandos sincronizados aplicando un truncado defensivo:

- **Embed de Éxito (`L80-92`):**
  - **Color:** Verde (`discord.Color.green()`).
  - **Título:** `"🔄 Árbol de Comandos Sincronizado"`.
  - **Descripción:** `"Se han sincronizado satisfactoriamente {len(synced)} comando(s)."`.
  - **Campo "Alcance de Sincronización":** Descripción del destino.
  - **Campo "Comandos Registrados":** Concatena los nombres con formato `` `/comando` `` separados por comas. Si la longitud de la cadena acumulada supera 1.020 caracteres, se recorta a 1.000 caracteres añadiendo el sufijo `" ... (truncado)"` (`L88-90`).
- **Control de Excepciones de Discord:**
  - `discord.Forbidden`: Captura fallos de privilegios del bot y muestra un Embed rojo con `"❌ Error de Permisos en Discord"`.
  - `discord.HTTPException`: Captura errores de red o cuotas de Discord y muestra un Embed rojo con `"❌ Error de Discord API"`.

---

## 2. Auditoría Manual de Tickets: `/revisar-tickets` y `/revisar-tickets-manual`

Estos comandos ejecutan una inspección bajo demanda de todos los canales de tickets del servidor para detectar inactividad del staff superior a 24 horas, despachar alertas y actualizar el estado en base de datos sin necesidad de esperar a la ejecución del bucle automático diario.

- **Ubicación en código:** `src/liga_bot/cogs/tickets.py:173-214`
- **Comando Canónico:** `/revisar-tickets` (`L173-206`)
- **Alias Manual:** `/revisar-tickets-manual` (`L207-214`)
- **Firma de los Métodos:**
  ```python
  @app_commands.command(
      name="revisar-tickets",
      description="Fuerza la revisión inmediata de tickets inactivos sin esperar a las 24h",
  )
  @app_commands.default_permissions(manage_guild=True)
  async def revisar_tickets(self, interaction: discord.Interaction) -> None: ...


  @app_commands.command(
      name="revisar-tickets-manual",
      description="Alias manual para forzar la revisión inmediata de tickets inactivos",
  )
  @app_commands.default_permissions(manage_guild=True)
  async def revisar_tickets_manual(self, interaction: discord.Interaction) -> None:
      await self.revisar_tickets.callback(self, interaction)
  ```

### 2.1 Restricciones de Seguridad y Permisos

1. **Contexto de Servidor (`L180-185`):**
   Comprueba que `interaction.guild is not None`. Si se invoca por mensaje directo (DM), responde de forma efímera:
   ```text
   ❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.
   ```
2. **Permisos Nativos en el Cliente (`L177`):**
   El decorador `@app_commands.default_permissions(manage_guild=True)` oculta el comando en la interfaz a usuarios sin privilegios de administración del servidor.
3. **Autorización Ampliada (`is_authorized_scheduler`, `permissions.py:167-190`):**
   Comprueba que el usuario sea administrador nativo o posea al menos uno de los siguientes roles configurados en `Settings`:
   - `staff_role_id`
   - `admin_role_id`
   - `ceo_premier_role_id`
   - `ceo_ascend_role_id`
   
   Si no supera la verificación, responde efímeramente (`L188-192`):
   ```text
   No tienes permiso para usar este comando.
   ```

### 2.2 Flujo de Ejecución y Embed de Resultados

1. **Diferimiento:** `await interaction.response.defer(ephemeral=True)` (`L194`).
2. **Invocación del Servicio:** `result = await self.ticket_service.check_tickets(interaction.guild)` (`L197`).
3. **Construcción del Embed (`_build_audit_embed`, `L116-171`):**
  - **Color:** Verde (`discord.Color.green()`) si `alerts_sent == 0`, o Dorado (`discord.Color.gold()`) si hubo canales notificados.
  - **Título:** `"🔍 Auditoría de Inactividad de Tickets"`.
  - **Campo "📊 Resumen General":** Muestra canales auditados, categorías revisadas y avisos enviados.
  - **Campo "⏭️ Canales Descartados / En Regla":** Desglose de canales con actividad reciente (<24h), respondidos por staff, ya notificados (<24h), vacíos y con error o falta de permisos.
  - **Campo "🚨 Tickets Notificados" (Condicional si `alerts_sent > 0`):**
    - Muestra hasta 15 menciones de canal con formato `• <#channel_id> (channel_name)`.
    - Si hay más de 15 canales, agrega la línea `\n*... y {more} ticket(s) más.*`.
    - **Protección estricta de 1.024 caracteres:** Si el contenido del campo supera 1.020 caracteres, trunca preservando espacio para el sufijo `\n*... (truncado por límite de Discord)*` y aplica un corte de seguridad en `val[:1024]`.
  - **Pie de Página:** Indica el nombre del autor de la auditoría y marca de tiempo UTC.

---

## 3. Dictamen Fáctico y Refutación de Comandos Hipotéticos

En análisis arquitectónicos preliminares se mencionaron diversas variantes de comandos de administración y mantenimiento. La siguiente matriz certifica el estado real en el código fuente vivo:

| Comando / Ruta Hipotética | Estado en Código Vivo | Implementación Real / Archivo y Línea | Dictamen Técnico |
|---|---|---|---|
| `/admin clean-channels` | **INEXISTENTE** | No existe comando slash de borrado masivo de canales. El rollback de canales huérfanos se ejecuta de forma programática en `RoleService.create_role_request_ticket` (`src/liga_bot/services/role_service.py:292`) y en `ScheduleService.provision_match_channel` (`src/liga_bot/services/schedule_service.py:351`). El cierre de canales resueltos se efectúa en los botones de interacción de `src/liga_bot/ui/roles.py:336, 432`. | **Refutado.** La eliminación de canales huérfanos es un mecanismo transaccional de los servicios, no un comando expuesto a usuarios. |
| `/admin sync-commands` | **INEXISTENTE** | Los comandos de sincronización están registrados en la raíz del árbol como `/sync` (`src/liga_bot/cogs/admin.py:114`) y `/sincronizar` (`src/liga_bot/cogs/admin.py:131`). No existe un grupo de comandos con prefijo `/admin`. | **Refutado.** Los comandos son `/sync` y `/sincronizar` en el nivel superior. |
| `/admin health` | **INEXISTENTE** como slash command | No existe ningún comando Discord de salud. La verificación de salud es un endpoint HTTP REST `GET /health` expuesto por `WebSocketBridgeService` (`src/liga_bot/services/websocket_bridge_service.py:81, 142-144`) en el puerto local del bridge HTTP (`127.0.0.1:{bridge_port}/health`). | **Refutado.** Corresponde a una ruta HTTP del servicio de pasarela WebSocket, no a un comando slash. |
| Cierre automático de tickets a las 24h | **INEXISTENTE** | `TicketService` **no** borra ni cierra canales de tickets automáticamente. Su única función es detectar inactividad del staff $\ge 24\text{h}$, despachar un mensaje de alerta mencionando roles y marcar `ticket_notices.is_pending_staff = True`. | **Refutado.** El servicio vigila y alerta, no destruye canales. |
