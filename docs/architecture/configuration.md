# Sistema de Configuración y Variables de Entorno

[⬅️ Volver a Arquitectura](./README.md)

Este documento detalla la arquitectura de configuración del bot, implementada en `src/liga_bot/config.py` mediante **Pydantic Settings v2**. Cubre la matriz completa de las 26 variables de entorno, las constantes canónicas de la liga, los validadores de campo, las propiedades computadas para el motor de base de datos y la factoría singleton en caché.

---

## 1. Arquitectura de Configuración (`Settings`)

La configuración global de `LigaBot` está encapsulada en la clase `Settings(BaseSettings)` (`src/liga_bot/config.py`). Utiliza Pydantic Settings v2 para garantizar tipado estricto, lectura automática de archivos `.env` y variables de entorno del sistema operativo, con capacidad de sobreescritura controlada.

### Configuración del Modelo (`model_config`)

En `src/liga_bot/config.py`:

```python
model_config = SettingsConfigDict(
    env_file=os.environ.get("LIGA_BOT_ENV_FILE", ".env"),
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)
```

- **`env_file`**: Lee de manera predeterminada el archivo `.env` en la raíz de ejecución del proyecto; `LIGA_BOT_ENV_FILE` permite apuntar a otro fichero (o a uno inexistente para ignorarlo).
- **`env_file_encoding="utf-8"`**: Asegura la interpretación de caracteres UTF-8 en rutas y secretos.
- **`case_sensitive=False`**: Permite definir variables de entorno tanto en mayúsculas (`DISCORD_TOKEN`) como en minúsculas (`discord_token`).
- **`extra="ignore"`**: Ignora variables adicionales presentes en el entorno sin disparar errores de validación.

---

## 2. Matriz Exhaustiva de las 26 Variables de Entorno

`Settings` tiene 26 campos: los 23 de esta tabla, organizados por dominio funcional, y los 3 de la auditoría de tickets de la sección 2.2. `tests/test_config.py` comprueba que esta página, la tabla del `README.md` y `.env.example` recogen todos los campos de `Settings`.

| # | Atributo Python | Tipo | Valor Predeterminado | Variable de Entorno | Descripción Funcional |
|---|---|---|---|---|---|
| 1 | `discord_token` | `str` | `""` | `DISCORD_TOKEN` | Token secreto de autenticación del bot en la API de Discord. |
| 2 | `guild_id` | `int` | `1547725310508667010` | `GUILD_ID` | Snowflake ID del servidor principal de Discord de la liga. |
| 3 | `staff_role_id` | `int` | `1547729760384319518` | `STAFF_ROLE_ID` | Snowflake ID del rol asignado a árbitros y personal de Staff. |
| 4 | `admin_role_id` | `int` | `1548795786110967919` | `ADMIN_ROLE_ID` | Snowflake ID del rol de Administradores de la liga. |
| 5 | `ceo_premier_role_id` | `int` | `1548795782360993842` | `CEO_PREMIER_ROLE_ID` | Snowflake ID del rol otorgado a capitanes/CEOs de la división Premier. |
| 6 | `ceo_ascend_role_id` | `int` | `1548795784655405087` | `CEO_ASCEND_ROLE_ID` | Snowflake ID del rol otorgado a capitanes/CEOs de la división Ascend. |
| 7 | `competition_dept_role_id` | `int` | `1548795790896398448` | `COMPETITION_DEPT_ROLE_ID` | Snowflake ID del rol del Departamento de Competición (árbitros con acceso y permisos en canales de partido). |
| 8 | `ceo_role_id` | `int` | `0` | `CEO_ROLE_ID` | Snowflake ID del rol de CEO unificado o general. |
| 9 | `sin_verificar_role_id` | `int` | `0` | `SIN_VERIFICAR_ROLE_ID` | Snowflake ID del rol asignado a usuarios recién ingresados sin verificar. |
| 10 | `ticket_rol_category_id` | `int` | `0` | `TICKET_ROL_CATEGORY_ID` | Snowflake ID de la categoría donde se generan canales de solicitud de roles. |
| 11 | `free_role_name` | `str` | `"Libre"` | `FREE_ROLE_NAME` | Nombre textual del rol asignado a jugadores en condición de agente libre. |
| 12 | `moderators_channel_id` | `int` | `1548038711697080494` | `MODERATORS_CHANNEL_ID` | Snowflake ID del canal de moderadores donde `RoleService` publica alertas del sistema. `0` las desactiva. |
| 13 | `casters_channel_id` | `int` | `1550210628361392278` | `CASTERS_CHANNEL_ID` | Snowflake ID del canal donde se publica el panel de casters. |
| 14 | `caster_role_id` | `int` | `0` | `CASTER_ROLE_ID` | Snowflake ID del rol de caster requerido para usar los botones del panel. `0` desactiva la restricción. |
| 15 | `reglamento_channel_id` | `int` | `1548038711697080491` (`DEFAULT_REGLAMENTO_CHANNEL_ID`) | `REGLAMENTO_CHANNEL_ID` | Snowflake ID del canal del reglamento que `format_mensaje_2` menciona en el mensaje de coordinación de cada canal de partido. |
| 16 | `database_url` | `str` | `"pglite:///:memory:"` | `DATABASE_URL` | URI de conexión para SQLAlchemy (compatible con esquemas PGlite y PostgreSQL). |
| 17 | `bridge_enabled` | `bool` | `True` | `BRIDGE_ENABLED` | Conmutador booleano maestro para iniciar o deshabilitar el servidor WebSocket local. |
| 18 | `bridge_host` | `str` | `"127.0.0.1"` | `BRIDGE_HOST` | Dirección IP de enlace para el servidor WebSocket local. |
| 19 | `bridge_port` | `int` | `8765` | `BRIDGE_PORT` | Puerto TCP de enlace para el servidor WebSocket local. |
| 20 | `discord_bot_supertoken` | `str` | `""` | `DISCORD_BOT_SUPERTOKEN` | Clave secreta compartida requerida en el handshake de autenticación WebSocket. |
| 21 | `suggestions_channel_id` | `int` | `0` | `SUGGESTIONS_CHANNEL_ID` | Snowflake ID del canal de Discord donde se publican las sugerencias web. |
| 22 | `bridge_rate_limit_per_minute` | `int` | `10` | `BRIDGE_RATE_LIMIT_PER_MINUTE` | Límite máximo global de peticiones por minuto admitidas a través de la pasarela WebSocket. |
| 23 | `log_level` | `str` | `"INFO"` | `LOG_LEVEL` | Nivel de verbosidad del logger (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |

### 2.1 Avisos por IDs de Discord con valor por defecto

Los valores por defecto de `guild_id`, `staff_role_id`, `admin_role_id`, `ceo_premier_role_id`, `ceo_ascend_role_id`, `competition_dept_role_id`, `moderators_channel_id`, `casters_channel_id` y `reglamento_channel_id` son los IDs del servidor oficial de la liga (`DEFAULTED_DISCORD_ID_FIELDS` en `src/liga_bot/config.py`). Se conservan para no romper despliegues cuyo `.env` no los define, pero `get_settings()` llama a `warn_defaulted_discord_ids()` al crear la instancia y registra en el logger `liga_bot.config` un aviso `WARNING` por cada uno que no venga del entorno ni del fichero `.env`, con el nombre de la variable y el valor usado:

```text
STAFF_ROLE_ID no está definida en el entorno; se usa el valor por defecto 1547729760384319518 (servidor de la liga). Defínela en el .env si el bot opera en otro servidor.
```

La comprobación usa `model_fields_set`: un ID definido en el entorno no genera aviso aunque coincida con el valor por defecto. `Settings()` construido directamente (por ejemplo, en tests) no avisa; solo `get_settings()`, una vez por instancia en caché.

`python -m liga_bot` llama a `get_settings()` antes de configurar el logging (`setup_logging` en `src/liga_bot/__main__.py`), así que estos avisos salen por stderr con el manejador por defecto de `logging`, sin marca de tiempo; con el servicio de systemd de `deploy/` aparecen en `journalctl -u liga-bot`.

### 2.2 Ajustes de la auditoría de tickets

`TicketService` lee estos tres campos de `Settings` (grupo «Auditoría de tickets» en `src/liga_bot/config.py`). Sus valores por defecto reproducen el comportamiento anterior a que fueran configurables:

| Atributo Python | Tipo | Valor Predeterminado | Variable de Entorno | Descripción Funcional |
|---|---|---|---|---|
| `organizador_role_id` | `int` | `0` | `ORGANIZADOR_ROLE_ID` | Rol adicional cuyos mensajes cuentan como respuesta del staff en los tickets. `0` lo desactiva. |
| `ticket_revision_hours` | `int` (`>= 1`) | `24` (`DEFAULT_TICKET_REVISION_HOURS`) | `TICKET_REVISION_HOURS` | Horas sin respuesta del staff a partir de las que se avisa en un ticket; también es la ventana mínima entre dos avisos al mismo canal. Un valor menor que 1 hace fallar la carga de la configuración. |
| `tickets_category_name` | `str` | `""` | `TICKETS_CATEGORY_NAME` | Categoría adicional que auditar además de `DEFAULT_TICKETS_CATEGORY_NAMES`. La comparación es exacta tras normalizar el nombre (ver [Servicios de tickets](../features/tickets/services.md)). |

La frecuencia del bucle periódico (`@tasks.loop(hours=24)` en `TicketsCog`) no depende de `TICKET_REVISION_HOURS`.

---

## 3. Validadores y Propiedades Computadas

### 3.1 Validador Estricto de `log_level`

El nivel de logging se procesa mediante un validador en modo previo (`mode="before"`):

```python
@field_validator("log_level", mode="before")
@classmethod
def normalize_log_level(cls, value: str) -> str:
    """Normaliza el nivel de logging a mayúsculas y valida valores permitidos."""
    if isinstance(value, str):
        normalized = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized in allowed:
            return normalized
        raise ValueError(f"Invalid log_level '{value}'. Allowed values: {sorted(allowed)}")
    return value
```

- **Resiliencia:** Acepta valores con espacios y en minúsculas (ej. `" debug "` -> `"DEBUG"`).
- **Control de Errores:** Cualquier valor ajeno al conjunto (`{"CRITICAL", "DEBUG", "ERROR", "INFO", "WARNING"}`) lanza inmediatamente un `ValueError` descriptivo que aborta la carga de la configuración.

### 3.2 Discriminador de Motor de Base de Datos

- **`is_pglite`:**
  ```python
  @property
  def is_pglite(self) -> bool:
      return self.database_url.startswith("pglite")
  ```
- **`is_postgres`:**
  ```python
  @property
  def is_postgres(self) -> bool:
      return self.database_url.startswith("postgres")
  ```

### 3.3 Normalizador de URL Asíncrona (`async_database_url`)

SQLAlchemy 2.0 requiere drivers asíncronos explícitos en su esquema de conexión. En entornos de producción (Heroku, Supabase, Neon, AWS RDS), las cadenas de conexión suelen proveerse con el prefijo `postgres://` o `postgresql://`. La propiedad `async_database_url` normaliza estas URLs de forma transparente para el driver `asyncpg`:

```python
@property
def async_database_url(self) -> str:
    url = self.database_url
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url
```

- Si el esquema es `pglite://`, la URL se preserva sin alteraciones para su consumo por `py-pglite`.
- Si el esquema es `postgres://...` o `postgresql://...`, se transforma en `postgresql+asyncpg://...`.

---

## 4. Constantes Canónicas Preservadas de la Liga

En `src/liga_bot/config.py` se definen las constantes de dominio inmutables de la competición:

### Constantes de Soporte y Tickets
- `DEFAULT_REGLAMENTO_CHANNEL_ID: Final[int] = 1548038711697080491`: valor por defecto de `REGLAMENTO_CHANNEL_ID`.
- `DEFAULT_REGLAMENTO_CHANNEL: Final[str] = "<#1548038711697080491>"`: mención construida a partir del ID anterior. `format_mensaje_2` ya no la usa como valor por defecto: sin argumento `reglamento`, menciona `settings.reglamento_channel_id`.
- `DEFAULT_TICKETS_CATEGORY_NAMES: Final[tuple[str, ...]] = (`
  - `"TICKETS-GENERAL-PREMIER"`
  - `"TICKETS-GENERAL-ASCEND"`
  - `"TICKETS-FICHAJES-PREMIER"`
  - `"TICKETS-FICHAJES-ASCEND"`
  - `"TICKETS-ADMINISTRACION"`
  `)`
- `DEFAULT_TICKET_REVISION_HOURS: Final[int] = 24`
- `DEFAULT_TICKET_AVISO_MARCADOR: Final[str] = "⚠️ TICKET_SIN_RESPUESTA"`

### Constantes de Equipos Oficiales
La liga organiza exactamente 20 equipos oficiales distribuidos en 2 divisiones:

- **División PREMIER (`TEAMS_PREMIER`, 10 equipos):**
  1. Vanguard Gaming
  2. Nexus Esports
  3. Aegis Club
  4. Eclipse Gaming
  5. Apex Predators
  6. Storm Legion
  7. Titan Gaming
  8. Ironclad Esports
  9. Shadow Guard
  10. Valiant Esports

- **División ASCEND (`TEAMS_ASCEND`, 10 equipos):**
  1. Frostbite Esports
  2. Infernal Gaming
  3. Thunder Squad
  4. Venomous Club
  5. Quantum Gaming
  6. Zephyr Esports
  7. Nova Core
  8. Crimson Tide
  9. Spectre Gaming
  10. Blaze Syndicate

- **Totalidad de Equipos (`TEAMS_ALL`):**
  Concatenación de tuplas `TEAMS_PREMIER + TEAMS_ASCEND` (20 equipos en total).

---

## 5. Factoría Singleton y Aislamiento en Pruebas

En `src/liga_bot/config.py` se expone la factoría canónica:

```python
@lru_cache
def get_settings() -> Settings:
    """
    Provee una instancia singleton en caché de Settings.
    Permite limpiar la caché en pruebas unitarias mediante get_settings.cache_clear().
    ...
    """
    settings = Settings()
    warn_defaulted_discord_ids(settings)
    return settings
```

### Mecanismo de Aislamiento para Testing
Dado que `get_settings` está decorado con `@lru_cache`, sucesivas llamadas en el runtime retornan la misma instancia en memoria sin re-leer el archivo `.env`.

Para pruebas automatizadas donde se requiere simular variables de entorno temporales (mediante `monkeypatch.setenv`):
1. Se configuran las variables deseadas con `monkeypatch`.
2. Se ejecuta `get_settings.cache_clear()`.
3. La siguiente invocación a `get_settings()` genera una nueva instancia con los valores sobreescritos.
4. Al finalizar el test, se vuelve a ejecutar `get_settings.cache_clear()` para no contaminar otras suites.
