import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import DropTable

from alembic import context

# Garantizar que src/ preceda a la raíz en sys.path para evitar sombreado por liga_bot.py
SRC_PATH = str(Path(__file__).resolve().parent.parent / "src")
if SRC_PATH in sys.path:
    sys.path.remove(SRC_PATH)
sys.path.insert(0, SRC_PATH)

# Importar configuración de Alembic
config = context.config

# Configurar logging si existe archivo ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Enlace dinámico y seguro a los metadatos de los modelos
target_metadata = None
try:
    import liga_bot.models  # noqa: F401
    import liga_bot.models.roster  # noqa: F401
    from liga_bot.models.base import Base

    target_metadata = Base.metadata
except ImportError:
    try:
        import liga_bot.models.match  # noqa: F401
        import liga_bot.models.role_request  # noqa: F401
        import liga_bot.models.roster  # noqa: F401
        import liga_bot.models.team  # noqa: F401
        import liga_bot.models.ticket_notice  # noqa: F401
        from liga_bot.models.base import Base

        target_metadata = Base.metadata
    except ImportError:
        try:
            import liga_bot.db.models.match  # noqa: F401
            import liga_bot.db.models.team  # noqa: F401
            import liga_bot.db.models.ticket  # noqa: F401
            from liga_bot.db.models.base import Base

            target_metadata = Base.metadata
        except ImportError:
            target_metadata = None

# Tablas compartidas cuya gobernanza pertenece exclusivamente a RCL-Next (Drizzle ORM).
# Quedan estrictamente excluidas de las migraciones y autogenerate de Alembic en DiscordBots.
SHARED_TABLES: set[str] = {
    "teams",
    "team_memberships",
    "discord_users",
    "players",
    "roster_movements",
    "audit_logs",
}


def include_object(
    object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to,
) -> bool:
    """Excluye tablas compartidas de RCL-Next y objetos asociados de Alembic."""
    if type_ == "table" and name in SHARED_TABLES:
        return False
    table = getattr(object, "table", None)
    if table is not None:
        table_name = getattr(table, "name", None)
        if table_name in SHARED_TABLES:
            return False
    return True


@compiles(DropTable, "postgresql")
def _compile_drop_table(element, compiler, **kw):
    table_name = getattr(element.element, "name", None)
    if table_name == "teams":
        return (
            "DROP TABLE IF EXISTS audit_logs, roster_movements, "
            "team_memberships, players, discord_users CASCADE; "
            + compiler.visit_drop_table(element, **kw)
            + " CASCADE"
        )
    return compiler.visit_drop_table(element, **kw)


def get_database_url() -> str:
    """Obtiene y normaliza la URL de base de datos resolviendo la jerarquía:
    1. Variable de entorno DATABASE_URL (si está presente y no vacía).
    2. Opción 'sqlalchemy.url' de alembic.ini (si está configurada y no es placeholder).
    3. Fallback a Settings().database_url (que lee .env o default 'pglite:///:memory:').
    """
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        ini_url = (config.get_main_option("sqlalchemy.url", "") or "").strip()
        if ini_url and ini_url != "postgresql+asyncpg://postgres:postgres@localhost:5432/liga_bot":
            url = ini_url

    if not url:
        try:
            from liga_bot.config import get_settings

            url = get_settings().database_url
        except Exception:
            url = "pglite:///:memory:"

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def run_migrations_offline() -> None:
    """Ejecuta migraciones en modo 'offline' generando scripts SQL."""
    url = get_database_url()
    # PGlite no es un dialecto nativo registrado en SQLAlchemy;
    # el modo offline solo requiere el compilador DDL de PostgreSQL.
    if url.startswith("pglite"):
        url = "postgresql+asyncpg://postgres:postgres@localhost:5432/liga_bot"

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Aplica las migraciones sobre una conexión síncrona activa."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()

        # Si se ejecuta con conexión inyectada (entorno de pruebas local / test fixtures):
        if config.attributes.get("connection") is not None:
            import liga_bot.models.roster  # noqa: F401
            from liga_bot.models.base import Base

            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            if "teams" in tables:
                shared_tables_to_create = [
                    Base.metadata.tables[tbl]
                    for tbl in [
                        "discord_users",
                        "players",
                        "team_memberships",
                        "roster_movements",
                        "audit_logs",
                    ]
                    if tbl in Base.metadata.tables
                ]
                Base.metadata.create_all(connection, tables=shared_tables_to_create)
            else:
                connection.execute(
                    sa.text(
                        "DROP TABLE IF EXISTS audit_logs, roster_movements, "
                        "team_memberships, players, discord_users CASCADE"
                    )
                )


def _prepare_pglite_config(raw_path: str | None):
    """Prepara la configuración de PGlite asegurando la existencia del directorio de trabajo."""
    from py_pglite.config import PGliteConfig

    if not raw_path or raw_path == ":memory:":
        return None
    work_dir = Path(raw_path).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    socket_path = str(work_dir / ".s.PGSQL.5432")
    return PGliteConfig(work_dir=work_dir, socket_path=socket_path)


async def run_async_migrations() -> None:
    """Ejecuta migraciones en modo asíncrono con soporte dual PostgreSQL / PGlite."""
    # 1. Conexión explícita inyectada (p. ej. en fixtures de tests)
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        if isinstance(connectable, Connection):
            do_run_migrations(connectable)
        else:
            await connectable.run_sync(do_run_migrations)
        return

    # 2. Motor explícito inyectado en attributes
    engine = config.attributes.get("engine", None)
    if engine is not None:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
        return

    # 3. Resolución por URL
    db_url = get_database_url()

    # Manejo especial para PGlite desde CLI
    if db_url.startswith("pglite"):
        from py_pglite.sqlalchemy.manager_async import SQLAlchemyAsyncPGliteManager

        if db_url.startswith("pglite:///"):
            path = db_url[len("pglite:///") :]
            if path == ":memory:":
                path = ":memory:"
            elif not path.startswith(("./", "../", "/")):
                path = f"/{path}"
        elif db_url.startswith("pglite://"):
            path = db_url[len("pglite://") :]
        else:
            path = db_url.replace("pglite:", "")

        cfg = _prepare_pglite_config(path)

        manager = SQLAlchemyAsyncPGliteManager(config=cfg)
        manager.start()
        await manager.wait_for_ready()
        pg_engine = manager.get_engine()
        try:
            async with pg_engine.connect() as connection:
                await connection.run_sync(do_run_migrations)
        finally:
            await manager.stop()
        return

    # Modo estándar producción (PostgreSQL con asyncpg)
    config.set_main_option("sqlalchemy.url", db_url)
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Punto de entrada para modo online."""
    # Si se inyectó una conexión síncrona en attributes, evitar asyncio.run()
    connectable = config.attributes.get("connection", None)
    if connectable is not None and isinstance(connectable, Connection):
        do_run_migrations(connectable)
        return

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
