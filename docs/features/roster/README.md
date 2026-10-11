# Gestión de Plantillas y Sincronización (Roster Management)

[⬅️ Volver a WebSocket Bridge](../websocket-bridge/README.md) | [Siguiente: Roles y Onboarding ➡️](../roles/README.md)

---

## Resumen Ejecutivo

El módulo de **Gestión de Plantillas y Sincronización** (`roster`) gestiona el ciclo de vida de los jugadores y miembros del cuerpo técnico en los clubes oficiales de la liga. Su arquitectura conecta la asignación de roles en Discord con la base de datos relacional de la plataforma deportiva, garantizando la consistencia de datos, el cumplimiento estricto de las reglas de juego y la trazabilidad de los movimientos.

Entre sus responsabilidades clave se encuentran:
- **Sincronización Reactiva:** Detección en tiempo real de cambios en los roles de miembros de Discord (`on_member_update`) para gestionar altas y bajas de membresía de forma resiliente e idempotente.
- **Panel Interactivo de Gestión:** Comando slash `/gestionar-posicion` con vistas interactivas (`GestionarPosicionView`), menús desplegables para clubes y posiciones, y control de concurrencia restringido al staff.
- **Invariantes Deportivas de Liga:** Imposición de reglas reglamentarias, incluyendo la posición competitiva única por jugador en toda la competición y la limitación de la capitanía oficial exclusivamente a posiciones titulares.
- **Resolución Canónica de Apodos:** Algoritmo de formateo (`resolve_canonical_nick`) con prioridad deportiva competitiva sobre roles secundarios, normalizando tags y respetando el límite de 32 caracteres.
- **Trazabilidad y Auditoría:** Historial inmutable de movimientos en `roster_movements` y bitácoras estructuradas en `audit_logs` con snapshots JSONB de estados previos y posteriores.

---

## Tabla de Contenidos del Directorio

| Documento | Descripción |
|---|---|
| [**`commands.md`**](./commands.md) | Documenta los comandos slash `/gestionar-posicion`, `/traspasa-equipo` y `/liberar-jugador`, sus permisos de staff, deferral efímero, sincronización de apodos canónicos y el listener reactivo de Discord Gateway `on_member_update`. |
| [**`services.md`**](./services.md) | Detalla la lógica de dominio en `RosterSyncService` (`handle_role_added`, `handle_role_removed`, `change_player_position`, `get_user_teams`, `resolve_canonical_nick`), la jerarquía de excepciones de negocio y las invariantes deportivas. |
| [**`ui.md`**](./ui.md) | Describe los componentes visuales interactivos: `GestionarPosicionView`, auto-selección de equipo, selector de roles (`PositionSelect`), botones de acción (`SaveButton` con sincronización de apodo canónico, `CancelButton`) y generadores de embeds informativos. |
| [**`persistence.md`**](./persistence.md) | Detalla los modelos SQLAlchemy (`TeamMembership`, `RosterMovement`, `AuditLog`), las restricciones relacionales DDL (check de capitanía e índice parcial único), normalización JSONB y los repositorios de datos. |
