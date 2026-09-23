# Comandos y Gateway: Gestión de Plantillas y Sincronización

Este módulo documenta los puntos de entrada para la administración de plantillas de clubes, abarcando el comando slash interactivo `/gestionar-posicion` y el listener pasivo de Discord Gateway `on_member_update` para la sincronización reactiva de altas y bajas de membresía.

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

1. **Restricción de Contexto Guild (`L156–L161`):**
   Verifica que `interaction.guild is not None`. No se permite la ejecución por mensajes directos (DM). En caso de detectarse fuera de un servidor, responde de forma efímera y aborta:
   ```text
   ❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.
   ```
2. **Permisos Nativos de Discord (`L148`):**
   A través del decorador `@app_commands.default_permissions(manage_guild=True)`, restringe la visibilidad del comando en clientes de Discord exclusivamente a usuarios con permiso de gestión de servidor.
3. **Doble Cerrojo de Autorización de Staff (`L164–L171`):**
   Ejecuta `is_staff(interaction.user, self.settings)`, validando contra `Settings`:
   - Rol de Staff (`staff_role_id`).
   - Rol de CEO / Dirección (`ceo_role_id`).
   - Permiso de Administrador de Discord (`administrator=True`).
   - Permiso de Gestión de Servidor (`manage_guild=True`).
   
   Soporta evaluación tanto síncrona como asíncrona mediante `inspect.isawaitable`. Si el invocador no supera la validación, se deniega la ejecución:
   ```text
   ❌ Solo el personal de staff tiene autorización para gestionar posiciones.
   ```
4. **Verificación de Disponibilidad del Servicio (`L174–L181`):**
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

### 1.4 Aclaración Técnica sobre Comandos Slash de Plantilla

En la arquitectura del bot:
- **No existe ningún comando de grupo `/roster`** (como `/roster gestionar`, `/roster sincronizar` o `/roster exportar`).
- La administración interactiva de posiciones de plantilla se canaliza única y exclusivamente a través de `/gestionar-posicion`.
- La sincronización de altas y bajas de miembros no es un comando manual, sino un proceso reactivo automático ejecutado en segundo plano por el listener de Discord Gateway.

---

## 2. Event Listener: Sincronización Reactiva (`on_member_update`)

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

### 2.1 Detección Diferencial de Roles

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

### 2.2 Aislamiento de Excepciones y Resiliencia en Lote

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

## 3. Registro y Carga del Cog

- **Módulo Principal:** `src/liga_bot/cogs/roster.py`
- **Punto de Carga Estándar:**
  ```python
  async def setup(bot: LigaBot | commands.Bot) -> None:
      """Carga la extensión con guard de idempotencia."""
      if "Roster" not in bot.cogs:
          await bot.add_cog(RosterCog(bot))
  ```
- **Alias de Compatibilidad:** `src/liga_bot/cogs/roster_cog.py` reexporta directamente `RosterCog` y `setup` para mantener compatibilidad con rutas de importación de extensiones heredadas.
