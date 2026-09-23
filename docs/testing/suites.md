# Catálogo de Suites de Pruebas y Guía de Ejecución

[⬅️ Volver a Testing](README.md) | [⬅️ Volver a Documentación](../README.md)

---

## 1. Inventario General de la Suite

El repositorio `DiscordBots` cuenta con un total de **1.138 pruebas automatizadas** distribuidas a lo largo de **49 archivos de test** más el módulo raíz de fixtures (`tests/conftest.py`), sumando un volumen de **33.981 líneas totales de test** (27.014 líneas efectivas de código excluyendo comentarios y blancos) en 50 archivos y 1.138 tests.

La recolección completa de casos de prueba con `uv run pytest --collect-only -q` toma **0.49 segundos**, y la ejecución íntegra de la suite insume aproximadamente **~35.8 segundos** en una estación Linux x86_64 estándar.

### Métricas Consolidadas

| Métrica | Valor Medido |
|---|---|
| **Total de Archivos de Test** | 49 archivos (`tests/test_*.py`) + `tests/conftest.py` (50 archivos en total) |
| **Total de Casos de Prueba** | 1.138 tests recolectados |
| **Líneas de Código de Pruebas (LoC)** | 33.981 LoC totales (27.014 LoC netas) |
| **Tiempo de Recolección (pytest collect)** | ~0.49 s |
| **Tiempo de Ejecución Completa** | ~35.8 s |
| **Tasa de Aprobación** | 100% (1.138 pasados, 0 fallos, 0 errores) |

---

## 2. Catálogo Exhaustivo por Nivel de la Pirámide

### 2.1 Nivel 1: Unit Suites (22 archivos | 524 tests | 11.985 LoC)

Pruebas en memoria con mocks puros, tiempo submilisegundo por test, sin interacción con bases de datos ni sockets de red.

| Archivo | Tests | LoC | Dominio y Cobertura Funcional |
|---|---:|---:|---|
| `tests/test_bot.py` | 13 | 241 | Inicialización del bot `LigaBot`, carga de extensiones, manejo de argumentos CLI en `__main__.py`. |
| `tests/test_config.py` | 11 | 218 | Validación de esquema Pydantic `Settings`, URLs de base de datos (`postgresql+asyncpg://`, `pglite:///`), valores por defecto y cachés. |
| `tests/test_cli.py` | 24 | 351 | Interfaz de línea de comandos (`run`, `seed`, `check`), códigos de retorno de proceso y argumentos. |
| `tests/test_formatting.py` | 19 | 205 | Formateo de cadenas, cálculo de marcas de tiempo relativas, tablas monoespaciadas y escape de caracteres Markdown. |
| `tests/test_database.py` | 14 | 345 | Helpers de base de datos, resolución dinámica de motores (`get_engine`, `close_engine`) y factoría de sesiones. |
| `tests/test_models.py` | 16 | 444 | Modelos declarativos base de SQLAlchemy (`User`, `Player`, `Team`, `Match`, `TicketNotice`). |
| `tests/test_role_request_model.py` | 16 | 382 | Modelo relacional `RoleRequest`, estados enum (`PENDING`, `APPROVED`, `DENIED`) e integridad de campos. |
| `tests/test_roster_models.py` | 55 | 677 | Modelos de gestión de plantillas: `DiscordUser`, `TeamMembership`, `RosterMovement`, `AuditLog`. |
| `tests/test_services.py` | 28 | 1.214 | Servicios de dominio `ScheduleService` y `TicketService` con repositorios mockeados. |
| `tests/test_suggestion_service.py` | 14 | 306 | Generación de embeds para sugerencias y despacho de reacciones de votación (`✅`, `❌`). |
| `tests/test_role_service.py` | 37 | 1.212 | Creación, aprobación y rechazo de solicitudes de rol, asignación de apodos y cálculo de diferencias de roles. |
| `tests/test_roster_sync_service.py` | 29 | 1.013 | Sincronización lógica de eventos de Discord con altas, bajas y transferencias en plantillas deportivas. |
| `tests/test_bridge_config.py` | 18 | 146 | Validación de parámetros para WebSocket Bridge (`bridge_host`, `bridge_port`, tokens de autenticación). |
| `tests/test_bridge_protocol.py` | 46 | 238 | Validación de tramas JSON, formato UUID v4 RFC 4122 y discriminadores polimórficos de comandos entrantes. |
| `tests/test_rate_limiter.py` | 7 | 141 | Algoritmo de limitación de tasa por ventana deslizante en memoria (`SlidingWindowRateLimiter`). |
| `tests/test_websocket_bridge_service.py` | 19 | 590 | Autenticación bifásica, silencio pre-autenticación, código de cierre por timeout 4001 y despacho de sugerencias. |
| `tests/test_cogs.py` | 35 | 1.053 | Comandos slash base y captura global de excepciones en extensiones de Discord. |
| `tests/test_roles_cog.py` | 27 | 789 | Comandos slash de verificación (`/pedir-rol`, `/asignar-rol`) y flujo de solicitud de roles. |
| `tests/test_roster_cog.py` | 23 | 642 | Comando slash `/gestionar-posicion` y escucha de eventos `on_member_update`. |
| `tests/test_role_config_permissions.py` | 18 | 294 | Verificación de permisos de staff (`staff_role_id`, `ceo_role_id`) para aprobación de roles. |
| `tests/test_roles_ui.py` | 25 | 614 | Vistas interactivas de Discord (`RoleVerificationView`) y modales de entrada de datos. |
| `tests/test_roster_ui.py` | 30 | 870 | Vistas interactivas de plantillas (`GestionarPosicionView`) y menús de selección de roles competitivos. |
| **Subtotal Unit** | **524** | **11.985** | |

---

### 2.2 Nivel 2: Integration & End-to-End Suites (6 archivos | 171 tests | 5.853 LoC)

Pruebas sobre el motor real PostgreSQL (PGlite) migrado con Alembic o integración completa de flujos inter-servicio.

| Archivo | Tests | LoC | Dominio y Cobertura Funcional |
|---|---:|---:|---|
| `tests/test_repositories.py` | 39 | 1.061 | Repositorios asíncronos base (`BaseRepository`), paginación, filtros relacionales y ordenación sobre PGlite. |
| `tests/test_role_request_repo.py` | 25 | 605 | `RoleRequestRepository` sobre PGlite: transiciones atómicas de estado y queries relacionales. |
| `tests/test_roster_repositories.py` | 75 | 1.440 | Repositorios de miembros, movimientos de plantilla y logs de auditoría sobre esquema relacional real. |
| `tests/test_bot_bridge_lifecycle.py` | 7 | 288 | Arranque conjunto de `LigaBot` y `WebsocketBridgeService`, inyección de dependencias y apagado coordinado. |
| `tests/test_role_verification_e2e.py` | 16 | 1.152 | Flujo E2E completo: comando `/pedir-rol` -> mensaje embed a canal staff -> botón de aprobación -> asignación de rol y base de datos. |
| `tests/test_roster_sync_e2e.py` | 9 | 1.307 | Flujo E2E de sincronización: evento Discord `on_member_update` -> sincronización de plantilla -> registro de movimiento y auditoría. |
| **Subtotal Integration** | **171** | **5.853** | |

---

### 2.3 Nivel 3: Resilience, Concurrency & Stress Suites (5 archivos | 110 tests | 3.305 LoC)

Pruebas de estrés temporal, fallos de red simulados, contención de sockets y carreras concurrentes masivas.

| Archivo | Tests | LoC | Dominio y Cobertura Funcional |
|---|---:|---:|---|
| `tests/test_bot_bridge_lifecycle_resilience.py` | 18 | 619 | 20 ciclos de inicio/parada con puertos efímeros (`port=0`), 50 tareas concurrentes en `bot.close()`, desconexión limpia de clientes WS. |
| `tests/test_websocket_bridge_concurrency.py` | 17 | 747 | Inundación de tramas inválidas, timeouts forzados 4001, pipelining en socket único, drenaje de tareas en segundo plano. |
| `tests/test_suggestion_service_resilience.py` | 47 | 659 | Respuestas adversas de la API de Discord (429/503/403/404), payloads límite (4096 caracteres) y esquemas URI maliciosos (`javascript:`, `data:`). |
| `tests/test_role_request_stress.py` | 23 | 540 | Ráfagas de solicitudes simultáneas, transiciones de estado inválidas y verificación de idempotencia transaccional. |
| `tests/test_roster_sync_stress.py` | 5 | 740 | Ciclos flip-flop masivos de roles, desbandada de clubes (10 bajas simultáneas) y ráfagas con `asyncio.gather`. |
| **Subtotal Resilience** | **110** | **3.305** | |

---

### 2.4 Nivel 4: Adversarial Challenge Suites (16 archivos | 333 tests | 12.758 LoC)

Pruebas destructivas diseñadas para vulnerar restricciones de base de datos, corromper transacciones o exceder límites del sistema operativo.

| Archivo | Tests | LoC | Dominio y Cobertura Funcional |
|---|---:|---:|---|
| `tests/test_adversarial_roster_cascades.py` | 24 | 1.386 | Borrados en cascada DDL crudo vs ORM, eliminación masiva de 20 equipos y 100 usuarios, fuzzing JSONB, mitigación de desbordamiento de socket UNIX en Node 24+. |
| `tests/test_adversarial_roster_concurrency_and_audit.py` | 11 | 909 | Carreras `asyncio.gather`, invariante de posición competitiva única, índice parcial de capitanía, recuperación del lock tras fallos transaccionales. |
| `tests/test_adversarial_roster_constraints.py` | 22 | 561 | Invariante de 1 solo capitán por equipo (`is_captain`), clave primaria compuesta (`team_id`, `discord_user_id`). |
| `tests/test_adversarial_roster_repositories.py` | 66 | 509 | Búsquedas multi-club, aislamiento relacional y ataques a la invariante competitiva. |
| `tests/test_adversarial_roster_sync_service.py` | 31 | 848 | Estrés de capitanía, transferencias entre clubes, estados transaccionales en PGlite. |
| `tests/test_adversarial_roster_sync_lifecycle.py` | 6 | 599 | Casos límite en el ciclo de vida de sincronización de plantillas y eventos desordenados. |
| `tests/test_adversarial_roster_cog_events.py` | 24 | 902 | Inundación de eventos `on_member_update` con Discord Mocks concurrentes. |
| `tests/test_adversarial_roster_command.py` | 20 | 927 | Bypasses de permisos en `/gestionar-posicion` y parámetros corruptos. |
| `tests/test_adversarial_roster_ui_transitions.py` | 10 | 810 | Ataques a timeouts de vistas, transiciones de estado desordenadas en menús interactivos. |
| `tests/test_roster_ui_adversarial.py` | 11 | 602 | Respuestas efímeras, clics simultáneos por múltiples usuarios. |
| `tests/test_adversarial_roster_audit_and_history.py` | 10 | 690 | Inmutabilidad de `AuditLog`, paginación profunda de historial y no-orfandad. |
| `tests/test_adversarial_bot_lifecycle.py` | 17 | 469 | Fallos forzados en `setup_hook`, inyección de dependencias corruptas y paradas abruptas. |
| `tests/test_adversarial_schedule.py` | 31 | 866 | Formatos de fecha maliciosos, solapamiento de partidos y zonas horarias desfasadas. |
| `tests/test_adversarial_ticket_and_cli.py` | 16 | 972 | Excepciones deliberadas en semillas CLI y desbordamientos en avisos de tickets. |
| `tests/test_adversarial_cog_boundaries.py` | 20 | 830 | Límites perimetrales de cogs (`TeamsCog` y `TicketsCog`). |
| `tests/test_adversarial_cogs.py` | 14 | 878 | Comportamientos ante fallos de permisos perimetrales y guild desincronizado. |
| `tests/conftest.py` | - | 80 | Módulo raíz de fixtures globales: sesión PGlite, mocks de Discord, factory de sesiones y limpieza de tablas. |
| **Subtotal Adversarial** | **333** | **12.758** | |
| **TOTAL GENERAL** | **1.138** | **33.981** | **49 archivos de test (33.901 LoC) + conftest.py (80 LoC) = 50 archivos** |

---

## 3. Convenciones de Nomenclatura y Estructura de Tests

Para mantener la uniformidad en una base de código de más de 33.000 líneas de test, se aplican las siguientes reglas:

1. **Nomenclatura de Archivos:**
   - Pruebas unitarias: `tests/test_<modulo>.py` (ej. `tests/test_config.py`).
   - Pruebas de integración de repositorios: `tests/test_<entidad>_repo.py` (ej. `tests/test_role_request_repo.py`).
   - Pruebas de estrés y resiliencia: `tests/test_<modulo>_stress.py` o `tests/test_<modulo>_resilience.py`.
   - Pruebas adversariales: `tests/test_adversarial_<dominio>.py`.

2. **Nomenclatura de Funciones y Clases de Test:**
   - Clases agrupadoras: prefijo `Test<Feature>` (ej. `TestRosterCascades`, `TestEngineLockHandling`).
   - Funciones de prueba: descriptivas con formato `test_<accion>_<escenario>_<resultado_esperado>` (ej. `test_resilience_rapid_start_stop_real_ephemeral_sockets_20_cycles`, `test_engine_lock_recovery_after_transaction_failure`).

3. **Inyección de Fixtures y Aislamiento Asíncrono:**
   - Todo test que interactúe con el bucle de eventos asíncrono se decora con `@pytest.mark.asyncio`.
   - Tests de base de datos unitarios/integración inyectan `session: AsyncSession` para aislamiento por savepoint.
   - Tests de concurrencia inyectan `session_factory` y activan `clean_roster_tables` para truncado atómico.

---

## 4. Guía de Ejecución con `uv` y Filtros Pytest

### 4.1 Comandos Básicos

```bash
# Recolección rápida de casos sin ejecución (verificar conteo)
uv run pytest --collect-only -q

# Ejecución completa de toda la suite (1.138 tests)
uv run pytest

# Ejecución con resumen compacto mostrando porcentaje de avance
uv run pytest -q
```

### 4.2 Ejecución por Nivel Piramidal

```bash
# Nivel 1: Ejecutar solo pruebas unitarias de modelos y configuración
uv run pytest tests/test_models.py tests/test_config.py tests/test_roster_models.py

# Nivel 2: Ejecutar pruebas de repositorios e integración E2E
uv run pytest tests/test_repositories.py tests/test_roster_repositories.py tests/test_role_verification_e2e.py

# Nivel 3: Ejecutar pruebas de resiliencia y concurrencia
uv run pytest tests/test_bot_bridge_lifecycle_resilience.py tests/test_websocket_bridge_concurrency.py

# Nivel 4: Ejecutar todas las suites de desafío adversarial
uv run pytest tests/test_adversarial_*.py
```

### 4.3 Ejecución Focalizada de las Suites Críticas de Entorno

```bash
# Ejecuta las tres suites que validan límites de entorno del sistema
uv run pytest tests/test_adversarial_roster_cascades.py \
              tests/test_adversarial_roster_concurrency_and_audit.py \
              tests/test_bot_bridge_lifecycle_resilience.py
```

### 4.4 Filtrado Avanzado por Expresión (`-k`)

```bash
# Filtrar pruebas relacionadas con el lock de serialización
uv run pytest -k "engine_lock"

# Filtrar pruebas de ciclo de vida de sockets y puertos efímeros
uv run pytest -k "ephemeral_sockets or concurrent_close"

# Filtrar pruebas que validen borrado en cascada
uv run pytest -k "cascade_deletion"
```

### 4.5 Profiling de Tiempos de Ejecución

```bash
# Identificar los 10 tests más lentos de la suite
uv run pytest --durations=10
```
