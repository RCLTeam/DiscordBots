"""
Módulo de configuración y variables de entorno para LigaBot.

Utiliza Pydantic Settings v2 para validación estricta de tipos,
resolución de variables de entorno y preservación de constantes
canónicas de la liga.
"""

from functools import lru_cache
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Constantes canónicas auxiliares de la liga (preservadas de liga_bot.py:41-52)
DEFAULT_REGLAMENTO_CHANNEL: Final[str] = "📜𝗥𝗘𝗚𝗟𝗔𝗠𝗘𝗡𝗧𝗢📜"
DEFAULT_TICKETS_CATEGORY_NAMES: Final[tuple[str, ...]] = (
    "TICKETS-GENERAL-PREMIER",
    "TICKETS-GENERAL-ASCEND",
    "TICKETS-FICHAJES-PREMIER",
    "TICKETS-FICHAJES-ASCEND",
    "TICKETS-ADMINISTRACION",
)
DEFAULT_TICKET_REVISION_HOURS: Final[int] = 24
DEFAULT_TICKET_AVISO_MARCADOR: Final[str] = "⚠️ TICKET_SIN_RESPUESTA"

# Constantes canónicas de equipos oficiales de la liga
TEAMS_PREMIER: tuple[str, ...] = (
    "Vanguard Gaming",
    "Nexus Esports",
    "Aegis Club",
    "Eclipse Gaming",
    "Apex Predators",
    "Storm Legion",
    "Titan Gaming",
    "Ironclad Esports",
    "Shadow Guard",
    "Valiant Esports",
)
TEAMS_ASCEND: tuple[str, ...] = (
    "Frostbite Esports",
    "Infernal Gaming",
    "Thunder Squad",
    "Venomous Club",
    "Quantum Gaming",
    "Zephyr Esports",
    "Nova Core",
    "Crimson Tide",
    "Spectre Gaming",
    "Blaze Syndicate",
)
TEAMS_ALL: tuple[str, ...] = TEAMS_PREMIER + TEAMS_ASCEND


class Settings(BaseSettings):
    """
    Configuración global de la aplicación respaldada por variables de entorno.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Autenticación y Servidor Discord
    discord_token: str = Field(
        default="",
        description="Token secreto del bot de Discord. Obligatorio para ejecutar el bot.",
    )
    guild_id: int = Field(
        default=1547725310508667010,
        description="ID del servidor de Discord de la liga.",
    )

    # Roles de Discord
    staff_role_id: int = Field(
        default=1547729760384319518,
        description="ID del rol de Staff / Árbitros.",
    )
    admin_role_id: int = Field(
        default=1548795786110967919,
        description="ID del rol de Administradores.",
    )
    ceo_premier_role_id: int = Field(
        default=1548795782360993842,
        description="ID del rol de CEO de la división Premier.",
    )
    ceo_ascend_role_id: int = Field(
        default=1548795784655405087,
        description="ID del rol de CEO de la división Ascend.",
    )
    ceo_role_id: int = Field(
        default=0,
        description="ID del rol de CEO general.",
    )
    sin_verificar_role_id: int = Field(
        default=0,
        description="ID del rol Sin Verificar asignado a nuevos miembros.",
    )
    ticket_rol_category_id: int = Field(
        default=0,
        description="ID de categoría de Discord para tickets de verificación de rol.",
    )
    free_role_name: str = Field(
        default="Libre",
        description="Nombre del rol asignado a agentes libres.",
    )

    # Persistencia y Motores Duales
    database_url: str = Field(
        default="pglite:///:memory:",
        description="URL de conexión SQLAlchemy (PGlite o PostgreSQL).",
    )

    # WebSocket Bridge y Sugerencias
    bridge_enabled: bool = Field(
        default=True,
        description="Habilita o deshabilita el servidor WebSocket interno de comandos.",
    )
    bridge_host: str = Field(
        default="127.0.0.1",
        description="Host local donde escucha el servidor WebSocket.",
    )
    bridge_port: int = Field(
        default=8765,
        description="Puerto para el servidor WebSocket de integración.",
    )
    discord_bot_supertoken: str = Field(
        default="",
        description="Supertoken secreto requerido para autenticar el WebSocket.",
    )
    suggestions_channel_id: int = Field(
        default=0,
        description="ID del canal de Discord donde se publicarán las sugerencias.",
    )
    suggestions_rate_limit_per_minute: int = Field(
        default=10,
        description="Máximo número de sugerencias admitidas por minuto a través del bridge.",
    )

    # Diagnóstico y Logging
    log_level: str = Field(
        default="INFO",
        description="Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )

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

    @property
    def is_pglite(self) -> bool:
        """Indica si el motor configurado es PGlite."""
        return self.database_url.startswith("pglite")

    @property
    def is_postgres(self) -> bool:
        """Indica si el motor configurado es PostgreSQL estándar."""
        return self.database_url.startswith("postgres")

    @property
    def async_database_url(self) -> str:
        """
        Devuelve una URL compatible con los motores asíncronos de SQLAlchemy 2.0.
        Normaliza automáticamente los esquemas postgres:// y postgresql:// a postgresql+asyncpg://.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    """
    Provee una instancia singleton en caché de Settings.
    Permite limpiar la caché en pruebas unitarias mediante get_settings.cache_clear().
    """
    return Settings()
