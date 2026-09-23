# Módulos Funcionales (Features)

[⬅️ Volver a Documentación](../README.md)

---

## Resumen Ejecutivo

La capa funcional de `DiscordBots` está estructurada en módulos verticales desacoplados (*vertical slices*), organizados por dominio de negocio. Cada módulo funcional agrupa sus propios comandos slash de Discord, controladores de eventos (*listeners*), servicios de lógica de negocio, adaptadores de interfaz de usuario (vistas interactivas, botones, modales) y esquemas de persistencia relacional.

Esta separación por responsabilidades garantiza que las reglas de negocio de cada subsistema (como la gestión de plantillas de clubes, la solicitud y verificación de roles competitivos o el calendario de jornadas) permanezcan aisladas, facilitando el mantenimiento, las pruebas unitarias y la escalabilidad del bot.

---

## Índice de Funcionalidades

| Funcionalidad | Dominio y Responsabilidad | Componentes Clave | Documentación |
|---|---|---|---|
| [**Gestión de Base de Datos**](database/README.md) | Motor de persistencia PostgreSQL/PGlite, modelos declarativos de SQLAlchemy 2.0, esquemas relacionales, enumerados y migraciones con Alembic. | `database.py`, `models/base.py`, `models/roster.py`, `repositories/base.py` | [Índice Database](database/README.md) |
| [**WebSocket Bridge**](websocket-bridge/README.md) | Pasarela de comunicación bidireccional en tiempo real entre el bot de Discord y servicios externos (Node.js/Next.js). Autenticación de tramas, limitación de tasa y despacho de sugerencias. | `websocket_bridge_service.py`, `bridge_protocol.py`, `rate_limiter.py` | [Índice WebSocket Bridge](websocket-bridge/README.md) |
| [**Plantillas y Roster**](roster/README.md) | Gestión de plantillas competitivas de equipos: asignación de posiciones en el quinteto, capitanías exclusivas, transferencias y sincronización automática de roles ante eventos de Discord. | `roster_sync_service.py`, `cogs/roster/`, `ui/roster.py`, `roster_movement_repo.py` | [Índice Roster](roster/README.md) |
| [**Solicitudes de Roles**](roles/README.md) | Flujo interactivo de onboarding y verificación de miembros: comandos `/pedir-rol` y `/asignar-rol`, modales interactivos de Discord, aprobación por staff y asignación atómica de roles y apodos. | `role_service.py`, `cogs/roles/`, `ui/roles.py`, `role_request_repo.py` | [Índice Roles](roles/README.md) |
| [**Tickets y Soporte**](tickets/README.md) | Sistema de soporte mediante tickets privados en canales dedicados de Discord, avisos de inactividad, cierre ordenado y transcripciones para el equipo de administración. | `ticket_service.py`, `cogs/tickets/`, `cogs/admin/`, `ticket_notice_repo.py` | [Índice Tickets](tickets/README.md) |
| [**Calendario y Partidos**](schedule/README.md) | Programación de partidos de liga, seguimiento de resultados deportivos, publicación periódica de cronogramas y validación de zonas horarias. | `schedule_service.py`, `cogs/schedule/`, `match_repo.py`, `models/match.py` | [Índice Schedule](schedule/README.md) |

---

## Estructura Atómica Estándar por Módulo

Cada subdirectorio de funcionalidad se descompone en un conjunto homogéneo de documentos atómicos especializados:

- **`README.md`**: Punto de entrada del módulo con cabecera de navegación (*breadcrumbs*), resumen funcional y tabla descriptiva de contenidos.
- **`commands.md`**: Especificación técnica de comandos slash (nombres, opciones, permisos requeridos, respuestas efímeras o públicas y validaciones de entrada).
- **`events.md`**: Eventos del gateway de Discord escuchados por los cogs del módulo (triggers, efectos secundarios y lógica de despacho).
- **`services.md`**: Orquestación de lógica de negocio pura, validaciones de dominio y coordinación entre entidades.
- **`persistence.md`**: Modelos SQLAlchemy, relaciones, restricciones relacionales, operaciones de repositorios e índices de base de datos.
- **`ui.md`**: Componentes visuales interactivos de Discord (vistas, botones de acción, menús desplegables `Select` y modales de captura de datos).
