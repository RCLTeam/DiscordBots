"""
Pruebas unitarias para el CLI administrativo en src/liga_bot/cli.py.

Verifica el parsing de argumentos, carga de archivos JSON/CSV, inserción
idempotente con TeamRepository y ciclo de vida del motor de base de datos sobre PGlite.
"""

import json
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.cli import (
    CANONICAL_DEFAULT_TEAMS,
    build_parser,
    load_teams_from_file,
    main,
    run_seed_command,
    seed_teams,
)
from liga_bot.models.enums import Division
from liga_bot.repositories.team_repo import TeamRepository


@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provee una sesión limpia truncando teams antes y después de cada prueba."""
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE ticket_notices, matches, teams CASCADE;"))
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(text("TRUNCATE TABLE ticket_notices, matches, teams CASCADE;"))
        await session.commit()


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


# ---------------------------------------------------------------------------
# 1. Argument Parser Tests
# ---------------------------------------------------------------------------


def test_cli_parser_help(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "liga-cli" in captured.out
    assert "seed-teams" in captured.out


def test_cli_parser_seed_teams_defaults():
    parser = build_parser()
    args = parser.parse_args(["seed-teams"])
    assert args.subcommand == "seed-teams"
    assert args.file_path is None
    assert args.division is None
    assert args.clear is False


def test_cli_parser_seed_teams_custom_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "seed-teams",
            "-f",
            "teams.json",
            "-d",
            "PREMIER",
            "--clear",
        ]
    )
    assert args.subcommand == "seed-teams"
    assert args.file_path == "teams.json"
    assert args.division == "PREMIER"
    assert args.clear is True


def test_cli_parser_seed_teams_json_csv_flags():
    parser = build_parser()
    args_json = parser.parse_args(["seed-teams", "--json-file", "teams.json"])
    assert args_json.json_file == "teams.json"

    args_csv = parser.parse_args(["seed-teams", "--csv-file", "teams.csv"])
    assert args_csv.csv_file == "teams.csv"


# ---------------------------------------------------------------------------
# 2. File Loading Tests (JSON & CSV)
# ---------------------------------------------------------------------------


def test_load_teams_from_json(tmp_path):
    json_file = tmp_path / "teams.json"
    data = [
        {
            "name": "Team SoloMid",
            "tag": "tsm",
            "division": "PREMIER",
            "discord_role_id": 900000000000000001,
        },
        {
            "name": "Cloud9",
            "tag": "c9",
            "division": "ASCEND",
            "discord_role_id": 900000000000000002,
        },
    ]
    json_file.write_text(json.dumps(data), encoding="utf-8")

    teams = load_teams_from_file(json_file)
    assert len(teams) == 2
    assert teams[0]["name"] == "Team SoloMid"
    assert teams[0]["tag"] == "TSM"
    assert teams[0]["division"] == Division.PREMIER
    assert teams[0]["discord_role_id"] == 900000000000000001
    assert teams[1]["tag"] == "C9"
    assert teams[1]["division"] == Division.ASCEND


def test_load_teams_from_csv(tmp_path):
    csv_file = tmp_path / "teams.csv"
    csv_content = (
        "name,tag,division,discord_role_id\n"
        "Fnatic,fnc,PREMIER,900000000000000003\n"
        "G2 Esports,g2,PREMIER,900000000000000004\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    teams = load_teams_from_file(csv_file)
    assert len(teams) == 2
    assert teams[0]["name"] == "Fnatic"
    assert teams[0]["tag"] == "FNC"
    assert teams[1]["name"] == "G2 Esports"


def test_load_teams_file_not_found(tmp_path):
    missing_file = tmp_path / "non_existent.json"
    with pytest.raises(FileNotFoundError):
        load_teams_from_file(missing_file)


def test_load_teams_invalid_extension(tmp_path):
    txt_file = tmp_path / "teams.txt"
    txt_file.write_text("dummy", encoding="utf-8")
    with pytest.raises(ValueError, match="Formato no soportado"):
        load_teams_from_file(txt_file)


def test_load_teams_invalid_division(tmp_path):
    json_file = tmp_path / "invalid.json"
    json_file.write_text(
        json.dumps([{"name": "Team", "tag": "T", "division": "IRON", "discord_role_id": 123}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="División 'IRON' inválida"):
        load_teams_from_file(json_file)


# ---------------------------------------------------------------------------
# 3. Seed Teams Function & Idempotency Tests (PGlite in-memory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_teams_canonical_default(session_factory, db_session: AsyncSession):
    repo = TeamRepository(db_session)
    assert await repo.count() == 0

    stats = await seed_teams(session_factory, CANONICAL_DEFAULT_TEAMS)

    assert stats["created"] == len(CANONICAL_DEFAULT_TEAMS)
    assert stats["updated"] == 0
    assert stats["skipped"] == 0
    assert stats["total"] == len(CANONICAL_DEFAULT_TEAMS)

    # Comprobar en base de datos
    teams_in_db = await repo.list_all()
    assert len(teams_in_db) == len(CANONICAL_DEFAULT_TEAMS)

    # Verificar que existen tanto Premier como Ascend
    premier_teams = await repo.list_by_division(Division.PREMIER)
    ascend_teams = await repo.list_by_division(Division.ASCEND)
    assert len(premier_teams) > 0
    assert len(ascend_teams) > 0
    assert len(premier_teams) + len(ascend_teams) == len(CANONICAL_DEFAULT_TEAMS)


@pytest.mark.asyncio
async def test_seed_teams_idempotency(session_factory, db_session: AsyncSession):
    # Primer pase
    stats1 = await seed_teams(session_factory, CANONICAL_DEFAULT_TEAMS)
    assert stats1["created"] == len(CANONICAL_DEFAULT_TEAMS)

    # Segundo pase idéntico
    stats2 = await seed_teams(session_factory, CANONICAL_DEFAULT_TEAMS)
    assert stats2["created"] == 0
    assert stats2["updated"] == 0
    assert stats2["skipped"] == len(CANONICAL_DEFAULT_TEAMS)

    repo = TeamRepository(db_session)
    assert await repo.count() == len(CANONICAL_DEFAULT_TEAMS)


@pytest.mark.asyncio
async def test_seed_teams_division_filter(session_factory, db_session: AsyncSession):
    stats = await seed_teams(
        session_factory,
        CANONICAL_DEFAULT_TEAMS,
        division_filter=Division.PREMIER,
    )
    repo = TeamRepository(db_session)
    assert await repo.count() == stats["created"]

    premier_teams = await repo.list_by_division(Division.PREMIER)
    ascend_teams = await repo.list_by_division(Division.ASCEND)
    assert len(premier_teams) == stats["created"]
    assert len(ascend_teams) == 0


@pytest.mark.asyncio
async def test_seed_teams_clear_existing(session_factory, db_session: AsyncSession):
    # Crear un equipo inicial
    repo = TeamRepository(db_session)
    await repo.create(
        name="Equipo Antiguo",
        tag="OLD",
        slug="equipo-antiguo",
        division=Division.PREMIER,
        discord_role_id=999999999999999999,
    )
    assert await repo.count() == 1

    # Sembrar con flag clear_existing=True
    stats = await seed_teams(
        session_factory,
        CANONICAL_DEFAULT_TEAMS[:2],
        clear_existing=True,
    )
    assert stats["created"] == 2
    assert await repo.count() == 2

    # El equipo antiguo debe haber sido eliminado
    old_team = await repo.get_by_name("Equipo Antiguo")
    assert old_team is None


@pytest.mark.asyncio
async def test_run_seed_command_success(migrated_db: AsyncEngine, db_session: AsyncSession):
    exit_code = await run_seed_command(
        file_path=None,
        division=None,
        clear=False,
        engine=migrated_db,
    )
    assert exit_code == 0

    repo = TeamRepository(db_session)
    assert await repo.count() == len(CANONICAL_DEFAULT_TEAMS)


@pytest.mark.asyncio
async def test_run_seed_command_with_custom_files(
    migrated_db: AsyncEngine, db_session: AsyncSession, tmp_path
):
    json_file = tmp_path / "custom.json"
    json_file.write_text(
        json.dumps(
            [
                {
                    "name": "Custom Team",
                    "tag": "CST",
                    "division": "PREMIER",
                    "discord_role_id": 888888888888888888,
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = await run_seed_command(
        json_file=str(json_file),
        clear=True,
        engine=migrated_db,
    )
    assert exit_code == 0

    repo = TeamRepository(db_session)
    assert await repo.count() == 1
    team = await repo.get_by_name("Custom Team")
    assert team is not None
    assert team.tag == "CST"


def test_main_cli_entrypoint():
    # Simula invocación con --help
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("pglite:///:memory:", ":memory:"),
        ("pglite:///./.data/dev_db", "./.data/dev_db"),
        ("pglite:///tmp/test_db", "/tmp/test_db"),
        ("pglite:////absolute/path/db", "/absolute/path/db"),
        ("pglite://./my_folder", "./my_folder"),
        ("pglite:data_dir", "data_dir"),
        ("postgresql+asyncpg://postgres:pass@localhost:5432/liga_bot", None),
    ],
)
def test_parse_pglite_path(url: str, expected: str | None):
    from liga_bot.database import _parse_pglite_path

    assert _parse_pglite_path(url) == expected


@pytest.mark.asyncio
async def test_run_seed_command_auto_creates_tables_on_unmigrated_engine():
    from py_pglite.sqlalchemy.manager_async import SQLAlchemyAsyncPGliteManager

    manager = SQLAlchemyAsyncPGliteManager(config=None)
    manager.start()
    await manager.wait_for_ready()
    fresh_engine = manager.get_engine()

    try:
        # Intencionalmente no ejecutamos migraciones de alembic aquí;
        # run_seed_command debe llamar a Base.metadata.create_all y tener éxito
        exit_code = await run_seed_command(engine=fresh_engine)
        assert exit_code == 0

        # Verificar que la tabla teams existe y contiene los equipos
        session_factory = async_sessionmaker(bind=fresh_engine, expire_on_commit=False)
        async with session_factory() as session:
            repo = TeamRepository(session)
            count = await repo.count()
            assert count == len(CANONICAL_DEFAULT_TEAMS)
    finally:
        await manager.stop()
