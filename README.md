# LigaBot — Discord Bot Modular para RCL

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Package Manager](https://img.shields.io/badge/uv-0.5.0%2B-blueviolet.svg)](https://github.com/astral-sh/uv)
[![Code Style](https://img.shields.io/badge/code%20style-ruff-black.svg)](https://github.com/astral-sh/ruff)
[![Testing](https://img.shields.io/badge/tests-1138%20passed-brightgreen.svg)](https://docs.pytest.org/)
[![Database](https://img.shields.io/badge/SQLAlchemy-2.0%20Async-red.svg)](https://www.sqlalchemy.org/)

**LigaBot** es el bot de Discord oficial para la gestión deportiva, coordinación de enfrentamientos, verificación de miembros y enlace de integración en tiempo real de la liga amateur de League of Legends **RCL** (Rift Champions League). 

Diseñado bajo una arquitectura modular por capas desacopladas (*vertical slices*), el sistema integra persistencia asíncrona mediante **SQLAlchemy 2.0**, soporte de motores duales (**PostgreSQL** en producción y **PGlite** para ejecución local ultrarrápida sin dependencias externas), comandos slash modernos organizados en **Cogs**, interfaces de usuario reactivas con componentes interactivos (`discord.ui`), y una pasarela **WebSocket Bridge** bidireccional para comunicación en tiempo real con la plataforma web RCL-Next.

---

## 📑 Tabla de Contenidos

1. [Stack Tecnológico](#-stack-tecnológico)
2. [Características Principales](#-características-principales)
3. [Arquitectura del Sistema](#-arquitectura-del-sistema)
4. [Estructura del Repositorio (Árbol Comentado)](#-estructura-del-repositorio)
5. [Variables de Entorno](#-variables-de-entorno)
6. [Guía de Inicio Rápido](#-guía-de-inicio-rápido)
7. [Consola CLI y Sembrado de Datos](#-consola-cli-y-sembrado-de-datos)
8. [Estrategia de Testing y Calidad](#-estrategia-de-testing-y-calidad)
9. [Mapa del Sitio (Sitemap de Documentación)](#-mapa-del-sitio-sitemap-de-documentación)

---

## 🚀 Stack Tecnológico

| Componente | Tecnología | Versión | Propósito en el Ecosistema |
|---|---|---|---|
| **Lenguaje Base** | Python | `3.12+` | Tipado estricto (`typing`), características asíncronas modernas (`asyncio`). |
| **Gestor de Paquetes** | Astral `uv` | `0.5+` | Gestión determinista de dependencias, resolución ultrarrápida y entornos virtuales aislados. |
| **Framework Discord** | `discord.py` | `2.4+` | Conexión a Discord Gateway, comandos de aplicación (*slash commands*), vistas interactivas (`discord.ui.View`), botones y modales. |
| **ORM / Persistencia** | `SQLAlchemy` | `2.0+ Async` | Modelado relacional declarativo, mapeo objeto-relacional asíncrono, factoría de sesiones y context managers transaccionales. |
| **Driver de Producción** | `asyncpg` | `0.30+` | Conector nativo de alto rendimiento para PostgreSQL con soporte completo para pools de conexiones asíncronas. |
| **Motor Embebido** | `py-pglite` | `0.5+` | PostgreSQL compilado a WebAssembly ejecutado en proceso para desarrollo local y pruebas automatizadas sin Docker. |
| **Migraciones de Esquema** | `Alembic` | `1.14+` | Control de versiones de la base de datos relacional con soporte asíncrono para migraciones automáticas. |
| **Red y WebSockets** | `aiohttp` | `3.11+` | Servidor WebSocket interno y endpoints HTTP REST para la pasarela de integración bidireccional (*WebSocket Bridge*). |
| **Configuración** | `pydantic-settings` | `2.7+` | Validación estricta en tiempo de arranque, normalización de URLs y lectura de 18 variables de entorno. |
| **Calidad y Testing** | `pytest` + `ruff` | `9.1+` / `0.9+` | Suite de 1.138 pruebas deterministas (`pytest-asyncio`) y formateo/linting estricto de código. |

---

## ✨ Características Principales

- **Arquitectura Modular por Dominios**: Desacoplamiento estricto entre Capa de Presentación (Bot y Cogs), Servicios de Dominio, Repositorios de Persistencia y Modelos Relacionales.
- **Persistencia Dual Transparente**:
  - **PGlite** (`py-pglite[sqlalchemy]`): PostgreSQL embebido en memoria o en disco local para desarrollo y tests sin requerir servicios externos ni contenedores Docker.
  - **PostgreSQL Asíncrono** (`asyncpg`): Motor de alto rendimiento para entornos de producción con pool de conexiones y transacciones concurrentes.
- **Gestión Automatizada de Calendario y Jornadas**:
  - Creación individual de partidos (`/crear-partido`) con validación de división competitiva (Premier y Ascend).
  - Importación masiva por lotes desde archivos CSV (`/importar-jornada` y `/crear-jornada`) con tolerancia a delimitadores (`,` y `;`) y BOM UTF-8.
  - Generación de canales privados de Discord con permisos automáticos para equipos, árbitros, administración y CEOs.
  - Publicación instantánea de plantillas oficiales de coordinación, reglas de horario, convocatorias y Fearless Draft.
  - **Garantía Anti-Huérfanos**: Reversión transaccional inmediata y eliminación física del canal de Discord si ocurre un error durante el envío de mensajes o la transacción de base de datos.
- **Gestión de Plantillas y Conciliación de Rosters**:
  - Registro de alineaciones oficiales y capitanías exclusivas.
  - Sincronización automática de roles ante eventos de entrada, salida o transferencia de jugadores.
  - Trazabilidad y auditoría histórica inmutable de movimientos en la liga.
- **Onboarding Interactivo y Verificación de Roles**:
  - Comando `/pedir-rol` con formulario modal de captura de nombre de invocador (*Summoner Name*) y división.
  - Canales dedicados de revisión con vistas interactivas de aprobación/rechazo para el Staff.
  - Asignación atómica de roles de Discord y normalización automática de apodos en el servidor.
- **Auditoría Inteligente de Tickets de Soporte**:
  - Detección de inactividad superior a 24 horas en categorías designadas de soporte y fichajes.
  - Exclusión automática de mensajes originados por miembros del Staff, Administradores o CEOs.
  - Control de recurrencia diaria en base de datos para evitar spam de alertas.
  - Tarea periódica en segundo plano (`tasks.loop`) con aceleración controlada (*throttling*) y comando manual (`/revisar-tickets`).
- **WebSocket Bridge en Tiempo Real**:
  - Servidor WebSocket local seguro con autenticación mediante supertoken secreto.
  - Recepción de sugerencias desde el portal web RCL-Next y publicación automática en canales designados de Discord.
  - Limitador de tasa por ventana deslizante (*Sliding Window Rate Limiter*) para mitigar ataques de denegación de servicio.

---

## 🏛️ Arquitectura del Sistema

```
                            ┌────────────────────────────────────────┐
                            │          Discord Gateway / API         │
                            └───────────────────┬────────────────────┘
                                                │
                                                ▼
                            ┌────────────────────────────────────────┐
                            │         LigaBot (commands.Bot)         │
                            │   (setup_hook, dependency container)   │
                            └───────────────────┬────────────────────┘
                                                │
         ┌──────────────────┬───────────────────┼───────────────────┬──────────────────┐
         ▼                  ▼                   ▼                   ▼                  ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│    TeamsCog     │ │   ScheduleCog   │ │   TicketsCog    │ │    RosterCog    │ │    RolesCog     │
│ /registrar-equip│ │ /crear-partido  │ │ /revisar-tickets│ │ /gestionar-posic│ │ /pedir-rol      │
│ /equipos        │ │ /importar-jornad│ │ (Loop cada 24h) │ │ (Sync reactivo) │ │ /asignar-rol    │
└────────┬────────┘ └────────┬────────┘ └────────┬────────┘ └────────┬────────┘ └────────┬────────┘
         │                   │                   │                   │                   │
         └───────────────────┼───────────────────┼───────────────────┼───────────────────┘
                             │                   │                   │
                             ▼                   ▼                   ▼
         ┌─────────────────────────────────────────────────────────────────────────────┐
         │                            Servicios de Dominio                             │
         │  (ScheduleService, TicketService, RoleService, RosterSyncService, Bridge)   │
         └──────────────────────────────────────┬──────────────────────────────────────┘
                                                │
                                                ▼
         ┌─────────────────────────────────────────────────────────────────────────────┐
         │                         Repositorios de Persistencia                        │
         │ (TeamRepo, MatchRepo, TicketRepo, RoleRequestRepo, RosterRepo, BaseRepo)    │
         └──────────────────────────────────────┬──────────────────────────────────────┘
                                                │
                                                ▼
         ┌─────────────────────────────────────────────────────────────────────────────┐
         │                     Factoría de Sesiones SQLAlchemy 2.0                     │
         │             (transactional_session, AsyncSession, async_engine)             │
         └──────────────────────────────┬───────────────────────────────┬──────────────┘
                                        │                               │
                                        ▼                               ▼
                             ┌─────────────────────┐         ┌─────────────────────┐
                             │    Motor PGlite     │         │  Motor PostgreSQL   │
                             │ (Desarrollo/Tests)  │         │    (Producción)     │
                             │  pglite:///:memory: │         │  postgresql+asyncpg │
                             └─────────────────────┘         └─────────────────────┘
```

Para consultar la documentación exhaustiva del núcleo arquitectónico, visita el [Portal de Arquitectura](docs/architecture/README.md).

---

## 📂 Estructura del Repositorio

A continuación se muestra el árbol estructurado del repositorio, con comentarios en línea explicando el propósito de cada carpeta y archivo principal:

```
.
├── .data/                                    # Directorio local de datos y almacenamiento persistente PGlite
├── .github/                                  # Configuración de automatización y flujos de trabajo de GitHub
│   └── workflows/                            # Definición de pipelines de integración continua (CI)
│       └── ci.yml                            # Pipeline de CI (linting, tipado estricto y ejecución de tests)
├── alembic/                                  # Entorno de migraciones de base de datos relacional
│   ├── versions/                             # Scripts secuenciales de migración versionada
│   │   ├── 001_initial_schema.py             # Migración inicial: tablas teams, matches y ticket_notices
│   │   └── 002_role_requests.py              # Migración de roles: tablas role_requests, discord_users, team_memberships
│   ├── env.py                                # Configuración de ejecución asíncrona de Alembic con SQLAlchemy
│   └── script.py.mako                        # Plantilla Mako para generación de nuevas revisiones de migración
├── docs/                                     # Centro neurálgico de documentación técnica granular
│   ├── architecture/                         # Fundamentos arquitectónicos y runtime central
│   │   ├── README.md                         # Índice y mapa de navegación del núcleo arquitectónico
│   │   ├── cli.md                            # Consola CLI (liga-cli), comandos de sembrado y formatos
│   │   ├── configuration.md                  # Matriz de 18 variables de entorno con Pydantic Settings v2
│   │   ├── database-engine.md                # Persistencia dual (PostgreSQL asyncpg y PGlite embebido)
│   │   └── runtime.md                        # Ciclo de vida asíncrono de LigaBot, inyección de dependencias y hooks
│   ├── features/                             # Módulos funcionales del bot organizados por dominios
│   │   ├── database/                         # Modelos, esquemas relacionales y persistencia SQLAlchemy 2.0
│   │   │   ├── README.md                     # Índice del módulo de base de datos
│   │   │   ├── enums.md                      # Enumerados canónicos (Division, MatchStatus, RoleRequestStatus)
│   │   │   ├── models.md                     # Definición técnica de modelos declarativos y columnas
│   │   │   ├── persistence.md                # Repositorios de persistencia y consultas asíncronas
│   │   │   └── relationships.md              # Relaciones relacionales, claves foráneas y cascadas
│   │   ├── roles/                            # Sistema de onboarding y verificación de roles competitivos
│   │   │   ├── README.md                     # Índice del módulo de verificación de roles
│   │   │   ├── commands.md                   # Comandos slash (/pedir-rol, /asignar-rol, /publicar-panel-rol)
│   │   │   ├── persistence.md                # Persistencia de solicitudes de rol y auditoría de transiciones
│   │   │   ├── services.md                   # Servicio de lógica de negocio y validación de nombres de invocador
│   │   │   └── ui.md                         # Vistas interactivas, modales y botones de aprobación/rechazo
│   │   ├── roster/                           # Gestión integral de plantillas, jugadores y roles de equipo
│   │   │   ├── README.md                     # Índice del módulo de plantillas y alineaciones
│   │   │   ├── commands.md                   # Comandos slash de registro, alineaciones y capitanías
│   │   │   ├── persistence.md                # Modelos de membresía, auditoría histórica y repositorios
│   │   │   ├── services.md                   # Servicio de sincronización y conciliación automática con Discord
│   │   │   └── ui.md                         # Componentes UI de confirmación de traspasos y capitanías
│   │   ├── schedule/                         # Gestión de partidos, cronogramas y canales privados de juego
│   │   │   ├── README.md                     # Índice del módulo de calendario competitivo
│   │   │   ├── commands.md                   # Comandos slash (/crear-partido, /importar-jornada, /crear-jornada)
│   │   │   ├── persistence.md                # Persistencia relacional de partidos y validación de divisiones
│   │   │   └── services.md                   # Orquestación de creación segura de canales y plantillas oficiales
│   │   ├── tickets/                          # Auditoría y supervisión de tickets de soporte y fichajes
│   │   │   ├── README.md                     # Índice del módulo de tickets y soporte
│   │   │   ├── commands.md                   # Comandos slash de revisión manual y configuración (/revisar-tickets)
│   │   │   ├── persistence.md                # Repositorio de avisos de inactividad y trazabilidad
│   │   │   └── services.md                   # Tarea periódica cada 24h, algoritmos de detección y throttling
│   │   ├── websocket-bridge/                 # Pasarela de integración bidireccional en tiempo real
│   │   │   ├── README.md                     # Índice del subsistema WebSocket Bridge
│   │   │   ├── protocol.md                   # Protocolo de tramas JSON, códigos de acción y eventos
│   │   │   ├── rate-limiter.md               # Algoritmo de ventana deslizante para mitigación de sobrecarga
│   │   │   └── security.md                   # Handshake de autenticación con supertoken secreto
│   │   └── README.md                         # Portal central de módulos funcionales y catálogo de servicios
│   ├── testing/                              # Infraestructura, estrategia y suites de pruebas automatizadas
│   │   ├── README.md                         # Índice del subsistema de pruebas y resumen métrico
│   │   ├── environment-limits.md             # Mitigación de límites del kernel UNIX, Node 24+ y PGlite
│   │   ├── strategy.md                       # Estrategia de testing en pirámide de 4 niveles y fixtures
│   │   └── suites.md                         # Catálogo de las 49 suites de prueba (1.138 tests unitarios/e2e)
│   └── README.md                             # Portal principal de documentación técnica
├── src/                                      # Código fuente modular de la aplicación
│   └── liga_bot/                             # Paquete principal de LigaBot
│       ├── cogs/                             # Controladores de presentación y comandos slash (discord.ext.commands.Cog)
│       │   ├── __init__.py                   # Exportación de cogs principales del sistema
│       │   ├── admin.py                      # Comandos administrativos de bajo nivel y utilidades
│       │   ├── admin_cog.py                  # Cog de sincronización de comandos slash (/sync, /sincronizar)
│       │   ├── permissions.py                # Verificadores de permisos y decoradores de autorización por rol
│       │   ├── roles.py                      # Cog y listeners para el flujo de verificación de roles
│       │   ├── roster.py                     # Implementación interna de comandos de gestión de plantillas
│       │   ├── roster_cog.py                 # Cog de registro de plantillas, capitanías y agentes libres
│       │   ├── schedule.py                   # Implementación interna de gestión de partidos
│       │   ├── schedule_cog.py               # Cog de calendario, creación de partidos y procesado CSV de jornadas
│       │   ├── teams.py                      # Implementación interna de registro y consulta de equipos
│       │   ├── teams_cog.py                  # Cog para listar y registrar clubes de la liga
│       │   ├── tickets.py                    # Implementación interna de lógica de canales de soporte
│       │   └── tickets_cog.py                # Cog de auditoría de inactividad de tickets y bucle en segundo plano
│       ├── models/                           # Modelos declarativos SQLAlchemy 2.0 (esquemas relacionales)
│       │   ├── __init__.py                   # Exportación de modelos para registro en metadata
│       │   ├── base.py                       # Clase base declarativa (AsyncAttrs, DeclarativeBase, UUIDPrimaryKey)
│       │   ├── enums.py                      # Enumeraciones Python respaldadas por tipos ENUM de PostgreSQL
│       │   ├── match.py                      # Entidad Match (partidos, canales de Discord, fechas y estados)
│       │   ├── role_request.py               # Entidad RoleRequest (solicitudes de rol, apodos y resoluciones)
│       │   ├── roster.py                     # Entidades DiscordUser, TeamMembership y RosterMovementHistory
│       │   ├── team.py                       # Entidad Team (equipos, divisiones, roles de capitán y servidor)
│       │   └── ticket_notice.py              # Entidad TicketNotice (registro de avisos de inactividad de soporte)
│       ├── repositories/                     # Capa de abstracción y persistencia relacional (patrón Repository)
│       │   ├── __init__.py                   # Exportación de repositorios del sistema
│       │   ├── base.py                       # Repositorio base genérico asíncrono con operaciones CRUD estándar
│       │   ├── match_repo.py                 # Consultas especializadas sobre partidos y fechas de juego
│       │   ├── role_request_repo.py          # Consultas para solicitudes de rol por usuario y estado
│       │   ├── roster_repo.py                # Consultas atómicas de membresías de equipo y conciliación de roles
│       │   ├── team_repo.py                  # Consultas de equipos por nombre normalizado, división o rol
│       │   └── ticket_repo.py                # Consultas para control de avisos y marcas de inactividad
│       ├── services/                         # Capa de servicios de dominio y orquestación de negocio
│       │   ├── __init__.py                   # Exportación de servicios inyectables en el contenedor del bot
│       │   ├── bridge_protocol.py            # Serialización, validación y deserialización de mensajes WebSocket
│       │   ├── rate_limiter.py               # Algoritmo de ventana deslizante para limitación de tasa por IP/origen
│       │   ├── role_service.py               # Lógica de verificación de solicitudes de rol y asignación atómica
│       │   ├── roster_sync_service.py        # Conciliación y sincronización de roles de equipo con Discord
│       │   ├── schedule_service.py           # Creación transaccional de canales, permisos y plantillas de partido
│       │   ├── suggestion_service.py         # Recepción y publicación de sugerencias web en canales de Discord
│       │   ├── ticket_service.py             # Supervisión periódica de tickets, cálculo de 24h y filtros de staff
│       │   └── websocket_bridge_service.py   # Servidor WebSocket asíncrono aiohttp para integración con RCL-Next
│       ├── ui/                               # Componentes interactivos de usuario de Discord (discord.ui)
│       │   ├── __init__.py                   # Exportación de vistas y componentes interactivos
│       │   ├── roles.py                      # Formularios modales y vistas con botones de aprobación de roles
│       │   └── roster.py                     # Vistas interactivas de confirmación y selección de plantillas
│       ├── utils/                            # Utilidades auxiliares y funciones de formateo
│       │   ├── __init__.py                   # Inicialización del módulo de utilidades
│       │   └── formatting.py                 # Normalización de textos, formato de fechas y constructores de embeds
│       ├── __init__.py                       # Metadatos del paquete liga_bot y versión oficial
│       ├── __main__.py                       # Punto de entrada de ejecución directa (python -m liga_bot)
│       ├── bot.py                            # Definición de la clase LigaBot, ciclo de vida e inyección de servicios
│       ├── cli.py                            # Interfaz CLI para tareas administrativas y sembrado (liga-cli)
│       ├── config.py                         # Configuración Pydantic Settings v2 y constantes canónicas de la liga
│       └── database.py                       # Factoría de motores duales, sesiones asíncronas y context managers
├── tests/                                    # Batería exhaustiva de pruebas unitarias, integración y adversariales
│   ├── conftest.py                           # Fixtures de pytest, motor PGlite en memoria y mocks del bot
│   ├── test_adversarial_bot_lifecycle.py     # Pruebas adversariales de inicio, señales OS y cierre idempotente
│   ├── test_adversarial_cog_boundaries.py    # Pruebas de aislamiento entre cogs e inyección de servicios
│   ├── test_adversarial_cogs.py              # Inyecciones malformadas y validación de errores en cogs
│   ├── test_adversarial_roster_audit_and_history.py # Verificación de inmutabilidad en historial de movimientos
│   ├── test_adversarial_roster_cascades.py   # Integridad referencial y cascadas de borrado en plantillas
│   ├── test_adversarial_roster_cog_events.py # Concurrencia de eventos de Discord sobre membresías
│   ├── test_adversarial_roster_command.py    # Invocaciones adversariales de comandos slash de roster
│   ├── test_adversarial_roster_concurrency_and_audit.py # Condiciones de carrera en transferencias simultáneas
│   ├── test_adversarial_roster_constraints.py # Claves primarias compuestas y restricciones de unicidad
│   ├── test_adversarial_roster_repositories.py # Estrés en consultas masivas de repositorios de roster
│   ├── test_adversarial_roster_sync_lifecycle.py # Recuperación ante caídas durante la sincronización de roles
│   ├── test_adversarial_roster_sync_service.py # Desfases entre estado de Discord y base de datos
│   ├── test_adversarial_roster_ui_transitions.py # Manipulación concurrente de botones e interacciones UI
│   ├── test_adversarial_schedule.py          # Rollback transaccional ante fallos en creación de canales
│   ├── test_adversarial_ticket_and_cli.py    # Casos límite en revisión de tickets y argumentos de CLI
│   ├── test_bot.py                           # Pruebas de configuración base del bot y registro de cogs
│   ├── test_bot_bridge_lifecycle.py          # Ciclo de vida del servidor WebSocket durante la ejecución del bot
│   ├── test_bot_bridge_lifecycle_resilience.py # Resiliencia ante puertos ocupados y desconexiones abruptas
│   ├── test_bridge_config.py                 # Validación de parámetros de configuración del bridge
│   ├── test_bridge_protocol.py               # Serialización y deserialización de tramas JSON del protocolo
│   ├── test_cli.py                           # Validación de comandos CLI (seed-teams, esquemas JSON y CSV)
│   ├── test_cogs.py                          # Carga y descarga dinámica de cogs en LigaBot
│   ├── test_config.py                        # Validación estricta de las 18 variables con Pydantic Settings
│   ├── test_database.py                      # Conexión, pooling y sesiones asíncronas con PGlite y PostgreSQL
│   ├── test_formatting.py                    # Formateo de plantillas oficiales de coordinación y embeds
│   ├── test_models.py                        # Instanciación y restricciones de modelos relacionales
│   ├── test_rate_limiter.py                  # Pruebas de consumo de tokens y tasa de reposición en el limitador
│   ├── test_repositories.py                  # Operaciones CRUD en repositorios de persistencia
│   ├── test_role_config_permissions.py       # Permisos de comandos slash de asignación de roles
│   ├── test_role_request_model.py            # Estados y transiciones de la entidad RoleRequest
│   ├── test_role_request_repo.py             # Consultas de repositorio para solicitudes de verificación
│   ├── test_role_request_stress.py           # Concurrencia masiva en solicitudes simultáneas de roles
│   ├── test_role_service.py                  # Lógica de aprobación, rechazo y asignación de roles
│   ├── test_role_verification_e2e.py         # Flujo E2E desde comando /pedir-rol hasta otorgamiento de rol
│   ├── test_roles_cog.py                     # Manejo de interacciones slash en cogs de roles
│   ├── test_roles_ui.py                      # Interacción de modales y botones de verificación de roles
│   ├── test_roster_cog.py                    # Comandos slash de asignación de alineaciones y capitanías
│   ├── test_roster_models.py                 # Modelos DiscordUser, TeamMembership y RosterMovementHistory
│   ├── test_roster_repositories.py           # Métodos de acceso a datos para gestión de plantillas
│   ├── test_roster_sync_e2e.py               # Flujo E2E de sincronización de roles de equipo con Discord
│   ├── test_roster_sync_service.py           # Servicio de conciliación de discrepancias entre Discord y BD
│   ├── test_roster_sync_stress.py            # Estrés en sincronización masiva de miembros del servidor
│   ├── test_roster_ui.py                     # Componentes visuales de confirmación de roster
│   ├── test_roster_ui_adversarial.py         # Interacciones duplicadas o desincronizadas en UI de roster
│   ├── test_services.py                      # Pruebas unitarias de servicios de dominio
│   ├── test_suggestion_service.py            # Despacho de sugerencias a canales designados de Discord
│   ├── test_suggestion_service_resilience.py # Resiliencia ante fallos en envío de sugerencias o canal inválido
│   ├── test_websocket_bridge_concurrency.py  # Concurrencia de múltiples clientes WebSocket simultáneos
│   └── test_websocket_bridge_service.py      # Flujo de conexión, handshake y procesamiento de tramas
├── .env.example                              # Plantilla sincronizada con las 18 variables de entorno del sistema
├── .gitignore                                # Reglas de exclusión de archivos temporales y entornos virtuales
├── README.md                                 # Portal principal y guía integral del proyecto LigaBot
├── alembic.ini                               # Archivo de configuración principal para Alembic
├── jornada_prueba.csv                        # Archivo CSV de ejemplo para importación de partidos de liga
├── pyproject.toml                            # Definición de dependencias, herramientas (ruff, pytest) y metadatos
└── uv.lock                                   # Archivo de bloqueo determinista del gestor de paquetes uv
```

---

## ⚙️ Variables de Entorno

LigaBot utiliza **Pydantic Settings v2** para validar las 18 variables de configuración respaldadas por el archivo `.env`. Todos los nombres de variables son insensibles a mayúsculas/minúsculas.

A continuación se muestra la matriz completa de las 18 variables sincronizada con `src/liga_bot/config.py` y `.env.example`:

| Variable de Entorno | Tipo | Valor Predeterminado | Requerido | Descripción |
|---|---|---|:---:|---|
| `DISCORD_TOKEN` | `str` | `""` | **Sí** (en prod) | Token secreto del bot obtenido del Discord Developer Portal. |
| `GUILD_ID` | `int` | `1547725310508667010` | No | ID numérico (Snowflake) del servidor de Discord oficial de la liga. |
| `STAFF_ROLE_ID` | `int` | `1547729760384319518` | No | Snowflake del rol de Staff y Árbitros (gestión de partidos y tickets). |
| `ADMIN_ROLE_ID` | `int` | `1548795786110967919` | No | Snowflake del rol de Administradores (acceso completo y comando `/sync`). |
| `CEO_PREMIER_ROLE_ID` | `int` | `1548795782360993842` | No | Snowflake del rol de CEO para la división Premier. |
| `CEO_ASCEND_ROLE_ID` | `int` | `1548795784655405087` | No | Snowflake del rol de CEO para la división Ascend. |
| `CEO_ROLE_ID` | `int` | `0` | No | Snowflake del rol de CEO general unificado de la liga. |
| `SIN_VERIFICAR_ROLE_ID` | `int` | `0` | No | Snowflake del rol 'Sin Verificar' asignado a nuevos miembros. |
| `TICKET_ROL_CATEGORY_ID` | `int` | `0` | No | Snowflake de la categoría de Discord para canales de solicitud de rol. |
| `FREE_ROLE_NAME` | `str` | `"Libre"` | No | Nombre textual del rol asignado a agentes libres en el servidor. |
| `DATABASE_URL` | `str` | `"pglite:///:memory:"` | No | URI de conexión SQLAlchemy (`pglite:///:memory:` o `postgresql+asyncpg://...`). |
| `BRIDGE_ENABLED` | `bool` | `True` | No | Conmutador booleano maestro para activar/desactivar el servidor WebSocket. |
| `BRIDGE_HOST` | `str` | `"127.0.0.1"` | No | Dirección IP de enlace local del servidor WebSocket. |
| `BRIDGE_PORT` | `int` | `8765` | No | Puerto TCP de escucha para la pasarela WebSocket. |
| `DISCORD_BOT_SUPERTOKEN` | `str` | `""` | No | Clave secreta compartida requerida para autenticar conexiones WebSocket. |
| `SUGGESTIONS_CHANNEL_ID` | `int` | `0` | No | Snowflake del canal de Discord donde se publican sugerencias web. |
| `BRIDGE_RATE_LIMIT_PER_MINUTE` | `int` | `10` | No | Límite máximo global de peticiones por minuto admitidas a través de la pasarela WebSocket. |
| `LOG_LEVEL` | `str` | `"INFO"` | No | Nivel de logging (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |

Para un desglose pormenorizado de las propiedades computadas y validadores, consulta [docs/architecture/configuration.md](docs/architecture/configuration.md).

---

## 🛠️ Guía de Inicio Rápido

### 1. Requisitos Previos

- **Python 3.12 o superior** instalado en el sistema.
- Gestor de paquetes **uv** instalado (`curl -LsSf https://astral.sh/uv/install.sh | sh` o vía `brew`/`pipx`).

### 2. Instalación de Dependencias

Clona el repositorio e instala el entorno virtual con todas las dependencias bloqueadas:

```bash
cd DiscordBots
uv sync
```

### 3. Configuración del Entorno Local

Copia la plantilla de variables de entorno y ajusta tus credenciales:

```bash
cp .env.example .env
```

#### En Linux / macOS (PGlite embebido)
Para desarrollo local y pruebas no necesitas configurar un servidor PostgreSQL externo; el bot iniciará por defecto con **PGlite** en memoria (`DATABASE_URL=pglite:///:memory:`). Si deseas persistir los datos de desarrollo localmente en disco sin Docker, usa:

```env
DATABASE_URL=pglite:///./.data/pglite_dev_db
```

#### En Windows (PostgreSQL en Docker Desktop)
En Windows, `py-pglite` depende de Node.js abriendo sockets de dominio UNIX en el sistema de archivos, lo cual falla debido a que Node.js no permite sockets de escucha UNIX en rutas estándar de Windows (`Error: listen EACCES: permission denied`). Por ello, la solución canónica en Windows consiste en ejecutar PostgreSQL estándar en un contenedor mediante **Docker Desktop**:

```bash
docker run -d --name postgres-liga -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=liga_bot postgres:16
```

Y configurar en tu `.env`:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/liga_bot
```

### 4. Inicialización de la Base de Datos (Alembic)

Aplica las migraciones de esquema relacional para estructurar las tablas:

```bash
uv run alembic upgrade head
```

### 5. Sembrado Inicial de Equipos Canónicos

Puebla la base de datos con los 20 equipos oficiales de las divisiones Premier y Ascend mediante el script `liga-cli`:

```bash
uv run liga-cli seed-teams
```

### 6. Ejecución del Bot

Inicia el proceso principal de LigaBot mediante el script `liga-bot`:

```bash
uv run liga-bot
```

> **Nota sobre entornos Windows:** Asegúrate de que el contenedor Docker (`postgres-liga`) esté levantado antes de arrancar el bot si utilizas la cadena de conexión `postgresql+asyncpg`. En Linux y macOS, PGlite arranca de forma transparente en proceso.
>
> **Nota sobre sincronización de comandos:** Para prevenir límites de tasa (*rate limits*) de la API de Discord al reiniciar, LigaBot desacopla el registro de comandos de `on_ready`. Los administradores pueden forzar la sincronización global o de guild ejecutando el comando slash `/sync` directamente en el servidor.

---

## 💻 Consola CLI y Sembrado de Datos

El paquete incluye la utilidad de línea de comandos `liga-cli` (`src/liga_bot/cli.py`) para tareas de administración del sistema y operaciones de base de datos fuera de Discord:

```bash
# Ayuda y comandos disponibles
uv run liga-cli --help

# Sembrado con equipos canónicos predeterminados (20 equipos)
uv run liga-cli seed-teams

# Sembrado desde archivo JSON personalizado
uv run liga-cli seed-teams --file /ruta/a/equipos.json

# Sembrado desde archivo CSV personalizado
uv run liga-cli seed-teams --file /ruta/a/equipos.csv
```

La operación de sembrado es **idempotente y no destructiva**: verifica la existencia previa de cada equipo por nombre normalizado, crea los registros faltantes y asocia las divisiones correspondientes dentro de una transacción atómica.

Para especificaciones detalladas de los esquemas JSON y CSV, consulta [docs/architecture/cli.md](docs/architecture/cli.md).

---

## 🧪 Estrategia de Testing y Calidad

El proyecto cuenta con una infraestructura de pruebas automatizadas determinista y rigurosa respaldada por **33.981 líneas totales de test** (27.014 líneas efectivas de código excluyendo comentarios y blancos) en 50 archivos y **1.138 casos de prueba** divididos en 49 suites:

```bash
# Ejecución completa de la suite de pruebas
uv run pytest tests/

# Ejecución silenciosa y rápida
uv run pytest -q

# Ejecución con reporte de cobertura de código
uv run pytest --cov=liga_bot tests/

# Análisis estático y verificación de estilo con Ruff
uv run ruff check .
uv run ruff format --check .
```

Consulta el [Portal de Testing](docs/testing/README.md) para conocer la pirámide de pruebas, la estrategia de fixtures herméticas con PGlite y el análisis de límites de sockets del sistema operativo.

---

## 🗺️ Mapa del Sitio (Sitemap de Documentación)

Toda la documentación técnica se encuentra modularizada bajo el directorio [`docs/`](docs/README.md). A continuación se presentan los puntos de entrada para cada área del sistema:

- 📖 [**Portal Central de Documentación (`docs/README.md`)**](docs/README.md): Centro neurálgico, resumen de diseño modular y estándar de documentación atómica.
- 🏛️ [**Arquitectura y Núcleo (`docs/architecture/README.md`)**](docs/architecture/README.md):
  - [Ciclo de Vida y Runtime (`runtime.md`)](docs/architecture/runtime.md): Clase `LigaBot`, contenedor de servicios, `setup_hook` y cierre ordenado.
  - [Configuración (`configuration.md`)](docs/architecture/configuration.md): Matriz de 18 variables, validadores Pydantic y constantes.
  - [Consola CLI (`cli.md`)](docs/architecture/cli.md): Sintaxis de `seed-teams`, esquemas JSON/CSV y transacciones.
  - [Motor de Base de Datos (`database-engine.md`)](docs/architecture/database-engine.md): Persistencia dual PostgreSQL/PGlite y context managers.
- 🧩 [**Hub de Módulos Funcionales (`docs/features/README.md`)**](docs/features/README.md):
  - 🗄️ [**Base de Datos y Modelos (`docs/features/database/README.md`)**](docs/features/database/README.md):
    - [Modelos Relacionales (`models.md`)](docs/features/database/models.md)
    - [Tipos Enumerados (`enums.md`)](docs/features/database/enums.md)
    - [Relaciones y Restricciones (`relationships.md`)](docs/features/database/relationships.md)
    - [Capa de Persistencia (`persistence.md`)](docs/features/database/persistence.md)
  - 🌐 [**WebSocket Bridge (`docs/features/websocket-bridge/README.md`)**](docs/features/websocket-bridge/README.md):
    - [Protocolo y Tramas JSON (`protocol.md`)](docs/features/websocket-bridge/protocol.md)
    - [Seguridad y Handshake (`security.md`)](docs/features/websocket-bridge/security.md)
    - [Limitador de Tasa (`rate-limiter.md`)](docs/features/websocket-bridge/rate-limiter.md)
  - 👥 [**Plantillas y Roster (`docs/features/roster/README.md`)**](docs/features/roster/README.md):
    - [Comandos Slash (`commands.md`)](docs/features/roster/commands.md)
    - [Servicio de Sincronización (`services.md`)](docs/features/roster/services.md)
    - [Persistencia y Membresías (`persistence.md`)](docs/features/roster/persistence.md)
    - [Componentes UI (`ui.md`)](docs/features/roster/ui.md)
  - 🛡️ [**Solicitudes de Roles (`docs/features/roles/README.md`)**](docs/features/roles/README.md):
    - [Comandos Slash (`commands.md`)](docs/features/roles/commands.md)
    - [Servicio de Onboarding (`services.md`)](docs/features/roles/services.md)
    - [Persistencia de Solicitudes (`persistence.md`)](docs/features/roles/persistence.md)
    - [Vistas y Modales UI (`ui.md`)](docs/features/roles/ui.md)
  - 🎫 [**Tickets y Soporte (`docs/features/tickets/README.md`)**](docs/features/tickets/README.md):
    - [Comandos de Revisión (`commands.md`)](docs/features/tickets/commands.md)
    - [Servicio de Auditoría (`services.md`)](docs/features/tickets/services.md)
    - [Persistencia de Avisos (`persistence.md`)](docs/features/tickets/persistence.md)
  - 📅 [**Calendario y Partidos (`docs/features/schedule/README.md`)**](docs/features/schedule/README.md):
    - [Comandos Slash (`commands.md`)](docs/features/schedule/commands.md)
    - [Servicio de Partidos y Canales (`services.md`)](docs/features/schedule/services.md)
    - [Persistencia de Enfrentamientos (`persistence.md`)](docs/features/schedule/persistence.md)
- 🧪 [**Subsistema de Testing (`docs/testing/README.md`)**](docs/testing/README.md):
  - [Estrategia de Pruebas (`strategy.md`)](docs/testing/strategy.md): Pirámide de 4 niveles, fixtures y aislamiento.
  - [Catálogo de Suites (`suites.md`)](docs/testing/suites.md): Detalle de las 49 suites y distribución de casos.
  - [Límites de Entorno (`environment-limits.md`)](docs/testing/environment-limits.md): Mitigaciones de buffers de sockets y kernel.
