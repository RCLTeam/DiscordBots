"""
Configuración global y fixtures para la suite de pruebas de LigaBot con pytest.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from alembic.config import Config
from py_pglite.sqlalchemy.manager_async import SQLAlchemyAsyncPGliteManager
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from liga_bot.config import get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache():
    """Limpia la caché de get_settings() antes y después de cada test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture(scope="session")
async def pglite_manager() -> AsyncGenerator[SQLAlchemyAsyncPGliteManager, None]:
    """Inicia el servidor local de PGlite para la sesión de pruebas."""
    manager = SQLAlchemyAsyncPGliteManager()
    manager.start()
    await manager.wait_for_ready()
    yield manager
    await manager.stop()


@pytest_asyncio.fixture(scope="session")
async def async_engine(pglite_manager: SQLAlchemyAsyncPGliteManager) -> AsyncEngine:
    """Devuelve el motor asíncrono conectado al socket UNIX de PGlite."""
    return pglite_manager.get_engine()


@pytest_asyncio.fixture(scope="session")
async def migrated_db(async_engine: AsyncEngine) -> AsyncEngine:
    """Aplica las migraciones de Alembic sobre PGlite para pruebas de base de datos."""
    cfg = Config("alembic.ini")

    async with async_engine.connect() as conn:

        def do_upgrade(sync_conn):
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(do_upgrade)

    return async_engine
