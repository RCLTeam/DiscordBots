"""
Pruebas unitarias para el módulo de configuración src/liga_bot/config.py
y verificación de compatibilidad de migraciones Alembic sobre PGlite.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from liga_bot.config import (
    DEFAULT_REGLAMENTO_CHANNEL,
    DEFAULT_TICKET_AVISO_MARCADOR,
    DEFAULT_TICKET_REVISION_HOURS,
    DEFAULT_TICKETS_CATEGORY_NAMES,
    Settings,
    get_settings,
)


def test_default_settings_canonical_values():
    """Valida que los valores por defecto coincidan exactamente con las constantes canónicas."""
    settings = Settings()
    assert settings.guild_id == 1547725310508667010
    assert settings.staff_role_id == 1547729760384319518
    assert settings.admin_role_id == 1548795786110967919
    assert settings.ceo_premier_role_id == 1548795782360993842
    assert settings.ceo_ascend_role_id == 1548795784655405087
    assert settings.competition_dept_role_id == 1548795790896398448
    assert settings.database_url == "pglite:///:memory:"
    assert settings.log_level == "INFO"
    assert settings.discord_token == ""
    assert settings.is_pglite is True
    assert settings.is_postgres is False


def test_environment_overrides(monkeypatch):
    """Valida que las variables de entorno sobreescriban los valores predeterminados."""
    monkeypatch.setenv("DISCORD_TOKEN", "fake-token-42")
    monkeypatch.setenv("GUILD_ID", "999888777")
    monkeypatch.setenv("STAFF_ROLE_ID", "111222333")
    monkeypatch.setenv("ADMIN_ROLE_ID", "444555666")
    monkeypatch.setenv("CEO_PREMIER_ROLE_ID", "777888999")
    monkeypatch.setenv("CEO_ASCEND_ROLE_ID", "123123123")
    monkeypatch.setenv("COMPETITION_DEPT_ROLE_ID", "555666777")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    settings = Settings()
    assert settings.discord_token == "fake-token-42"
    assert settings.guild_id == 999888777
    assert settings.staff_role_id == 111222333
    assert settings.admin_role_id == 444555666
    assert settings.ceo_premier_role_id == 777888999
    assert settings.ceo_ascend_role_id == 123123123
    assert settings.competition_dept_role_id == 555666777
    assert settings.database_url == "postgresql+asyncpg://user:pass@localhost:5432/testdb"
    assert settings.log_level == "DEBUG"
    assert settings.is_pglite is False
    assert settings.is_postgres is True


def test_async_database_url_normalization():
    """Valida la conversión de esquemas postgres estándar hacia el driver asyncpg."""
    s1 = Settings(database_url="postgresql://user:pass@host:5432/db")
    assert s1.async_database_url == "postgresql+asyncpg://user:pass@host:5432/db"

    s2 = Settings(database_url="postgres://user:pass@host:5432/db")
    assert s2.async_database_url == "postgresql+asyncpg://user:pass@host:5432/db"

    s3 = Settings(database_url="pglite:///:memory:")
    assert s3.async_database_url == "pglite:///:memory:"


def test_competition_dept_role_id_default():
    """Verifica el valor por defecto del rol de Departamento de Competición."""
    from liga_bot.config import DEFAULTED_DISCORD_ID_FIELDS

    settings = Settings()
    assert settings.competition_dept_role_id == 1548795790896398448
    assert "competition_dept_role_id" in DEFAULTED_DISCORD_ID_FIELDS


def test_competition_dept_role_id_env_override(monkeypatch):
    """Verifica que la variable de entorno sobreescribe el rol por defecto."""
    monkeypatch.setenv("COMPETITION_DEPT_ROLE_ID", "999888777")
    settings = Settings()
    assert settings.competition_dept_role_id == 999888777


def test_get_settings_singleton_and_cache_clear():
    """Valida el comportamiento singleton con lru_cache."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()
    s3 = get_settings()
    assert s3 is not s1


def test_invalid_types_raise_validation_error():
    """Valida que entradas no numéricas en campos enteros disparen ValidationError."""
    with pytest.raises(ValidationError):
        Settings(guild_id="not-an-integer-id")


def test_invalid_log_level_raises_validation_error():
    """Valida que un nivel de logging no permitido dispare ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(log_level="INVALID")
    assert "Invalid log_level 'INVALID'" in str(exc_info.value)


def test_environment_override_invalid_log_level(monkeypatch):
    """Valida que una variable de entorno LOG_LEVEL inválida dispare ValidationError."""
    monkeypatch.setenv("LOG_LEVEL", "BOGUS")
    with pytest.raises(ValidationError):
        Settings()


def test_canonical_domain_constants():
    """Valida que las constantes de dominio auxiliares estén correctamente definidas."""
    assert DEFAULT_REGLAMENTO_CHANNEL == "<#1548038711697080491>"
    assert len(DEFAULT_TICKETS_CATEGORY_NAMES) == 5
    assert "TICKETS-GENERAL-PREMIER" in DEFAULT_TICKETS_CATEGORY_NAMES
    assert DEFAULT_TICKET_REVISION_HOURS == 24
    assert DEFAULT_TICKET_AVISO_MARCADOR == "⚠️ TICKET_SIN_RESPUESTA"


@pytest.mark.asyncio
async def test_alembic_migrations_lifecycle_on_pglite(async_engine: AsyncEngine):
    """Valida que las migraciones de Alembic se apliquen y reviertan limpiamente sobre PGlite."""
    cfg = Config("alembic.ini")

    # 1. Upgrade a head
    async with async_engine.connect() as conn:

        def do_upgrade(sync_conn):
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(do_upgrade)

    # 2. Verificar existencia de tablas creadas por la revisión 001
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        tables = [row[0] for row in result.fetchall()]
        assert "alembic_version" in tables
        assert "teams" in tables
        assert "matches" in tables
        assert "ticket_notices" in tables

    # 3. Downgrade a base para certificar reversibilidad limpia
    async with async_engine.connect() as conn:

        def do_downgrade(sync_conn):
            cfg.attributes["connection"] = sync_conn
            command.downgrade(cfg, "base")

        await conn.run_sync(do_downgrade)

    # 4. Verificar que las tablas fueron eliminadas
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('teams', 'matches', 'ticket_notices')"
            )
        )
        remaining = result.fetchall()
        assert len(remaining) == 0

    # 5. Re-aplicar para dejar la BD lista
    async with async_engine.connect() as conn:

        def do_upgrade_final(sync_conn):
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(do_upgrade_final)


def test_alembic_cli_with_pglite_file_path(tmp_path):
    """Valida que Alembic CLI ejecute migraciones sobre una ruta persistente de PGlite."""
    db_path = tmp_path / "pglite_test_db"
    db_url = f"pglite://{db_path}"
    env = {**os.environ, "DATABASE_URL": db_url}

    # 1. Primera ejecución (creación y migración a head)
    res1 = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, f"Alembic upgrade failed: {res1.stderr}"
    assert "0000, initial_shared_tables" in (res1.stdout + res1.stderr)
    assert "0001, bot_tables" in (res1.stdout + res1.stderr)
    assert (db_path / "pglite_manager.js").exists()
    assert (db_path / "package.json").exists()
    assert str(db_path / ".s.PGSQL.5432") in (db_path / "pglite_manager.js").read_text()

    # 2. Segunda ejecución sobre la misma ruta (verificación de no-bloqueo por socket reuse)
    res2 = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"Second Alembic upgrade failed: {res2.stderr}"


def test_alembic_cli_default_fallback_without_env_var():
    """
    Valida que Alembic CLI ejecute migraciones con fallback en memoria
    cuando DATABASE_URL está ausente.
    """
    clean_env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    # Ignorar el .env local del desarrollador: aquí se valida el fallback por defecto.
    clean_env["LIGA_BOT_ENV_FILE"] = ".env.inexistente"
    res = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic default upgrade failed: {res.stderr}"
    assert "0000, initial_shared_tables" in (res.stdout + res.stderr)
    assert "0001, bot_tables" in (res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# IDs de Discord con valor por defecto y sincronía de la documentación
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULTED_ID_VARS = (
    "GUILD_ID",
    "STAFF_ROLE_ID",
    "ADMIN_ROLE_ID",
    "CEO_PREMIER_ROLE_ID",
    "CEO_ASCEND_ROLE_ID",
    "COMPETITION_DEPT_ROLE_ID",
    "MODERATORS_CHANNEL_ID",
    "CASTERS_CHANNEL_ID",
    "REGLAMENTO_CHANNEL_ID",
)


def _clear_id_env(monkeypatch):
    for var in _DEFAULTED_ID_VARS:
        monkeypatch.delenv(var, raising=False)
    # Las migraciones de Alembic ejecutadas en esta suite llaman a fileConfig(),
    # que deshabilita los loggers ya existentes.
    monkeypatch.setattr(logging.getLogger("liga_bot.config"), "disabled", False)


def test_reglamento_channel_id_default_and_override(monkeypatch):
    """REGLAMENTO_CHANNEL_ID conserva el canal actual por defecto y se puede sobrescribir."""
    _clear_id_env(monkeypatch)
    assert Settings().reglamento_channel_id == 1548038711697080491
    assert DEFAULT_REGLAMENTO_CHANNEL == f"<#{Settings().reglamento_channel_id}>"
    monkeypatch.setenv("REGLAMENTO_CHANNEL_ID", "42")
    assert Settings().reglamento_channel_id == 42


def test_get_settings_warns_for_each_defaulted_id(monkeypatch, caplog):
    """Sin STAFF_ROLE_ID (ni el resto de IDs) en el entorno, se avisa nombrando cada variable."""
    _clear_id_env(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="liga_bot.config"):
        settings = get_settings()

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for var in _DEFAULTED_ID_VARS:
        matching = [m for m in messages if var in m]
        assert len(matching) == 1, f"Falta el aviso de {var}: {messages}"
    staff_msg = next(m for m in messages if "STAFF_ROLE_ID" in m)
    assert str(settings.staff_role_id) in staff_msg


def test_get_settings_does_not_warn_for_ids_defined_in_env(monkeypatch, caplog):
    """Un ID definido en el entorno (aunque coincida con el valor por defecto) no genera aviso."""
    _clear_id_env(monkeypatch)
    monkeypatch.setenv("STAFF_ROLE_ID", "1547729760384319518")
    monkeypatch.setenv("GUILD_ID", "123")
    monkeypatch.setenv("COMPETITION_DEPT_ROLE_ID", "1548795790896398448")
    with caplog.at_level(logging.WARNING, logger="liga_bot.config"):
        get_settings()

    messages = [r.getMessage() for r in caplog.records]
    assert not any("STAFF_ROLE_ID" in m for m in messages)
    assert not any("GUILD_ID" in m for m in messages)
    assert not any("COMPETITION_DEPT_ROLE_ID" in m for m in messages)
    assert any("ADMIN_ROLE_ID" in m for m in messages)


def _env_example_vars() -> set[str]:
    text_env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text_env, flags=re.MULTILINE))


def _settings_env_vars() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_lists_exactly_the_settings_fields():
    """Cada campo de Settings aparece en .env.example, y no hay variables sobrantes."""
    assert _env_example_vars() == _settings_env_vars()


@pytest.mark.parametrize(
    "doc_path",
    ["README.md", "docs/architecture/configuration.md"],
)
def test_docs_variable_tables_list_every_settings_field(doc_path):
    """Las tablas de variables del README y de configuration.md recogen todos los campos."""
    text_doc = (REPO_ROOT / doc_path).read_text(encoding="utf-8")
    table_rows = [line for line in text_doc.splitlines() if line.startswith("|")]
    documented = {name for row in table_rows for name in re.findall(r"`([A-Z][A-Z0-9_]+)`", row)}
    missing = _settings_env_vars() - documented
    assert not missing, f"{doc_path} no documenta: {sorted(missing)}"
    count = len(Settings.model_fields)
    assert f"{count} variables" in text_doc
    stale = {f"{n} variables" for n in range(10, 40) if n != count}
    assert not any(s in text_doc for s in stale), f"{doc_path} conserva un recuento desfasado"


def test_no_docs_contain_stale_variable_counts():
    """Certifica que ningún documento técnico (.md) contiene conteos de variables desfasados."""
    count = len(Settings.model_fields)
    stale_patterns = [f"{n} variable" for n in range(10, 40) if n != count]
    for md_file in REPO_ROOT.glob("docs/**/*.md"):
        content = md_file.read_text(encoding="utf-8").lower()
        for stale in stale_patterns:
            assert stale not in content, (
                f"{md_file.relative_to(REPO_ROOT)} contiene conteo desfasado: '{stale}'"
            )
