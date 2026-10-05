"""
Pruebas de que la URL de conexión nunca expone credenciales en logs ni excepciones.

Cubre la descripción segura de la URL (`describe_database_url`), el log de arranque
de `LigaBot.setup_hook` y los errores de `get_engine` con URLs no soportadas o mal formadas.
"""

import logging
import traceback
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from liga_bot.bot import LigaBot
from liga_bot.config import Settings
from liga_bot.database import describe_database_url, get_engine

POSTGRES_URL = "postgresql+asyncpg://usuario:secreto@localhost:5432/liga_bot"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (POSTGRES_URL, "PostgreSQL (postgresql+asyncpg://localhost:5432/liga_bot)"),
        ("postgres://usuario:secreto@db.interna/rcl", "PostgreSQL (postgres://db.interna/rcl)"),
        ("pglite:///:memory:", "PGlite (memoria)"),
        ("pglite:///./.data/pglite_dev_db", "PGlite (ruta local: ./.data/pglite_dev_db)"),
    ],
)
def test_describe_database_url(url: str, expected: str):
    assert describe_database_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        POSTGRES_URL,
        # Usuario y parámetros de consulta también pueden llevar credenciales.
        "postgresql://usuario:pass@localhost/liga_bot?password=secreto",
        "postgresql://secreto:pass@localhost/liga_bot",
        # Contraseña con '@' sin codificar: el parser la reparte en el host.
        "postgresql://usuario:pa@secreto@localhost/liga_bot",
        # URL que SQLAlchemy no sabe interpretar.
        "postgres usuario:secreto",
        "postgresql://usuario:secreto@localhost:abc/liga_bot",
        "mysql://usuario:secreto@localhost/liga_bot",
    ],
)
def test_describe_database_url_never_contains_credentials(url: str):
    assert "secreto" not in describe_database_url(url)


@pytest.mark.asyncio
async def test_setup_hook_log_does_not_contain_password(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """El log de arranque indica motor, host y base de datos, pero no la contraseña."""
    # alembic/env.py usa fileConfig(), que desactiva los loggers ya creados: si otro test
    # aplicó migraciones antes, caplog no recibiría nada y el test pasaría sin comprobar.
    monkeypatch.setattr(logging.getLogger("liga_bot.bot"), "disabled", False)
    settings = Settings(database_url=POSTGRES_URL, bridge_enabled=False)
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock, return_value=mock_engine),
        patch("liga_bot.bot.get_session_factory"),
        caplog.at_level(logging.DEBUG),
    ):
        bot = LigaBot(settings=settings, extensions=())
        bot.load_extension = AsyncMock()
        await bot.setup_hook()
        await bot.close()

    assert "secreto" not in caplog.text
    assert "localhost" in caplog.text
    assert "liga_bot" in caplog.text
    assert "PostgreSQL" in caplog.text


def _full_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


@pytest.mark.asyncio
async def test_get_engine_unsupported_scheme_hides_password():
    """Con un esquema no soportado el error nombra los prefijos admitidos sin credenciales."""
    settings = Settings(database_url="mysql://usuario:secreto@localhost/liga_bot")

    with pytest.raises(ValueError) as exc_info:
        await get_engine(settings)

    message = str(exc_info.value)
    assert "secreto" not in _full_traceback(exc_info.value)
    assert "localhost" in message
    assert "'postgresql://', 'postgres://' o 'pglite://'" in message


@pytest.mark.asyncio
async def test_get_engine_malformed_postgres_url_hides_password():
    """Si SQLAlchemy rechaza la URL, ni el mensaje ni la traza encadenada la exponen."""
    settings = Settings(database_url="postgresql://usuario:secreto@localhost:abc/liga_bot")

    with pytest.raises(ValueError) as exc_info:
        await get_engine(settings)

    assert "secreto" not in _full_traceback(exc_info.value)
    assert "no válida" in str(exc_info.value)
