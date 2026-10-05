# Política de Autorización del Staff

[⬅️ Volver a Arquitectura](./README.md)

Este documento define qué roles y permisos de Discord cuentan como staff para cada tipo de acción. La política está concentrada en `has_staff_access` (`src/liga_bot/cogs/permissions.py:91-105`), que decide a partir de la tabla de roles que devuelve `allowed_role_ids` (`src/liga_bot/cogs/permissions.py:43-50`). Los tests `tests/test_staff_permissions_policy.py` recorren esta tabla completa.

---

## 1. Dos capas de control

1. **Visibilidad por defecto (`@app_commands.default_permissions(manage_guild=True)`):** Discord oculta el comando a quien no tiene «Gestionar servidor». Es solo un valor por defecto: el servidor puede cambiar quién ve cada comando desde *Ajustes del servidor → Integraciones*. No es una comprobación de autorización.
2. **Autorización en el bot:** antes de ejecutar nada, el comando llama a la función de la política correspondiente a su tipo de acción y responde con un mensaje efímero si el invocador no está autorizado.

Por tanto, un miembro con rol Staff, Admin o CEO que no tenga «Gestionar servidor» solo ve los comandos si el servidor los habilita para su rol en Integraciones.

## 2. Tabla de la política

| Tipo de acción (`StaffAction`) | Administrador nativo | «Gestionar servidor» sin rol | Rol Staff | Rol Admin | Rol CEO general¹ | CEO Premier / CEO Ascend |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `ROLES_Y_PLANTILLAS` | sí | no | sí | sí | sí | no |
| `CALENDARIO_Y_CASTERS` | sí | no | sí | sí | sí | sí |
| `TICKETS` | sí | no | sí | sí | sí | sí |
| `SINCRONIZACION` | sí | no | sí | sí | sí | no |

¹ Solo si `CEO_ROLE_ID` está configurado (distinto de `0`). Cualquier rol con ID `0` se considera no configurado y se ignora.

Reglas comunes:

- El permiso nativo de Administrador autoriza siempre (el propietario del servidor lo tiene implícitamente).
- «Gestionar servidor» no autoriza por sí solo en ninguna acción.
- Staff, Admin y CEO general forman el núcleo del staff y autorizan todas las acciones.
- Los CEO de división (Premier y Ascend) solo autorizan las acciones de calendario, casters y tickets.

## 3. Comandos por tipo de acción

| Tipo de acción | Función | Comandos e interacciones |
|---|---|---|
| `ROLES_Y_PLANTILLAS` | `is_staff` | `/asignar-rol`, `/publicar-panel-rol`, botones Confirmar y Denegar del ticket de rol, `/registrar-equipo`, `/gestionar-posicion`, `/traspasa-equipo`, `/liberar-jugador` |
| `CALENDARIO_Y_CASTERS` | `is_authorized_scheduler` | `/crear-partido`, `/importar-jornada`, `/crear-jornada`, `/stream_url`, `/stream_url_live`, `/panel-casters`, `/cartelera-casters` |
| `TICKETS` | `has_staff_access(..., StaffAction.TICKETS)` | `/revisar-tickets`, `/revisar-tickets-manual` |
| `SINCRONIZACION` | `is_staff_or_admin` | `/sync`, `/sincronizar`, `!sync` (comando de texto) |

`is_staff`, `is_authorized_scheduler` e `is_staff_or_admin` son envoltorios de `has_staff_access` con su tipo de acción fijo. Aceptan tanto una `discord.Interaction` como un `discord.Member`.

Los comandos `/pedir-rol` y `/equipos` y el botón del panel para pedir rol están abiertos a todos los miembros.

## 4. Resolución del miembro

`resolve_member` (`src/liga_bot/cogs/permissions.py:53-88`) obtiene el `discord.Member` del invocador:

1. Si recibe un `discord.Member`, lo devuelve.
2. Si `interaction.user` es un `discord.Member`, lo devuelve.
3. Si hay servidor, lo busca en la caché (`guild.get_member`) y, si no está, en la API (`guild.fetch_member`).
4. En mensajes directos o si la API falla, devuelve `None` y la autorización se deniega.

## 5. Conceptos relacionados que no son autorización

- **Respuesta del staff en la auditoría de tickets:** `TicketService.staff_role_ids` decide qué mensajes cuentan como respuesta del staff en un ticket (incluye CEO Premier, CEO Ascend y `ORGANIZADOR_ROLE_ID`). No decide quién puede ejecutar comandos. Ver [servicios de tickets](../features/tickets/services.md).
- **Botones del panel de casters:** exigen el rol de caster (`CASTER_ROLE_ID`) o, en su defecto, pasar `is_staff`. Ver [UI de casters](../features/casters/ui.md).
