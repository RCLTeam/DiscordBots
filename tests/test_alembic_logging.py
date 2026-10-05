"""Aplicar las migraciones de Alembic no debe desactivar los loggers del bot.

``alembic/env.py`` configura el logging con ``fileConfig``; sin
``disable_existing_loggers=False`` desactiva todos los loggers ya creados que no aparecen
en ``alembic.ini``, entre ellos los de ``liga_bot.*``, y ``caplog`` deja de recibir sus
registros en el resto de la sesión de pytest.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

# Se crea al importar el módulo, durante la colección: antes de que la fixture
# migrated_db ejecute las migraciones, igual que los loggers de los módulos del bot.
_BOT_LOGGER = logging.getLogger("liga_bot.bot")


@pytest.mark.asyncio
async def test_migrations_keep_bot_logger_enabled(migrated_db: AsyncEngine, caplog):
    assert _BOT_LOGGER.disabled is False

    with caplog.at_level(logging.WARNING, logger="liga_bot.bot"):
        _BOT_LOGGER.warning("registro tras aplicar las migraciones")

    assert [r.getMessage() for r in caplog.records if r.name == "liga_bot.bot"] == [
        "registro tras aplicar las migraciones"
    ]
