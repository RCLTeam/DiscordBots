# LigaBot — Discord Bot Modular para RCL

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Package Manager](https://img.shields.io/badge/uv-0.5.0%2B-blueviolet.svg)](https://github.com/astral-sh/uv)
[![Code Style](https://img.shields.io/badge/code%20style-ruff-black.svg)](https://github.com/astral-sh/ruff)
[![Testing](https://img.shields.io/badge/tests-288%20passed-brightgreen.svg)](https://docs.pytest.org/)
[![Database](https://img.shields.io/badge/SQLAlchemy-2.0%20Async-red.svg)](https://www.sqlalchemy.org/)

**LigaBot** es el bot de Discord oficial para la gestión deportiva, coordinación de enfrentamientos y auditoría de soporte de la liga amateur de League of Legends **RCL** (Rift Champions League). Construido con una arquitectura modular por capas desacopladas, persistencia asíncrona mediante **SQLAlchemy 2.0**, soporte de motores duales (**PGlite** para pruebas locales ultrarrápidas y **PostgreSQL** para producción), y comandos slash organizados en **Cogs**.

---

## 📑 Tabla de Contenidos

- [Características Principales](#-características-principales)
- [Arquitectura del Sistema](#-arquitectura-del-sistema)
- [Requisitos Previos](#-requisitos-previos)
- [Instalación y Configuración](#-instalación-y-configuración)
- [Variables de Entorno](#-variables-de-entorno)
- [Base de Datos y Migraciones (Alembic)](#-base-de-datos-y-migraciones-alembic)
- [Sembrado de Datos (CLI Seed-Teams)](#-sembrado-de-datos-cli-seed-teams)
- [Ejecución del Bot](#-ejecución-del-bot)
- [Pruebas y Calidad de Código](#-pruebas-y-calidad-de-código)
- [Estructura del Repositorio](#-estructura-del-repositorio)
- [Documentación Adicional](#-documentación-adicional)

---

## ✨ Características Principales

- **Arquitectura Modular Desacoplada**: Separación estricta de responsabilidades entre Capa de Presentación (Bot y Cogs), Servicios de Dominio, Repositorios de Persistencia y Modelos Relacionales.
- **Persistencia Dual Transparente**:
  - **PGlite** (`py-pglite[sqlalchemy]`): PostgreSQL 17 embebido en memoria o local para desarrollo y tests sin necesidad de Docker ni servicios externos.
  - **PostgreSQL Asíncrono** (`asyncpg`): Motor de alto rendimiento para entornos de producción.
- **Gestión Automatizada de Calendario y Jornadas**:
  - Creación individual de partidos (`/crear-partido`) con verificación de división competitiva.
  - Importación masiva por lotes desde archivos CSV (`/importar-jornada` y `/crear-jornada`) con soporte para delimitadores `,` y `;`, así como codificación UTF-8 con o sin BOM.
  - Generación de canales privados de Discord con permisos automáticos para equipos, árbitros, administración y CEOs de división.
  - Publicación instantánea de plantillas oficiales de coordinación, reglas de horario, convocatorias y Fearless Draft.
  - **Garantía Anti-Huérfanos**: Reversión inmediata y eliminación del canal de Discord si ocurre un error durante el envío de mensajes o la transacción de base de datos.
- **Auditoría Inteligente de Tickets**:
  - Detección de inactividad superior a 24 horas en categorías designadas de soporte y fichajes.
  - Exclusión automática de mensajes originados por miembros del Staff, Administradores o CEOs mediante resolución de caché y API.
  - Control de recurrencia diaria en base de datos para evitar spam de alertas.
  - Ejecución en segundo plano mediante bucle periódico (`tasks.loop`) con aceleración controlada (*throttling*) y comando bajo demanda (`/revisar-tickets`).
- **Sincronización Segura de Comandos**: Árbol de comandos slash desacoplado del evento `on_ready` para evitar saturación de peticiones y límites de tasa (HTTP 429), gestionado bajo demanda vía `/sync` o `/sincronizar`.

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
                            │      (setup_hook, intents, close)      │
                            └───────────────────┬────────────────────┘
                                                │
                 ┌───────────────────┬──────────┴─────────┬──────────────────┐
                 ▼                   ▼                    ▼                  ▼
        ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
        │    TeamsCog     │ │   ScheduleCog   │ │   TicketsCog    │ │    AdminCog     │
        │ /registrar-equipo│ │ /crear-partido  │ │ /revisar-tickets│ │ /sync           │
        │ /equipos        │ │ /importar-jornad│ │ (Loop cada 24h) │ │ /sincronizar    │
        └────────┬────────┘ └────────┬────────┘ └────────┬────────┘ └────────┬────────┘
                 │                   │                   │                   │
                 ▼                   ▼                   ▼                   │
        ┌─────────────────────────────────────────────────────────┐          │
        │                   Servicios de Dominio                  │          │
        │          (ScheduleService, TicketService)               │          │
        └────────────────────────────┬────────────────────────────┘          │
                                     │                                       │
                                     ▼                                       ▼
        ┌─────────────────────────────────────────────────────────┐ ┌─────────────────┐
        │                Repositorios de Persistencia             │ │ permissions.py  │
        │  (TeamRepository, MatchRepository, TicketNoticeRepository)│ └─────────────────┘
        └────────────────────────────┬────────────────────────────┘
                                     │
                                     ▼
        ┌─────────────────────────────────────────────────────────┐
        │             Factoría de Sesiones SQLAlchemy 2.0         │
        │    (transactional_session, AsyncSession, async_engine)  │
        └──────────────┬───────────────────────────┬──────────────┘
                       │                           │
                       ▼                           ▼
            ┌─────────────────────┐     ┌─────────────────────┐
            │   Motor PGlite      │     │  Motor PostgreSQL   │
            │ (Desarrollo/Tests)  │     │    (Producción)     │
            │   pglite:///:memory:│     │  postgresql+asyncpg │
            └─────────────────────┘     └─────────────────────┘
```

Para una explicación exhaustiva de cada componente, consulta el documento [docs/architecture.md](docs/architecture.md).

---

## 🚀 Requisitos Previos

- **Python**: `>= 3.10` (compatible con 3.10, 3.11 y 3.12).
- **Gestor de Paquetes**: [`uv`](https://github.com/astral-sh/uv) `>= 0.5.0` (recomendado para máxima velocidad de resolución y sincronización de dependencias).
- **Discord Developer Portal**: Una aplicación de Discord con bot creado y los siguientes **Privileged Gateway Intents** activados:
  - `Server Members Intent` (`intents.members = True`)
  - `Message Content Intent` (`intents.message_content = True`)

---

## 📦 Instalación y Configuración

1. **Clonar e ingresar al directorio del proyecto:**
   ```bash
   cd DiscordBots
   ```

2. **Instalar dependencias y sincronizar el entorno virtual con `uv`:**
   ```bash
   uv sync
   ```
   *Nota: `uv sync` creará automáticamente el entorno virtual `.venv`, resolverá el candado `uv.lock`, instalará todas las librerías necesarias y dejará el paquete `liga-bot` instalado en modo editable (`editable mode`).*

3. **Crear el archivo de configuración `.env`:**
   ```bash
   cp .env.example .env
   ```

4. **Configurar credenciales en `.env`:**
   Abre `.env` con tu editor preferido y asigna el token del bot y los identificadores de tu servidor de Discord:
   ```env
   DISCORD_TOKEN=tu_token_secreto_de_discord
   GUILD_ID=1547725310508667010
   DATABASE_URL=pglite:///:memory:
   ```

---

## ⚙️ Variables de Entorno

Todas las opciones de configuración se gestionan en `src/liga_bot/config.py` mediante `pydantic-settings` v2:

| Variable | Tipo | Valor por Defecto | Descripción |
| :--- | :--- | :--- | :--- |
| `DISCORD_TOKEN` | `str` | `""` | Token secreto de autenticación de la aplicación de Discord. Requerido para iniciar el bot. |
| `GUILD_ID` | `int` | `1547725310508667010` | ID del servidor de Discord de la liga (RCL). Utilizado para registrar y sincronizar comandos por servidor. |
| `STAFF_ROLE_ID` | `int` | `1547729760384319518` | ID del rol de Staff / Árbitros. Habilita permisos operativos para crear partidos, registrar equipos y gestionar canales. |
| `ADMIN_ROLE_ID` | `int` | `1548795786110967919` | ID del rol de Administradores. Otorga control total y acceso exclusivo a `/sync`. |
| `CEO_PREMIER_ROLE_ID`| `int` | `1548795782360993842` | ID del rol de CEO de División Premier. Permite visibilidad y coordinación en partidos de Premier. |
| `CEO_ASCEND_ROLE_ID` | `int` | `1548795784655405087` | ID del rol de CEO de División Ascenso. Permite visibilidad y coordinación en partidos de Ascenso. |
| `ORGANIZADOR_ROLE_ID`| `int` (opcional) | `None` | ID del rol opcional de Organizador General. Considerado parte del personal exento de alertas de tickets. |
| `DATABASE_URL` | `str` | `pglite:///:memory:` | Cadena de conexión SQLAlchemy. Soporta esquemas `pglite:///` y `postgresql+asyncpg://`. |
| `LOG_LEVEL` | `str` | `INFO` | Nivel de registro de eventos (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |
| *Constantes de Tickets* | — | — | Nombres canónicos de categorías (`TICKETS-GENERAL-PREMIER`, etc.) y umbral de alerta (24 horas). |

---

## 🗄️ Base de Datos y Migraciones (Alembic)

LigaBot incorpora control de versiones del esquema relacional mediante **Alembic** asíncrono. El esquema incluye las tablas `teams`, `matches` y `ticket_notices`.

### Comandos de Migración

```bash
# Aplicar todas las revisiones pendientes hasta la última versión
uv run alembic upgrade head

# Ver el historial de revisiones del proyecto
uv run alembic history

# Revertir la última migración aplicada
uv run alembic downgrade -1

# Generar una nueva migración autogenerada basada en cambios en los modelos
uv run alembic revision --autogenerate -m "descripcion_del_cambio"
```

Para detalles completos sobre las tablas, índices, restricciones y soporte de motores duales, consulta [docs/database.md](docs/database.md).

---

## 🌾 Sembrado de Datos (CLI Seed-Teams)

LigaBot incluye una interfaz de línea de comandos (`src/liga_bot/cli.py`) para registrar de forma masiva e idempotente los equipos oficiales:

```bash
# 1. Sembrar los 8 equipos canónicos predeterminados (4 Premier y 4 Ascenso):
uv run python -m liga_bot.cli seed-teams

# 2. Sembrar únicamente equipos de una división específica:
uv run python -m liga_bot.cli seed-teams --division PREMIER
uv run python -m liga_bot.cli seed-teams --division ASCEND

# 3. Sembrar desde un archivo JSON externo:
uv run python -m liga_bot.cli seed-teams --json-file ruta/a/equipos.json

# 4. Sembrar desde un archivo CSV externo:
uv run python -m liga_bot.cli seed-teams --csv-file ruta/a/equipos.csv

# 5. Limpiar equipos existentes y resembrar desde cero:
uv run python -m liga_bot.cli seed-teams --clear
```

*Nota: También puedes usar el binario de consola registrado: `uv run liga-bot seed-teams`.*

---

## 🤖 Ejecución del Bot

### Iniciar el Bot

```bash
# Ejecución directa como módulo
uv run python -m liga_bot

# O ejecución mediante el script de entrada registrado
uv run liga-bot
```

### Sincronizar Comandos Slash en Discord

Para evitar saturar la API de Discord con reconexiones continuas, los comandos slash se sincronizan manualmente mediante el comando:

```
/sync
```
o su alias en español:
```
/sincronizar
```

---

## 🧪 Pruebas y Calidad de Código

### Suite de Pruebas Automatizadas

La suite de pruebas ejecuta **288 pruebas unitarias y de integración** sobre una base de datos **PGlite en memoria** totalmente aislada e independiente:

```bash
# Ejecutar todas las pruebas con salida detallada
uv run pytest -v

# Ejecutar pruebas sobre un módulo específico
uv run pytest tests/test_schedule_service.py -v
uv run pytest tests/test_ticket_service.py -v
uv run pytest tests/test_repositories.py -v
uv run pytest tests/test_models.py -v
uv run pytest tests/test_cogs.py -v
```

### Linter y Formateador (Ruff)

```bash
# Análisis estático y verificación de reglas de código
uv run ruff check .

# Comprobación de formato sin modificar archivos
uv run ruff format --check .

# Aplicar correcciones automáticas de formato
uv run ruff format .
```

---

## 📂 Estructura del Repositorio

```
DiscordBots/
├── alembic/                         # Configuración y scripts de migración de base de datos
│   ├── versions/                    # Revisiones versionadas (001_initial_schema.py)
│   └── env.py                       # Entorno de ejecución asíncrono de Alembic
├── docs/                            # Documentación técnica y manuales operativos
│   ├── architecture.md              # Diagramas de capas, ciclo de vida y decisiones de diseño
│   ├── database.md                  # Modelo relacional, ER, restricciones y motores duales
│   └── commands.md                  # Manual completo de comandos slash y herramientas CLI
├── src/
│   └── liga_bot/                    # Paquete raíz de la aplicación
│       ├── __init__.py              # Metadatos del paquete
│       ├── __main__.py              # Punto de entrada de ejecución (python -m liga_bot)
│       ├── bot.py                   # Subclase LigaBot(commands.Bot), ciclo de vida e inyección
│       ├── cli.py                   # Herramienta de consola (seed-teams)
│       ├── config.py                # Configuración tipada con pydantic-settings
│       ├── database.py              # Factoría de motores duales, sesiones y transacciones
│       ├── cogs/                    # Extensiones y comandos slash de Discord
│       │   ├── admin.py             # Cog Admin (/sync, /sincronizar)
│       │   ├── permissions.py       # Validadores de permisos y decoradores
│       │   ├── schedule.py          # Cog Schedule (/crear-partido, /importar-jornada)
│       │   ├── teams.py             # Cog Teams (/registrar-equipo, /equipos)
│       │   └── tickets.py           # Cog Tickets (loop de auditoría y /revisar-tickets)
│       ├── models/                  # Entidades declarativas SQLAlchemy 2.0
│       │   ├── base.py              # Clase Base, mixins de UUID y timestamps
│       │   ├── enums.py             # Enums de dominio (Division, MatchStatus)
│       │   ├── match.py             # Entidad Match (partidos programados)
│       │   ├── team.py              # Entidad Team (equipos y roles)
│       │   └── ticket_notice.py     # Entidad TicketNotice (seguimiento de alertas)
│       ├── repositories/            # Capa de acceso a datos desacoplada
│       │   ├── base.py              # Repositorio genérico base CRUD
│       │   ├── match_repo.py        # Consultas y persistencia de partidos
│       │   ├── team_repo.py         # Consultas y persistencia de equipos
│       │   └── ticket_repo.py       # Consultas y persistencia de tickets
│       ├── services/                # Capa de lógica de negocio y dominio
│       │   ├── schedule_service.py  # Orquestación de calendario, validación y canales
│       │   └── ticket_service.py    # Auditoría de inactividad y resolución de autores
│       └── utils/                   # Utilidades transversales
│           └── formatting.py        # Normalización NFKD, slugs, tags y plantillas
├── tests/                           # Suite integral de pruebas (288 tests)
├── .env.example                     # Plantilla documentada de variables de entorno
├── alembic.ini                      # Archivo de configuración de Alembic
├── pyproject.toml                   # Especificación de proyecto PEP 621 y dependencias
└── uv.lock                          # Archivo de bloqueo reproducible de dependencias
```

---

## 📚 Documentación Adicional

Para profundizar en el diseño, la persistencia y la operación de LigaBot, consulta los manuales en la carpeta `docs/`:

- [📐 Arquitectura del Sistema (`docs/architecture.md`)](docs/architecture.md): Explicación exhaustiva del flujo de datos, inyección de dependencias, contención de errores y ciclo de vida de tareas.
- [🗄️ Base de Datos y Persistencia (`docs/database.md`)](docs/database.md): Diagramas entidad-relación, catálogo de tablas, restricciones de integridad y guía de migraciones.
- [📖 Manual de Comandos y Operaciones (`docs/commands.md`)](docs/commands.md): Guía de referencia paso a paso para Staff, Administradores y CEOs de división sobre cada comando slash y script de consola.
