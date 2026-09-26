[⬅️ Volver a Plantillas](../roster/README.md) | [Siguiente: Tickets y Moderación ➡️](../tickets/README.md)

---

# Roles e Incorporación (Roles & Onboarding)

El subsistema de **Roles e Incorporación** gestiona de forma integral la bienvenida de nuevos miembros, el protocolo de solicitud deportiva de equipo, la asignación automática de agentes libres y la tramitación administrativa mediante canales de tickets privados con interacción visual y persistencia relacional.

El diseño sigue una arquitectura desacoplada en tres capas que asegura aislamiento transaccional, tolerancia a fallos en la interacción con Discord y coherencia estricta de datos en PostgreSQL.

---

## Índice de Documentación del Módulo

| Documento | Descripción Técnica |
|---|---|
| [Comandos (`commands.md`)](./commands.md) | Especificación de comandos slash (`/pedir-rol`, `/publicar-panel-rol`, `/asignar-rol`), permisos por defecto, controles en runtime (`is_staff`) y oyente `on_member_join`. |
| [Servicios de Dominio (`services.md`)](./services.md) | Lógica de negocio en `RoleService`: creación de canales privados, jerarquía de permisos, prevención de tickets duplicados y rollback atómico anti-canales huérfanos. |
| [Componentes de Interfaz (`ui.md`)](./ui.md) | Componentes visuales interactivos: modales (`SolicitudRolModal`), selector de 21 opciones (`EquipoSelect`), vistas persistentes y botones dinámicos (`ConfirmarRolButton`). |
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

1. **Rollback Atómico de Canales**: Si la inserción en la base de datos falla al abrir un ticket, el canal creado en Discord se destruye de inmediato vía `channel.delete()`, evitando canales huérfanos desvinculados de la persistencia.
2. **Resiliencia ante Reinicios (`DynamicItem`)**: El botón de confirmación de rol (`ConfirmarRolButton`) almacena los identificadores en su `custom_id` mediante expresiones regulares, permitiendo que cualquier botón creado antes de un reinicio del bot continúe operando sin recarga de memoria.
3. **Control Estricto de Apodos**: La concatenación del nombre en League of Legends y el Riot Tag se trunca automáticamente a 32 caracteres (`[:32]`), garantizando compatibilidad absoluta con la API de Discord.
4. **Ciclo de Vida Determinista**: Las solicitudes transicionan únicamente entre `PENDING`, `APPROVED` y `DENIED`, evitando inconsistencias con el motor relacional.
