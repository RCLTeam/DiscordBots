# Componentes de Interfaz: GestionarPosicionView y Selectores

[⬅️ Volver a Gestión de Plantillas](./README.md)

Este módulo describe los componentes visuales interactivos de Discord (vistas, selectores, botones y embeds) utilizados para la administración de plantillas de clubes deportivos.

- **Ubicación en código:** `src/liga_bot/ui/roster.py`

---

## 1. Vista Contenedora: `GestionarPosicionView`

`GestionarPosicionView` hereda de `discord.ui.View` y orquesta el panel de interacción para seleccionar el equipo objetivo, la posición deportiva deseada y aplicar los cambios validados.

- **Constructor (`L354–L394`):**
  ```python
  class GestionarPosicionView(discord.ui.View):
      def __init__(
          self,
          member: discord.Member,
          user_teams: list[tuple[Team, TeamMembership]],
          roster_sync_service: RosterSyncService,
          actor: discord.Member | discord.User,
          timeout: float | None = 180.0,
      ) -> None:
  ```

### 1.1 Auto-selección Inteligente de Equipo
Al inicializar la vista, evalúa la cantidad de clubes asociados al jugador:
```python
if len(user_teams) == 1:
    self.selected_team_id: UUID | None = user_teams[0][0].id
else:
    self.selected_team_id = None
```
- **Caso 1 solo equipo:** El sistema preselecciona automáticamente el club, permitiendo que el operador solo tenga que escoger el rol y pulsar guardar, reduciendo fricción operativa.
- **Caso ≥2 equipos:** Obliga al operador a seleccionar explícitamente el equipo en el menú desplegable antes de guardar.

### 1.2 Control de Concurrencia y Seguridad (`interaction_check`)
Implementa validación de autoría para impedir que terceros interfieran con el panel de administración:
```python
async def interaction_check(self, interaction: discord.Interaction) -> bool:
    if interaction.user.id == self.actor.id:
        return True
    await interaction.response.send_message(
        "⛔ No tienes autorización para interactuar con este panel de gestión de posiciones.",
        ephemeral=True,
    )
    return False
```
- Únicamente el usuario que invocó el comando slash original (`self.actor`) puede interactuar con los selectores y botones. Cualquier otro miembro del servidor (incluido el propio jugador inspeccionado) recibe un rechazo efímero.

### 1.3 Ciclo de Vida: Expiración y Gestión de Errores
- **`on_timeout` (`L407–L421`):** Tras 180 segundos sin interacción, el temporizador marca `child.disabled = True` en todos los controles y actualiza el mensaje en Discord para congelar la interfaz.
- **`on_error` (`L422–L438`):** Captura cualquier excepción no controlada en los callbacks de los componentes hijos, emite un log con trazabilidad completa y responde al usuario con un mensaje efímero de error.

---

## 2. Componentes Interactivos

La vista dispone sus controles en tres filas organizadas:

```
┌────────────────────────────────────────────────────────┐
│ [Row 0]  TeamSelect (Selector de Club)                │
├────────────────────────────────────────────────────────┤
│ [Row 1]  PositionSelect (Selector de Rol / Posición)  │
├────────────────────────────────────────────────────────┤
│ [Row 2]  [ 💾 Guardar Posición ]   [ ❌ Cancelar ]     │
└────────────────────────────────────────────────────────┘
```

### 2.1 Selector de Equipo: `TeamSelect` (`L42–L118`)
- **Fila:** `row=0`
- **Comportamiento sin equipos (`L53–L69`):** Si `user_teams` está vacío, renderiza una opción deshabilitada con placeholder `"Sin equipos asignados..."` y valor `"none"`.
- **Opciones de equipos (`L71–L84`):**
  - **Etiqueta:** `f"{team.name} [{team.tag}]"`
  - **Valor:** `str(team.id)`
  - **Emoji:** `"🛡️"`
  - **Descripción:** `f"Rol actual: {membership.role.value}"` (añadiendo `" (Capitán)"` si `is_captain=True`), truncada a 100 caracteres.
  - **Estado por defecto:** Se marca como seleccionada la opción cuyo ID coincide con `view.selected_team_id`.
- **Callback:** Actualiza `self.view.selected_team_id` con el `UUID` seleccionado, refresca el flag `default` en las opciones visuales y defiere la interacción.

---

### 2.2 Selector de Posición: `PositionSelect` (`L120–L184`)
- **Fila:** `row=1`
- **Catálogo de Opciones:** Presenta los 9 roles definidos en `RosterRole`:

| Rol (`RosterRole`) | Emoji | Categoría | Descripción Mostrada |
|---|:---:|---|---|
| `TOP` | ⚔️ | Titular Competitivo | Línea superior (Titular competitivo) |
| `JUNGLE` | 🌲 | Titular Competitivo | Jungla (Titular competitivo) |
| `MID` | 🧙 | Titular Competitivo | Línea central (Titular competitivo) |
| `ADC` | 🏹 | Titular Competitivo | Tirador / Bot (Titular competitivo) |
| `SUPPORT` | 🛡️ | Titular Competitivo | Apoyo / Soporte (Titular competitivo) |
| `SUBSTITUTE` | 🔄 | Suplente Competitivo | Suplente (Rol competitivo) |
| `COACH` | 📋 | No Competitivo | Entrenador (No competitivo, multiequipo) |
| `STAFF` | 💼 | No Competitivo | Cuerpo técnico / Staff (No competitivo) |
| `PARTNERS` | 🤝 | No Competitivo | Colaborador / Partner (No competitivo) |

- **Invariante Preventiva en Callback (`L179–L180`):**
  ```python
  self.view.selected_role = RosterRole(self.values[0])
  if not self.view.selected_role.is_starter():
      self.view.is_captain = False
  ```
  Si el rol elegido no corresponde a una de las 5 posiciones titulares, la vista apaga automáticamente el estado de capitanía antes de enviar la petición de guardado.

---

### 2.3 Botón de Confirmación: `SaveButton` (`L191–L304`)
- **Fila:** `row=2`
- **Estilo:** `discord.ButtonStyle.success`
- **Emoji:** `"💾"`
- **Custom ID:** `"roster:gestionar_posicion:save"`
- **Alias de importación:** `SavePositionButton = SaveButton` (`L304`)

#### Validaciones y Manejo de Errores:
1. **Validación de Selección Incompleta (`L210–L226`):**
   Comprueba que tanto `selected_team_id` como `selected_role` hayan sido seleccionados. Si falta alguno, emite un mensaje efímero indicando qué campos faltan:
   ```text
   ⚠️ Faltan campos por seleccionar: equipo, posición/rol. Por favor, selecciona ambos desplegables antes de guardar.
   ```
2. **Captura y Renderizado de Excepciones:**
   - **`CompetitivePositionConflictError` (`L240–L246`):** Envía el embed enriquecido de conflicto (`build_conflict_embed`).
   - **`InvalidCaptainRoleError` (`L247–L258`):** Notifica de forma efímera que solo las posiciones titulares pueden ostentar capitanía.
   - **`PlayerNotTeamMemberError` (`L259–L268`):** Notifica que el usuario ya no pertenece a la plantilla del equipo.
   - **`ValueError`, `RosterSyncError` (`L269–L275`):** Muestra el mensaje de la excepción en formato de alerta.
   - **`Exception` no controlada (`L276–L286`):** Registra el error en logs del bot y responde con un mensaje genérico.
3. **Flujo de Éxito (`L288–L302`):**
   - Deshabilita todos los componentes hijos de la vista.
   - Detiene la vista con `self.view.stop()`.
   - Genera el embed de éxito con `view.build_success_embed(updated_membership)`.
   - Actualiza el mensaje en Discord mediante `edit_original_response` o `edit_message`.

---

### 2.4 Botón de Cancelación: `CancelButton` (`L307–L342`)
- **Fila:** `row=2`
- **Estilo:** `discord.ButtonStyle.secondary`
- **Emoji:** `"❌"`
- **Custom ID:** `"roster:gestionar_posicion:cancel"`
- **Comportamiento:** Deshabilita todos los componentes, detiene la vista y reemplaza el mensaje con un embed neutro de color gris claro (`"Operación Cancelada"`), confirmando que no se realizaron modificaciones en la base de datos.

---

## 3. Generadores de Embeds

La vista centraliza la construcción de los mensajes enriquecidos:

### 3.1 `build_initial_embed` (`L440–L465`)
- **Color:** `discord.Color.blue()`
- **Contenido:**
  - Título: `"🛡️ Gestión de Posición de Plantilla"`
  - Thumbnail: Avatar del jugador auditado.
  - Campos: Mención del jugador (`<@id>`), etiqueta (`display_name`) y total de equipos vinculados.
  - Pie: `"RCL League • Selecciona el equipo y la posición deseada"`

### 3.2 `build_success_embed` (`L467–L535`)
- **Color:** `discord.Color.green()`
- **Contenido:**
  - Título: `"✅ Posición de Plantilla Actualizada"`
  - Timestamp: Hora UTC de la actualización.
  - Campos:
    - **Jugador:** Mención `<@id>`.
    - **Equipo:** Nombre y tag del club.
    - **Nueva Posición:** Rol asignado en negrita.
    - **Tipo de Posición:** `"Competitiva (Posición Única)"` o `"No Competitiva (Multiequipo)"`.
    - **Capitanía:** `"👑 Capitán Oficial"` o `"No"`.
    - **Modificado por:** Mención del actor que ejecutó la operación.
  - Pie: `"RCL League • Sincronización de Plantillas"`

### 3.3 `build_conflict_embed` (`L537–L560`)
- **Color:** `discord.Color.gold()`
- **Contenido:**
  - Título: `"⚠️ Conflicto de Posición Competitiva"`
  - Descripción: Explica que el reglamento prohíbe que un jugador ocupe más de una posición competitiva en toda la liga.
  - Campos:
    - **Equipo Actual:** UUID del equipo donde ya compite.
    - **Rol Actual:** Posición competitiva que ocupa actualmente.
    - **Equipo Solicitado:** UUID del equipo destino.
    - **Rol Intentado:** Posición competitiva que se intentó asignar.
    - **Acción Requerida:** Instrucciones para remover al jugador del equipo anterior o cambiarlo a rol no competitivo antes de proceder.
