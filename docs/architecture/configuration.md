# Sistema de Configuración y Variables de Entorno

[⬅️ Volver a Arquitectura](./README.md)

Este documento detalla la arquitectura de configuración del bot, implementada en `src/liga_bot/config.py` mediante **Pydantic Settings v2**. Cubre la matriz completa de las 18 variables de entorno, las constantes canónicas de la liga, los validadores de campo, las propiedades computadas para el motor de base de datos y la factoría singleton en caché.

---

## 1. Arquitectura de Configuración (`Settings`)

La configuración global de `LigaBot` está encapsulada en la clase `Settings(BaseSettings)` (`src/liga_bot/config.py:55-183`). Utiliza Pydantic Settings v2 para garantizar tipado estricto, lectura automática de archivos `.env` y variables de entorno del sistema operativo, con capacidad de sobreescritura controlada.

### Configuración del Modelo (`model_config`)

En `src/liga_bot/config.py:60-65`:

```python
model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)
```

- **`env_file=".env"`**: Lee de manera predeterminada el archivo `.env` en la raíz de ejecución del proyecto.
- **`env_file_encoding="utf-8"`**: Asegura la interpretación de caracteres UTF-8 en rutas y secretos.
- **`case_sensitive=False`**: Permite definir variables de entorno tanto en mayúsculas (`DISCORD_TOKEN`) como en minúsculas (`discord_token`).
- **`extra="ignore"`**: Ignora variables adicionales presentes en el entorno sin disparar errores de validación.

---

## 2. Matriz Exhaustiva de las 18 Variables de Entorno

A continuación se detalla la totalidad de los 18 campos configurables en `Settings`, organizados por dominio funcional:

| # | Atributo Python | Línea | Tipo | Valor Predeterminado | Variable de Entorno | Descripción Funcional |
|---|---|---|---|---|---|---|
| 1 | `discord_token` | 68-71 | `str` | `""` | `DISCORD_TOKEN` | Token secreto de autenticación del bot en la API de Discord. |
| 2 | `guild_id` | 72-75 | `int` | `1547725310508667010` | `GUILD_ID` | Snowflake ID del servidor principal de Discord de la liga. |
| 3 | `staff_role_id` | 78-81 | `int` | `1547729760384319518` | `STAFF_ROLE_ID` | Snowflake ID del rol asignado a árbitros y personal de Staff. |
| 4 | `admin_role_id` | 82-85 | `int` | `1548795786110967919` | `ADMIN_ROLE_ID` | Snowflake ID del rol de Administradores de la liga. |
| 5 | `ceo_premier_role_id` | 86-89 | `int` | `1548795782360993842` | `CEO_PREMIER_ROLE_ID` | Snowflake ID del rol otorgado a capitanes/CEOs de la división Premier. |
| 6 | `ceo_ascend_role_id` | 90-93 | `int` | `1548795784655405087` | `CEO_ASCEND_ROLE_ID` | Snowflake ID del rol otorgado a capitanes/CEOs de la división Ascend. |
| 7 | `ceo_role_id` | 94-97 | `int` | `0` | `CEO_ROLE_ID` | Snowflake ID del rol de CEO unificado o general. |
| 8 | `sin_verificar_role_id` | 98-101 | `int` | `0` | `SIN_VERIFICAR_ROLE_ID` | Snowflake ID del rol asignado a usuarios recién ingresados sin verificar. |
| 9 | `ticket_rol_category_id` | 102-105 | `int` | `0` | `TICKET_ROL_CATEGORY_ID` | Snowflake ID de la categoría donde se generan canales de solicitud de roles. |
| 10 | `free_role_name` | 106-109 | `str` | `"Libre"` | `FREE_ROLE_NAME` | Nombre textual del rol asignado a jugadores en condición de agente libre. |
| 11 | `database_url` | 112-115 | `str` | `"pglite:///:memory:"` | `DATABASE_URL` | URI de conexión para SQLAlchemy (compatible con esquemas PGlite y PostgreSQL). |
| 12 | `bridge_enabled` | 118-121 | `bool` | `True` | `BRIDGE_ENABLED` | Conmutador booleano maestro para iniciar o deshabilitar el servidor WebSocket local. |
| 13 | `bridge_host` | 122-125 | `str` | `"127.0.0.1"` | `BRIDGE_HOST` | Dirección IP de enlace para el servidor WebSocket local. |
| 14 | `bridge_port` | 126-129 | `int` | `8765` | `BRIDGE_PORT` | Puerto TCP de enlace para el servidor WebSocket local. |
| 15 | `discord_bot_supertoken` | 130-133 | `str` | `""` | `DISCORD_BOT_SUPERTOKEN` | Clave secreta compartida requerida en el handshake de autenticación WebSocket. |
| 16 | `suggestions_channel_id` | 134-137 | `int` | `0` | `SUGGESTIONS_CHANNEL_ID` | Snowflake ID del canal de Discord donde se publican las sugerencias web. |
| 17 | `suggestions_rate_limit_per_minute` | 138-141 | `int` | `10` | `SUGGESTIONS_RATE_LIMIT_PER_MINUTE` | Tasa máxima de sugerencias aceptadas por minuto a través del bridge. |
| 18 | `log_level` | 144-147 | `str` | `"INFO"` | `LOG_LEVEL` | Nivel de verbosidad del logger (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |

---

## 3. Validadores y Propiedades Computadas

### 3.1 Validador Estricto de `log_level` (`src/liga_bot/config.py:149-160`)

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

- **`is_pglite` (`src/liga_bot/config.py:162-164`):**
  ```python
  @property
  def is_pglite(self) -> bool:
      return self.database_url.startswith("pglite")
  ```
- **`is_postgres` (`src/liga_bot/config.py:166-170`):**
  ```python
  @property
  def is_postgres(self) -> bool:
      return self.database_url.startswith("postgres")
  ```

### 3.3 Normalizador de URL Asíncrona (`async_database_url`, líneas 172-183)

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

En `src/liga_bot/config.py:16-52`, se definen las constantes de dominio inmutables de la competición:

### Constantes de Soporte y Tickets
- `DEFAULT_REGLAMENTO_CHANNEL: Final[str] = "📜𝗥𝗘𝗚𝗟𝗔𝗠𝗘𝗡𝗧𝗢📜"`
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

En `src/liga_bot/config.py:185-191`, se expone la factoría canónica:

```python
@lru_cache
def get_settings() -> Settings:
    """
    Provee una instancia singleton en caché de Settings.
    Permite limpiar la caché en pruebas unitarias mediante get_settings.cache_clear().
    """
    return Settings()
```

### Mecanismo de Aislamiento para Testing
Dado que `get_settings` está decorado con `@lru_cache`, sucesivas llamadas en el runtime retornan la misma instancia en memoria sin re-leer el archivo `.env`.

Para pruebas automatizadas donde se requiere simular variables de entorno temporales (mediante `monkeypatch.setenv`):
1. Se configuran las variables deseadas con `monkeypatch`.
2. Se ejecuta `get_settings.cache_clear()`.
3. La siguiente invocación a `get_settings()` genera una nueva instancia con los valores sobreescritos.
4. Al finalizar el test, se vuelve a ejecutar `get_settings.cache_clear()` para no contaminar otras suites.
