"""
Pruebas unitarias para el módulo de persistencia y motor dual src/liga_bot/database.py.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.database import (
    close_engine,
    get_engine,
    get_session,
    get_session_factory,
    transactional_session,
)


@pytest.mark.asyncio
async def test_get_engine_pglite_in_memory():
    """Valida la creación, conectividad y cierre de un motor PGlite en memoria."""
    settings = Settings(database_url="pglite:///:memory:")
    engine = await get_engine(settings)
    assert isinstance(engine, AsyncEngine)

    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT 1 AS test_val"))
        assert res.scalar() == 1

    await close_engine(engine)


@pytest.mark.asyncio
async def test_get_engine_pglite_persistent_directory(tmp_path):
    """
    Valida que PGlite funcione sobre un directorio de trabajo específico,
    creando los artefactos de socket y ejecutando consultas sin conflicto.
    """
    db_dir = tmp_path / "pglite_db"
    settings = Settings(database_url=f"pglite://{db_dir}")

    # 1. Crear motor y verificar artefactos y conectividad
    engine1 = await get_engine(settings)
    assert (db_dir / "package.json").exists()
    assert (db_dir / "pglite_manager.js").exists()
    async with engine1.begin() as conn:
        res = await conn.execute(text("SELECT 42"))
        assert res.scalar() == 42
    await close_engine(engine1)

    # 2. Reabrir motor sobre el mismo directorio y verificar reinicio limpio
    engine2 = await get_engine(settings)
    async with engine2.connect() as conn:
        res = await conn.execute(text("SELECT 100"))
        assert res.scalar() == 100
    await close_engine(engine2)


@pytest.mark.asyncio
async def test_get_engine_postgres_with_mock():
    """
    Valida la inicialización de motor PostgreSQL verificando
    parámetros pool_pre_ping y URL asyncpg.
    """
    settings = Settings(
        database_url="postgresql://dbuser:secret@localhost:5432/ligadb",
        log_level="DEBUG",
    )

    with patch("liga_bot.database.create_async_engine") as mock_create:
        mock_engine = MagicMock(spec=AsyncEngine)

        async def fake_dispose():
            pass

        mock_engine.dispose.side_effect = fake_dispose
        mock_create.return_value = mock_engine

        engine = await get_engine(settings)
        assert engine is mock_engine
        mock_create.assert_called_once_with(
            "postgresql+asyncpg://dbuser:secret@localhost:5432/ligadb",
            pool_pre_ping=True,
            echo=True,
        )

        await close_engine(engine)
        mock_engine.dispose.assert_called_once()


@pytest.mark.asyncio
async def test_get_engine_unsupported_url_scheme():
    """Valida que esquemas de base de datos no soportados disparen ValueError explícito."""
    settings = Settings(database_url="mysql+aiomysql://root:pass@localhost/db")
    with pytest.raises(ValueError, match="Esquema de base de datos no soportado"):
        await get_engine(settings)


def test_get_session_factory(async_engine: AsyncEngine):
    """Valida que get_session_factory configure adecuadamente async_sessionmaker."""
    factory = get_session_factory(async_engine)
    assert isinstance(factory, async_sessionmaker)

    session = factory()
    assert isinstance(session, AsyncSession)
    assert session.sync_session.expire_on_commit is False


def test_get_session_factory_uninitialized_raises():
    """Valida que llamar a get_session_factory() sin motor previo dispare RuntimeError."""
    with patch("liga_bot.database._default_engine", None):
        with pytest.raises(RuntimeError, match="No se ha inicializado ningún motor"):
            get_session_factory(None)


@pytest.mark.asyncio
async def test_transactional_session_commit(async_engine: AsyncEngine):
    """Valida que transactional_session haga commit automático de operaciones exitosas."""
    factory = get_session_factory(async_engine)

    async with async_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS t_commit (id INT, val TEXT)"))

    try:
        async with transactional_session(factory) as session:
            await session.execute(text("INSERT INTO t_commit VALUES (10, 'ok')"))

        async with factory() as session:
            res = await session.execute(text("SELECT val FROM t_commit WHERE id = 10"))
            assert res.scalar() == "ok"
    finally:
        async with async_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_commit"))


@pytest.mark.asyncio
async def test_transactional_session_rollback_on_error(async_engine: AsyncEngine):
    """Valida que transactional_session ejecute rollback automático si ocurre una excepción."""
    factory = get_session_factory(async_engine)

    async with async_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS t_rollback (id INT)"))

    try:
        with pytest.raises(RuntimeError, match="Simulated failure"):
            async with transactional_session(factory) as session:
                await session.execute(text("INSERT INTO t_rollback VALUES (999)"))
                raise RuntimeError("Simulated failure")

        async with factory() as session:
            res = await session.execute(text("SELECT COUNT(*) FROM t_rollback WHERE id = 999"))
            assert res.scalar() == 0
    finally:
        async with async_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_rollback"))


@pytest.mark.asyncio
async def test_get_session_helper(async_engine: AsyncEngine):
    """Valida el helper get_session para transacciones exitosas y control de fallos."""
    factory = get_session_factory(async_engine)

    async with async_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS t_helper (val INT)"))

    try:
        async with get_session(factory) as session:
            await session.execute(text("INSERT INTO t_helper VALUES (55)"))

        async with factory() as session:
            res = await session.execute(text("SELECT val FROM t_helper"))
            assert res.scalar() == 55
    finally:
        async with async_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_helper"))


@pytest.mark.asyncio
async def test_close_engine_idempotent():
    """Valida que close_engine sea idempotente y tolere llamadas repetidas o argumentos None."""
    await close_engine(None)
    mock_engine = MagicMock(spec=AsyncEngine)

    async def fake_dispose():
        pass

    mock_engine.dispose.side_effect = fake_dispose

    await close_engine(mock_engine)
    await close_engine(mock_engine)
    assert mock_engine.dispose.call_count == 2


@pytest.mark.asyncio
async def test_pglite_concurrent_sessions_isolation_no_rollback_bleed(
    async_engine: AsyncEngine,
):
    """
    Verifica que dos sesiones asíncronas concurrentes sobre PGlite mantengan
    aislamiento transaccional estricto:
    - La Sesión A ejecuta una primera inserción y realiza un sleep asíncrono.
    - La Sesión B inicia, inserta un registro y realiza un rollback durante el sleep de la Sesión A.
    - Se verifica que la Sesión A confirme ambos registros y la Sesión B persista 0 registros.
    """
    factory = get_session_factory(async_engine)

    async with async_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS t_concurrency_isolation "
                "(id INT PRIMARY KEY, session_name TEXT, step INT)"
            )
        )

    try:

        async def session_a():
            async with transactional_session(factory) as s:
                await s.execute(
                    text("INSERT INTO t_concurrency_isolation VALUES (1, 'session_a', 1)")
                )
                await asyncio.sleep(0.15)
                await s.execute(
                    text("INSERT INTO t_concurrency_isolation VALUES (2, 'session_a', 2)")
                )

        async def session_b():
            await asyncio.sleep(0.05)
            try:
                async with transactional_session(factory) as s:
                    await s.execute(
                        text("INSERT INTO t_concurrency_isolation VALUES (3, 'session_b', 1)")
                    )
                    raise RuntimeError("Fallo simulado para forzar rollback en sesión B")
            except RuntimeError:
                pass

        await asyncio.gather(session_a(), session_b())

        async with factory() as s:
            rows = (
                await s.execute(
                    text("SELECT id, session_name, step FROM t_concurrency_isolation ORDER BY id")
                )
            ).fetchall()

            # Aserciones de regresión:
            # 1. Sesión A confirmó ambos registros
            a_rows = [r for r in rows if r[1] == "session_a"]
            assert len(a_rows) == 2, f"Sesión A sufrió pérdida de datos por rollback bleed: {rows}"
            assert a_rows == [(1, "session_a", 1), (2, "session_a", 2)]

            # 2. Sesión B tiene 0 registros persistidos
            b_rows = [r for r in rows if r[1] == "session_b"]
            assert len(b_rows) == 0, f"Sesión B dejó registros fantasma tras rollback: {rows}"

            # 3. Total exacto de registros persistidos es 2
            assert len(rows) == 2, f"Discrepancia en registros totales: {rows}"

    finally:
        async with async_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_concurrency_isolation"))


@pytest.mark.asyncio
async def test_get_session_concurrency_isolation(async_engine: AsyncEngine):
    """Valida que get_session aísle transacciones concurrentes en motores con StaticPool."""
    factory = get_session_factory(async_engine)

    async with async_engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS t_get_iso (id INT PRIMARY KEY, val TEXT)")
        )

    try:

        async def worker_a():
            async with get_session(factory) as s:
                await s.execute(text("INSERT INTO t_get_iso VALUES (10, 'a1')"))
                await asyncio.sleep(0.15)
                await s.execute(text("INSERT INTO t_get_iso VALUES (20, 'a2')"))

        async def worker_b():
            await asyncio.sleep(0.05)
            try:
                async with get_session(factory) as s:
                    await s.execute(text("INSERT INTO t_get_iso VALUES (30, 'b1')"))
                    raise RuntimeError("Simulated failure in worker B")
            except RuntimeError:
                pass

        await asyncio.gather(worker_a(), worker_b())

        async with factory() as s:
            rows = (await s.execute(text("SELECT id, val FROM t_get_iso ORDER BY id"))).fetchall()
            assert rows == [(10, "a1"), (20, "a2")]
    finally:
        async with async_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_get_iso"))


@pytest.mark.asyncio
async def test_postgres_engine_bypasses_serialization():
    """Valida que un motor PostgreSQL con pool estándar no active la serialización por lock."""
    from liga_bot.database import _requires_serialization

    settings = Settings(database_url="postgresql://dbuser:secret@localhost:5432/ligadb")
    with patch("liga_bot.database.create_async_engine") as mock_create:
        mock_engine = MagicMock(spec=AsyncEngine)
        mock_sync_engine = MagicMock()
        mock_pool = MagicMock()
        mock_sync_engine.pool = mock_pool
        mock_engine.sync_engine = mock_sync_engine

        async def fake_dispose():
            pass

        mock_engine.dispose.side_effect = fake_dispose
        mock_create.return_value = mock_engine

        engine = await get_engine(settings)
        assert _requires_serialization(engine) is False
        await close_engine(engine)


@pytest.mark.asyncio
async def test_close_engine_cleans_engine_lock():
    """Valida que close_engine limpie el lock de _engine_locks para evitar fugas de memoria."""
    from liga_bot.database import _engine_locks, _get_engine_lock

    mock_engine = MagicMock(spec=AsyncEngine)

    async def fake_dispose():
        pass

    mock_engine.dispose.side_effect = fake_dispose

    _get_engine_lock(mock_engine)
    assert mock_engine in _engine_locks

    await close_engine(mock_engine)
    assert mock_engine not in _engine_locks
