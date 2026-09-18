"""
Módulo de base de datos y persistencia para LigaBot.

Proporciona soporte de motor dual:
- PostgreSQL (producción) vía asyncpg.
- PGlite (desarrollo y pruebas) vía py-pglite[sqlalchemy].

Incluye gestión de ciclo de vida del motor (creación y cierre limpio),
factoría de sesiones asíncronas y helpers/context managers transaccionales
para servicios y eventos de Discord.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from py_pglite.config import PGliteConfig
from py_pglite.sqlalchemy.manager_async import SQLAlchemyAsyncPGliteManager
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from liga_bot.config import Settings, get_settings

# Registro de managers de PGlite para cierre limpio del proceso Node.js subyacente
_pglite_managers: dict[AsyncEngine, SQLAlchemyAsyncPGliteManager] = {}
# Registro de locks de serialización por motor para motores de conexión única (PGlite / StaticPool)
_engine_locks: dict[AsyncEngine, asyncio.Lock] = {}
_default_engine: AsyncEngine | None = None
_default_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine_lock(engine: AsyncEngine) -> asyncio.Lock:
    """Devuelve o crea el asyncio.Lock asociado al motor especificado."""
    lock = _engine_locks.get(engine)
    if lock is None:
        lock = asyncio.Lock()
        _engine_locks[engine] = lock
    return lock


def _requires_serialization(engine: AsyncEngine | None) -> bool:
    """
    Determina si un motor de base de datos requiere serialización de transacciones.
    Retorna True para motores PGlite o aquellos que utilicen StaticPool (conexión única compartida).
    Retorna False para motores multi-conexión como PostgreSQL con AsyncAdaptedQueuePool.
    """
    if engine is None:
        return False
    if engine in _pglite_managers:
        return True
    sync_engine = getattr(engine, "sync_engine", None)
    if sync_engine is not None:
        pool = getattr(sync_engine, "pool", None)
        if isinstance(pool, StaticPool):
            return True
    return False


def _parse_pglite_path(database_url: str) -> str | None:
    """Extrae la ruta de trabajo desde una URL con esquema pglite."""
    if database_url.startswith("pglite:///"):
        path = database_url[len("pglite:///") :]
        if path == ":memory:":
            return ":memory:"
        if path.startswith(("./", "../", "/")):
            return path
        return f"/{path}"
    if database_url.startswith("pglite://"):
        return database_url[len("pglite://") :]
    if database_url.startswith("pglite:"):
        return database_url.replace("pglite:", "")
    return None


def _prepare_pglite_config(raw_path: str | None) -> PGliteConfig | None:
    """Prepara la configuración de PGlite asegurando la existencia del directorio de trabajo."""
    if not raw_path or raw_path == ":memory:":
        return None
    work_dir = Path(raw_path).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    socket_path = str(work_dir / ".s.PGSQL.5432")
    return PGliteConfig(work_dir=work_dir, socket_path=socket_path)


async def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """
    Crea o devuelve el motor asíncrono configurado según los ajustes.

    Soporta:
    - PostgreSQL: crea un AsyncEngine vía asyncpg con pool_pre_ping=True.
    - PGlite: inicializa SQLAlchemyAsyncPGliteManager, arranca el servidor,
      espera a que esté listo y devuelve el AsyncEngine conectado.
    """
    global _default_engine
    if settings is None:
        if _default_engine is not None:
            return _default_engine
        settings = get_settings()

    if settings.is_postgres:
        engine = create_async_engine(
            settings.async_database_url,
            pool_pre_ping=True,
            echo=(settings.log_level == "DEBUG"),
        )
    elif settings.is_pglite:
        raw_path = _parse_pglite_path(settings.database_url)
        cfg = _prepare_pglite_config(raw_path)

        manager = SQLAlchemyAsyncPGliteManager(config=cfg)
        manager.start()
        await manager.wait_for_ready()
        engine = manager.get_engine()
        # Registrar manager para que close_engine pueda detener el proceso Node.js
        _pglite_managers[engine] = manager
    else:
        raise ValueError(
            f"Esquema de base de datos no soportado en URL: '{settings.database_url}'. "
            "Se requiere prefijo 'postgresql://', 'postgres://' o 'pglite://'."
        )

    if _default_engine is None:
        _default_engine = engine

    return engine


def get_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """
    Crea una factoría de sesiones asíncronas para el motor proporcionado
    o para el motor por defecto.
    """
    global _default_session_factory
    target_engine = engine or _default_engine
    if target_engine is None:
        raise RuntimeError(
            "No se ha inicializado ningún motor de base de datos. Llame a get_engine() primero."
        )

    if engine is None and _default_session_factory is not None:
        return _default_session_factory

    factory = async_sessionmaker(
        bind=target_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    if engine is None or engine is _default_engine:
        _default_session_factory = factory

    return factory


async def close_engine(engine: AsyncEngine | None = None) -> None:
    """
    Cierra de forma segura el motor asíncrono y libera sus recursos.
    Si el motor corresponde a PGlite, detiene el proceso Node.js subyacente.
    """
    global _default_engine, _default_session_factory
    target_engine = engine or _default_engine
    if target_engine is None:
        return

    _engine_locks.pop(target_engine, None)

    # Verificar si tiene un manager de PGlite asociado
    manager = _pglite_managers.pop(target_engine, None)

    if manager is not None:
        await manager.stop()
    else:
        await target_engine.dispose()

    if target_engine is _default_engine:
        _default_engine = None
        _default_session_factory = None


@asynccontextmanager
async def transactional_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager asíncrono que abre una sesión y una transacción explícita.
    Realiza commit automático al salir sin excepciones o rollback en caso de error.
    Serializa el acceso en motores de conexión única (PGlite / StaticPool).
    """
    factory = session_factory or get_session_factory()
    target_engine = factory.kw.get("bind") or _default_engine
    if isinstance(target_engine, AsyncConnection):
        target_engine = target_engine.engine

    if _requires_serialization(target_engine):
        lock = _get_engine_lock(target_engine)
        async with lock:
            async with factory() as session:
                async with session.begin():
                    yield session
    else:
        async with factory() as session:
            async with session.begin():
                yield session


@asynccontextmanager
async def get_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager / helper de dependencia para sesiones asíncronas estándar.
    Asegura commit al finalizar y rollback si ocurre una excepción no capturada.
    Serializa el acceso en motores de conexión única (PGlite / StaticPool).
    """
    factory = session_factory or get_session_factory()
    target_engine = factory.kw.get("bind") or _default_engine
    if isinstance(target_engine, AsyncConnection):
        target_engine = target_engine.engine

    if _requires_serialization(target_engine):
        lock = _get_engine_lock(target_engine)
        async with lock:
            async with factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
    else:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
