# Portal de Documentación Técnica — LigaBot

[⬅️ Volver al Inicio](../README.md)

---

## 🎯 Resumen Ejecutivo

Bienvenido al centro neurálgico de documentación técnica de **LigaBot**, el bot de Discord oficial para la gestión deportiva y operativa de la liga amateur **RCL** (Rift Champions League).

Este repositorio de documentación está estructurado bajo un estándar de arquitectura hipergranular y desacoplada, dividiendo la especificación técnica en tres áreas principales de ingeniería:

1. **Arquitectura y Núcleo de Ejecución (`architecture/`)**: Fundamentos del ciclo de vida del bot, inyección de dependencias, configuración validada con Pydantic Settings v2, consola CLI y arquitectura dual de persistencia.
2. **Módulos Funcionales (`features/`)**: Especificaciones atómicas de cada funcionalidad del sistema organizadas por slices verticales (base de datos, pasarela WebSocket, gestión de plantillas y rosters, solicitudes de roles de onboarding, sistema de tickets y calendario competitivo).
3. **Subsistema de Testing y Calidad (`testing/`)**: Estrategia de pruebas en pirámide de 4 niveles, inventario completo de suites y análisis de límites de entorno e infraestructura (PGlite, descriptores de sockets UNIX y buffers de red).

---

## 🗺️ Mapa de Contenidos de Documentación

| Sección | Directorio | Descripción |
|---|---|---|
| 🏛️ **Arquitectura y Runtime** | [**`architecture/`**](./architecture/README.md) | Ciclo de vida asíncrono (`setup_hook`, `close`), inyección de servicios, configuración estricta (18 variables de entorno), consola CLI y motor dual PostgreSQL/PGlite. |
| 🧩 **Módulos Funcionales (Features)** | [**`features/`**](./features/README.md) | Hub de funcionalidades divididas por dominio de negocio: base de datos, WebSocket bridge, roster, roles, tickets y calendario. |
| 🧪 **Estrategia y Suites de Testing** | [**`testing/`**](./testing/README.md) | Metodología de pruebas (1.138 casos en 49 suites), aislamiento transaccional hermético con PGlite y mitigación de cuellos de botella del kernel. |

### Detalle de Módulos Funcionales (`features/`)

Cada subsistema funcional dispone de su propia carpeta con documentación atómica y un índice local descriptivo:

- [**Base de Datos y Modelos (`features/database/`)**](./features/database/README.md): Modelos relacionales SQLAlchemy 2.0, tipos enumerados, diagramas ERD, restricciones de integridad y migraciones Alembic.
- [**WebSocket Bridge (`features/websocket-bridge/`)**](./features/websocket-bridge/README.md): Protocolo de enlace bidireccional en tiempo real con la plataforma web RCL-Next, autenticación mediante supertoken, limitador de tasa y despacho de sugerencias.
- [**Gestión de Plantillas y Roster (`features/roster/`)**](./features/roster/README.md): Comandos slash, servicios de alineaciones y capitanías, sincronización automática de roles de Discord ante movimientos de jugadores y componentes interactivos UI.
- [**Solicitudes de Roles y Onboarding (`features/roles/`)**](./features/roles/README.md): Flujo de verificación de nuevos miembros, formularios modales interactivos de Discord, revisión administrativa y asignación atómica de roles y apodos.
- [**Tickets y Soporte (`features/tickets/`)**](./features/tickets/README.md): Creación y supervisión de canales privados de asistencia, detección de inactividad de 24h, exclusión de staff y archivo ordenado de incidencias.
- [**Calendario y Jornadas (`features/schedule/`)**](./features/schedule/README.md): Creación individual de enfrentamientos, importación masiva por lotes desde archivos CSV, creación segura de canales con rollback anti-huérfanos y publicación de plantillas oficiales.

---

## 📐 Estándar de Documentación Atómica

Cada módulo funcional dentro de `docs/features/` sigue un patrón modular estandarizado, separando las responsabilidades de documentación en archivos especializados de alta cohesión:

- **`README.md`**: Índice de navegación ligero con migas de pan (*breadcrumbs*), resumen ejecutivo de la funcionalidad y tabla explicativa de los documentos contenidos.
- **`commands.md`**: Catálogo exhaustivo de comandos de aplicación (*slash commands*), incluyendo sintaxis, parámetros requeridos y opcionales, permisos de Discord exigidos, respuestas efímeras o públicas y validaciones de entrada.
- **`events.md`**: Eventos del gateway de Discord escuchados por los cogs del módulo (disparadores, efectos colaterales sobre roles o canales y lógica de despacho).
- **`services.md`**: Especificación de la lógica de negocio pura, validaciones de dominio, orquestación entre entidades y coordinación con repositorios.
- **`persistence.md`**: Definición de modelos SQLAlchemy, relaciones, índices, restricciones de integridad referencial, operaciones CRUD en repositorios y transacciones.
- **`ui.md`**: Componentes interactivos de Discord (`discord.ui.View`, `discord.ui.Button`, `discord.ui.Select`, `discord.ui.Modal`), control de timeouts y manejo de interacciones asíncronas.
