# Comandos y Gateway: Gestión de Plantillas y Sincronización

[⬅️ Volver a Gestión de Plantillas](./README.md)

Este módulo documenta los puntos de entrada para la administración de plantillas de clubes, abarcando los comandos slash interactivos `/gestionar-posicion`, `/traspasa-equipo` y `/liberar-jugador`, así como el listener pasivo de Discord Gateway `on_member_update` para la sincronización reactiva de altas y bajas de membresía.

---

## 1. Slash Command: `/gestionar-posicion`

El comando `/gestionar-posicion` permite al personal de administración y staff inspeccionar y modificar la asignación de posiciones de plantilla y la capitanía oficial de un jugador en los clubes a los que pertenece.

- **Ubicación en código:** `src/liga_bot/cogs/roster.py:141-237`
- **Firma del método:**
  ```python
  @app_commands.command(
      name="gestionar-posicion",
      description="Gestiona la posición y rol de plantilla de un jugador en sus equipos",
  )
  @app_commands.describe(
      member="Miembro del servidor cuya posición de plantilla se desea gestionar",
  )
  @app_commands.default_permissions(manage_guild=True)
  async def gestionar_posicion(
      self,
      interaction: discord.Interaction,
      member: discord.Member,
  ) -> None:
  ```

### 1.1 Parámetros de Entrada

| Parámetro | Tipo | Requerido | Descripción |
|---|---|:---:|---|
| `member` | `discord.Member` | Sí | Miembro del servidor de Discord cuya membresía y posición de plantilla se desea consultar o modificar. |

### 1.2 Restricciones de Seguridad y Permisos

El comando implementa un filtrado multinivel antes de acceder a la capa de datos:

1. **Restricción de Contexto Guild (`L158–L163`):**
   Verifica que `interaction.guild is not None`. No se permite la ejecución por mensajes directos (DM). En caso de detectarse fuera de un servidor, responde de forma efímera y aborta:
   ```text
   ❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.
   ```
2. **Permisos Nativos de Discord (`L150`):**
   A través del decorador `@app_commands.default_permissions(manage_guild=True)`, restringe la visibilidad del comando en clientes de Discord exclusivamente a usuarios con permiso de gestión de servidor.
3. **Doble Cerrojo de Autorización de Staff (`L166–L172`):**
   Ejecuta `is_staff(interaction.user, self.settings)` (acción `ROLES_Y_PLANTILLAS` de la [política de autorización](../../architecture/permissions.md)), que autoriza a:
   - Permiso de Administrador de Discord (`administrator=True`).
   - Rol de Staff (`staff_role_id`).
   - Rol de Admin (`admin_role_id`).
   - Rol de CEO general (`ceo_role_id`, si está configurado).

   El permiso «Gestionar servidor» (`manage_guild=True`) por sí solo no autoriza: solo decide la visibilidad del comando. Si el invocador no supera la validación, se deniega la ejecución:
   ```text
   ❌ Solo el personal de staff tiene autorización para gestionar posiciones.
   ```
4. **Verificación de Disponibilidad del Servicio (`L176–L181`):**
   Comprueba que `self.roster_sync_service` esté inicializado en la instancia del bot. Si es `None`, aborta la interacción informando el estado:
   ```text
   ❌ El servicio de sincronización de plantillas no está disponible.
   ```
5. **Diferimiento Efímero (`L183`):**
   Ejecuta `await interaction.response.defer(ephemeral=True)`. Esto otorga una ventana extendida de hasta 15 minutos frente al timeout nativo de 3 segundos de Discord Gateway mientras se consultan las membresías en la base de datos.
6. **Resolución Defensiva del Miembro (`L185`):**
   Utiliza `resolve_member(member)` para asegurar que se cuenta con el objeto `discord.Member` completo y su lista de roles activos, incluso si la interacción entregó un objeto parcial `discord.User`.

---

### 1.3 Flujo de Bifurcación en Respuesta

Tras recuperar las membresías activas mediante `await service.get_user_teams(str(target_member.id))`, el flujo se bifurca según la cantidad de equipos encontrados:

#### Caso A: 0 Equipos Registrados (`L205–L222`)
Si el usuario no posee membresías activas en la base de datos:
- Se genera un embed informativo con color `discord.Color.orange()`, título `"🛡️ Sin Equipos Registrados"`, descripción explicativa, thumbnail del avatar del usuario y pie `"RCL League • Sincronización de Plantillas"`.
- Se envía mediante `interaction.followup.send(embed=empty_embed, ephemeral=True)`.
- **Condición clave:** **No se adjunta ninguna vista interactiva (`view=None`)**, finalizando la interacción sin desplegar componentes interactivos en blanco.

```text
El usuario @Jugador (NombreGlobal) no pertenece a la plantilla de ningún equipo registrado en la liga.

Para gestionar su posición competitiva o rol en plantilla, primero debe tener asignado al menos un rol de equipo oficial en Discord.
```

#### Caso B: 1 o más Equipos Registrados (`L225–L236`)
Si el usuario está registrado en al menos un equipo:
- Se instancia la vista interactiva `GestionarPosicionView`, pasando el miembro objetivo, la lista de tuplas `(Team, TeamMembership)`, la referencia al servicio `RosterSyncService` y el actor invocador (`interaction.user`).
- Se envía el mensaje efímero con el embed inicial construido por `view.build_initial_embed()`.
- Se vincula el mensaje devuelto en `view.message = msg` para permitir el ciclo de vida y la desactivación visual automática de componentes ante expiración por timeout.

---

## 2. Slash Command: `/traspasa-equipo`

El comando `/traspasa-equipo` traspasa a un jugador hacia otro club, actualizando su posición en base de datos, intercambiando sus roles de equipo en Discord y sincronizando su apodo canónico según la prioridad deportiva competitiva.

- **Ubicación en código:** `src/liga_bot/cogs/roster.py:243-364`
- **Firma del método:**
  ```python
  @app_commands.command(
      name="traspasa-equipo",
      description="Traspasa a un jugador al equipo indicado con su nueva posición (Solo Staff)",
  )
  @app_commands.describe(
      usuario="Jugador que se traspasa",
      equipo="Rol de Discord del equipo de destino",
      posicion="Posición que ocupará en la plantilla",
      nombre_lol="Nombre de invocador para el apodo (opcional: por defecto el actual)",
  )
  @app_commands.default_permissions(manage_guild=True)
  async def traspasa_equipo(
      self,
      interaction: discord.Interaction,
      usuario: discord.Member,
      equipo: discord.Role,
      posicion: RosterRole,
      nombre_lol: str | None = None,
  ) -> None:
  ```

### 2.1 Parámetros de Entrada

| Parámetro | Tipo | Requerido | Descripción |
|---|---|:---:|---|
| `usuario` | `discord.Member` | Sí | Jugador que es traspasado al club objetivo. |
| `equipo` | `discord.Role` | Sí | Rol de Discord correspondiente al equipo de destino registrado. |
| `posicion` | `RosterRole` | Sí | Posición deportiva que ocupará en la plantilla (`TOP`, `JUNGLE`, `MID`, `ADC`, `SUPPORT`, `SUBSTITUTE`, `COACH`, `STAFF`, `PARTNERS`). |
| `nombre_lol` | `str \| None` | No | Nombre de invocador para el apodo. Si se omite, se usa el `display_name` actual del usuario. |

### 2.2 Flujo Operativo

1. **Base de Datos Primero (`L291–L313`):**
   Invoca `await service.transfer_player(...)`. La capa de datos gestiona el movimiento de forma transaccional (`roster.member_transferred_in`, `roster_movements`), asegurando que si ocurre un error o conflicto, la operación se revierte y no se tocan roles ni apodos en Discord.
2. **Sincronización de Roles en Discord (`L315–L331`):**
   - Si existía procedencia de un equipo anterior (`previous_team`), se localiza su rol en el servidor y se retira (`remove_roles`).
   - Se asigna el rol del equipo de destino si el jugador no lo poseía (`add_roles`).
   - Excepciones de jerarquía (`discord.Forbidden`, `discord.HTTPException`) se capturan y añaden a la lista de `avisos`.
3. **Apodo Canónico según Prioridad Deportiva Competitiva (`L333–L346`):**
   - Determina el nombre base: `(nombre_lol or "").strip() or target_member.display_name`.
   - Invoca `await service.resolve_canonical_nick(discord_user_id=target_member.id, base_name=base_nick)`.
   - Si la posición asignada es competitiva (`TOP`, `JUNGLE`, etc.), se antepone el tag del club (`<TAG> <NombreLoL>`).
   - Si la posición es no competitiva (`PARTNERS`, `COACH`, `STAFF`):
     - Si el jugador ostenta otra posición competitiva en la liga, conserva dicho tag competitivo.
     - Si no posee ninguna membresía competitiva, queda limpio sin tag de equipo.
   - En caso de fallo imprevisto en la resolución, se aplica defensivamente `apply_team_tag` con los tags conocidos.
   - Aplica el cambio con `await target_member.edit(nick=nuevo_nick)` capturando excepciones de permisos.
4. **Respuesta de Confirmación (`L347–L354`):**
   Emite un mensaje efímero detallando procedencia, equipo de destino, posición y apodo (`✅ @Jugador traspasado desde Equipo A a Equipo B como MID (TAG Nombre).`). Si hubo incidencias en roles o apodo, añade advertencias en `avisos`.

---

## 3. Slash Command: `/liberar-jugador`

El comando `/liberar-jugador` da de baja a un jugador de la plantilla de un equipo sin eliminar su ficha histórica ni cuenta de invocador.

- **Ubicación en código:** `src/liga_bot/cogs/roster.py:366-514`
- **Firma del método:**
  ```python
  @app_commands.command(
      name="liberar-jugador",
      description="Saca a un jugador de la plantilla del equipo indicado (Solo Staff)",
  )
  @app_commands.describe(
      equipo="Rol de Discord del equipo del que se libera al jugador",
      usuario="Jugador al que se libera",
  )
  @app_commands.default_permissions(manage_guild=True)
  async def liberar_jugador(
      self,
      interaction: discord.Interaction,
      equipo: discord.Role,
      usuario: discord.Member,
  ) -> None:
  ```

### 3.1 Flujo Operativo

1. **Baja en Base de Datos (`L405–L435`):**
   Ejecuta `await service.handle_role_removed(member=target_member, role=equipo, actor_id=interaction.user.id)`. Si el jugador no figuraba en la plantilla, aborta con aviso sin alterar Discord.
2. **Retirada de Rol (`L439–L445`):**
   Retira el rol de Discord del equipo en el servidor.
3. **Evaluación de Plantillas Restantes (`L447–L476`):**
   Consulta `await service.get_user_teams(target_member.id)`:
   - **Conserva otras plantillas:** No pasa a agente libre. Su apodo adopta el tag del equipo restante que prevalece por orden alfabético.
   - **Sin otras plantillas (`L478–L504`):** Se le asigna el rol de agente libre (`settings.free_role_name`, ej. `Libre`) y se limpia su apodo eliminando tags con `strip_team_tag`.

---

## 4. Event Listener: Sincronización Reactiva (`on_member_update`)

El bot escucha activamente los cambios de estado en los miembros del servidor para sincronizar en tiempo real las membresías de los clubes deportivos en la base de datos relacional.

- **Ubicación en código:** `src/liga_bot/cogs/roster.py:64-136`
- **Firma del listener:**
  ```python
  @commands.Cog.listener()
  async def on_member_update(
      self,
      before: discord.Member,
      after: discord.Member,
  ) -> None:
  ```

### 4.1 Detección Diferencial de Roles

El evento calcula la diferencia simétrica entre los roles previos y los posteriores al evento:

```python
before_roles = getattr(before, "roles", [])
after_roles = getattr(after, "roles", [])

added_roles = [r for r in after_roles if r not in before_roles]
removed_roles = [r for r in before_roles if r not in after_roles]

if not added_roles and not removed_roles:
    return
```

- **Filtrado Anticipado:** Si el evento `on_member_update` fue disparado por cambios que no involucran roles (como cambio de apodo, actualización de avatar, inicio de streaming o cambio en actividades de voz), el listener retorna inmediatamente sin realizar consultas a la base de datos.

### 4.2 Aislamiento de Excepciones y Resiliencia en Lote

Cuando un usuario recibe o pierde múltiples roles simultáneamente (por ejemplo, mediante una asignación administrativa en bloque):

1. **Procesamiento de Altas (`L92–L112`):**
   - Itera secuencialmente sobre `added_roles`.
   - Cada rol se procesa dentro de un bloque `try/except Exception` independiente.
   - Invoca `await service.handle_role_added(member=after, role=role)`.
   - Si el rol corresponde a un equipo oficial, crea la membresía con rol no competitivo (`RosterRole.STAFF`) y registra en log de nivel `INFO`.
   - Si el rol no corresponde a ningún club registrado, el servicio retorna `None` silenciosamente sin alterar la base de datos.
   - Si ocurre una excepción inesperada durante el procesamiento de un rol específico, se registra en el log con `logger.error(..., exc_info=True)` y el bucle continúa inmediatamente con el siguiente rol añadido, evitando bloqueos en cascada.

2. **Procesamiento de Bajas (`L114–L135`):**
   - Itera secuencialmente sobre `removed_roles`.
   - Cada desasignación de rol se procesa dentro de un bloque `try/except Exception` independiente.
   - Invoca `await service.handle_role_removed(member=after, role=role)`.
   - Si el rol correspondía a un club donde el usuario poseía membresía activa, elimina el registro, asienta el historial de movimientos y registra en log de nivel `INFO`.
   - Cualquier excepción en la desasignación de un rol queda contenida en su bloque correspondiente, continuando con el resto de roles retirados.

---

## 5. Registro y Carga del Cog

- **Módulo Principal:** `src/liga_bot/cogs/roster.py`
- **Punto de Carga Estándar (`src/liga_bot/cogs/roster.py:517-521`):**
  ```python
  async def setup(bot: LigaBot | commands.Bot) -> None:
      """Carga la extensión con guard de idempotencia."""
      if "Roster" not in bot.cogs:
          await bot.add_cog(RosterCog(bot))
  ```
