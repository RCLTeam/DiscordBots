[⬅️ Volver a Plantillas](../roster/README.md) | [Siguiente: Tickets y Moderación ➡️](../tickets/README.md)

---

# Roles e Incorporación (Roles & Onboarding)

El subsistema de **Roles e Incorporación** gestiona de forma integral la bienvenida de nuevos miembros, el protocolo de solicitud deportiva de equipo, la asignación automática de agentes libres y la tramitación administrativa mediante canales de tickets privados con interacción visual y persistencia relacional.

El diseño sigue una arquitectura desacoplada en tres capas que asegura aislamiento transaccional, tolerancia a fallos en la interacción con Discord y coherencia estricta de datos en PostgreSQL.

---

## Índice de Documentación del Módulo

| Documento | Descripción Técnica |
|---|---|
| [Comandos (`commands.md`)](./commands.md) | Especificación de comandos slash (`/pedir-rol`, `/publicar-panel-rol`, `/asignar-rol` con saneamiento de Riot Tag), permisos por defecto, controles en runtime (`is_staff`) y oyente `on_member_join`. |
| [Servicios de Dominio (`services.md`)](./services.md) | Lógica de negocio en `RoleService`: saneamiento defensivo de Riot Tag, creación de canales privados, jerarquía de permisos, prevención de tickets duplicados, rollback atómico anti-canales huérfanos, confirmación de tickets y asignación directa de equipo. |
| [Componentes de Interfaz (`ui.md`)](./ui.md) | Componentes visuales interactivos: modales (`SolicitudRolModal` con normalización de Riot Tag y límites de 3 a 5 caracteres), selector de equipos con límites de longitud de Discord (`EquipoSelect`), selector de posición con rollback del ticket (`PosicionSelect`), vistas persistentes y botones dinámicos (`ConfirmarRolButton`). |
| [Persistencia Relacional (`persistence.md`)](./persistence.md) | Esquema relacional de la tabla `role_requests`, tipos BigInteger para Snowflakes, ciclo de vida con enum nativo `RoleRequestStatus` e índice B-Tree. |

---

## Arquitectura del Subsistema

```
 ┌────────────────────────────────────────────────────────┐
 │                   Capa de Discord UI                   │
 │   - PanelPedirRolView & SolicitudRolModal              │
 │   - EquipoSelectView (21 opciones: 20 equipos + Libre) │
 │   - TicketView & ConfirmarRolButton (DynamicItem)      │
 └──────────────────────────┬─────────────────────────────┘
                            │
                            ▼
 ┌────────────────────────────────────────────────────────┐
 │                 Capa de Controladores                  │
 │   - RolesCog (/pedir-rol, /asignar-rol, /publicar)     │
 │   - Event Listener: on_member_join                     │
 └──────────────────────────┬─────────────────────────────┘
                            │
                            ▼
 ┌────────────────────────────────────────────────────────┐
 │                 Capa de Dominio y Core                 │
 │   - RoleService (Orquestación del ciclo de vida)       │
 │   - Rollback atómico: borrado de canal si falla la BD  │
 │   - Truncado estricto de apodo a 32 caracteres         │
 └──────────────────────────┬─────────────────────────────┘
                            │
                            ▼
 ┌────────────────────────────────────────────────────────┐
 │                  Capa de Persistencia                  │
 │   - RoleRequestRepository & transactional_session      │
 │   - Modelo RoleRequest & Enum PENDING/APPROVED/DENIED  │
 │   - Migración Alembic 002 (PostgreSQL nativo)          │
 └────────────────────────────────────────────────────────┘
```

---

## Garantías Operativas Principales

1. **Rollback Atómico de Canales**: Si la inserción en la base de datos falla al abrir un ticket, o no se puede publicar el mensaje con los botones, el canal creado en Discord se destruye vía `channel.delete()` (y, en el segundo caso, se elimina la solicitud pendiente), evitando canales huérfanos y jugadores bloqueados.
2. **Resiliencia ante Reinicios (`DynamicItem`)**: El botón de confirmación de rol (`ConfirmarRolButton`) almacena el ID del solicitante en su `custom_id` (`confirmar_rol:{user_id}`, sin el nombre del equipo para no superar los 100 caracteres de Discord), permitiendo que cualquier botón creado antes de un reinicio del bot continúe operando sin recarga de memoria.
3. **Control Estricto de Apodos**: El apodo es `<TAG> <NombreLoL>` al entrar en un equipo (ticket confirmado o `/asignar-rol`) y el nombre de invocador para Libre, truncado a 32 caracteres (`[:32]`) por el límite de la API de Discord.
4. **Ciclo de Vida Determinista**: Las solicitudes transicionan únicamente entre `PENDING`, `APPROVED` y `DENIED`, evitando inconsistencias con el motor relacional.
5. **Solo Equipos Registrados**: Tanto la confirmación de tickets como `/asignar-rol` resuelven el rol por el `discord_role_id` del equipo registrado; nunca se asigna un rol del servidor por coincidencia de nombre.
6. **Sanitización Defensiva de Riot Tag**: Tanto en el modal `SolicitudRolModal` como en el comando administrativo `/asignar-rol` y en los métodos de servicio (`RoleService.assign_free_role`, `RoleService.create_role_request_ticket`, `RoleService.assign_team_role` y `RosterSyncService.ensure_player`), el Riot Tag se sanea mediante `normalize_riot_tag` purgando cualquier carácter `#` y espacios residuales, garantizando que tanto `RoleRequest` como `Player` almacenen únicamente tags alfanuméricos limpios de 3 a 5 caracteres según el estándar oficial de Riot Games.
