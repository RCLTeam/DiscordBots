"""Pruebas unitarias para la configuración del WebSocket Bridge y Sugerencias
en src/liga_bot/config.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from liga_bot.config import Settings, get_settings


def test_bridge_settings_defaults():
    """Valida los valores por defecto canónicos para las 6 variables del bridge."""
    settings = Settings()
    assert settings.bridge_enabled is True
    assert settings.bridge_host == "127.0.0.1"
    assert settings.bridge_port == 8765
    assert settings.discord_bot_supertoken == ""
    assert settings.suggestions_channel_id == 0
    assert settings.bridge_rate_limit_per_minute == 10
    assert hasattr(settings, "_".join(["suggestions", "rate", "limit", "per", "minute"])) is False


def test_bridge_settings_custom_values():
    """Valida que los argumentos explícitos del constructor asignen correctamente las variables."""
    settings = Settings(
        bridge_enabled=False,
        bridge_host="0.0.0.0",
        bridge_port=9999,
        discord_bot_supertoken="custom-secret-token",
        suggestions_channel_id=123456789012345678,
        bridge_rate_limit_per_minute=25,
    )
    assert settings.bridge_enabled is False
    assert settings.bridge_host == "0.0.0.0"
    assert settings.bridge_port == 9999
    assert settings.discord_bot_supertoken == "custom-secret-token"
    assert settings.suggestions_channel_id == 123456789012345678
    assert settings.bridge_rate_limit_per_minute == 25


def test_bridge_settings_env_overrides(monkeypatch):
    """Valida que las variables de entorno sobreescriban los valores predeterminados."""
    monkeypatch.setenv("BRIDGE_ENABLED", "false")
    monkeypatch.setenv("BRIDGE_HOST", "192.168.1.50")
    monkeypatch.setenv("BRIDGE_PORT", "9123")
    monkeypatch.setenv("DISCORD_BOT_SUPERTOKEN", "env-supertoken-value")
    monkeypatch.setenv("SUGGESTIONS_CHANNEL_ID", "998877665544332211")
    monkeypatch.setenv("BRIDGE_RATE_LIMIT_PER_MINUTE", "60")

    settings = Settings()
    assert settings.bridge_enabled is False
    assert settings.bridge_host == "192.168.1.50"
    assert settings.bridge_port == 9123
    assert settings.discord_bot_supertoken == "env-supertoken-value"
    assert settings.suggestions_channel_id == 998877665544332211
    assert settings.bridge_rate_limit_per_minute == 60


def test_bridge_settings_case_insensitivity(monkeypatch):
    """Valida la insensibilidad a mayúsculas/minúsculas en los nombres de variables de entorno."""
    monkeypatch.setenv("bridge_enabled", "0")
    monkeypatch.setenv("Bridge_Host", "10.0.0.5")
    monkeypatch.setenv("bridge_port", "8888")
    monkeypatch.setenv("Discord_Bot_Supertoken", "case-insensitive-token")
    monkeypatch.setenv("suggestions_channel_id", "555555555555555555")
    monkeypatch.setenv("Bridge_Rate_Limit_Per_Minute", "15")

    settings = Settings()
    assert settings.bridge_enabled is False
    assert settings.bridge_host == "10.0.0.5"
    assert settings.bridge_port == 8888
    assert settings.discord_bot_supertoken == "case-insensitive-token"
    assert settings.suggestions_channel_id == 555555555555555555
    assert settings.bridge_rate_limit_per_minute == 15


@pytest.mark.parametrize(
    ("raw_val", "expected"),
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_bridge_settings_boolean_env_variants(monkeypatch, raw_val, expected):
    """Valida la conversión de representaciones de texto estándar a booleano."""
    monkeypatch.setenv("BRIDGE_ENABLED", raw_val)
    settings = Settings()
    assert settings.bridge_enabled is expected


def test_bridge_settings_invalid_types_raise_validation_error():
    """Valida que tipos incompatibles en los parámetros del constructor disparen ValidationError."""
    with pytest.raises(ValidationError):
        Settings(bridge_port="not-a-port")

    with pytest.raises(ValidationError):
        Settings(suggestions_channel_id="invalid-channel-id")

    with pytest.raises(ValidationError):
        Settings(bridge_rate_limit_per_minute="fast")

    with pytest.raises(ValidationError):
        Settings(bridge_enabled="not-a-bool")


def test_bridge_settings_invalid_env_types_raise_validation_error(monkeypatch):
    """Valida que valores de entorno no numéricos en campos enteros disparen ValidationError."""
    monkeypatch.setenv("BRIDGE_PORT", "not-a-number")
    with pytest.raises(ValidationError):
        Settings()


def test_bridge_settings_get_settings_integration(monkeypatch):
    """Valida la interacción con el singleton get_settings() y la limpieza de caché."""
    get_settings.cache_clear()
    s1 = get_settings()
    assert s1.bridge_port == 8765

    monkeypatch.setenv("BRIDGE_PORT", "7777")
    # Antes de limpiar la caché, mantiene el valor previo
    s2 = get_settings()
    assert s2.bridge_port == 8765

    get_settings.cache_clear()
    s3 = get_settings()
    assert s3.bridge_port == 7777
    get_settings.cache_clear()


def test_bridge_settings_preserves_existing_configuration():
    """Valida que la presencia de los campos del bridge no altere la configuración preexistente."""
    settings = Settings()
    assert settings.guild_id == 1547725310508667010
    assert settings.staff_role_id == 1547729760384319518
    assert settings.admin_role_id == 1548795786110967919
    assert settings.database_url == "pglite:///:memory:"
    assert settings.log_level == "INFO"
