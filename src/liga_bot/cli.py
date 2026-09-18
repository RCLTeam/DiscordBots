"""
Módulo de interfaz de línea de comandos (CLI) administrativa para LigaBot.

Proporciona comandos de consola para administración del sistema, incluyendo:
- 'seed-teams': precarga e inserción idempotente de equipos participantes
  (tanto desde lista canónica predefinida como desde archivos JSON/CSV).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, TypedDict

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from liga_bot.config import Settings, get_settings
from liga_bot.database import (
    close_engine,
    get_engine,
    get_session_factory,
    transactional_session,
)
from liga_bot.models.enums import Division
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.utils.formatting import normalize_slug, normalize_tag

logger = logging.getLogger("liga_bot.cli")


class TeamSeedData(TypedDict):
    name: str
    tag: str
    division: Division
    discord_role_id: int


CANONICAL_DEFAULT_TEAMS: Final[tuple[TeamSeedData, ...]] = (
    # División PREMIER
    {
        "name": "Planar Shock Pingus",
        "tag": "PSP",
        "division": Division.PREMIER,
        "discord_role_id": 1547729760384319501,
    },
    {
        "name": "Fnix Esports",
        "tag": "FNX",
        "division": Division.PREMIER,
        "discord_role_id": 1547729760384319502,
    },
    {
        "name": "Lobos",
        "tag": "LOB",
        "division": Division.PREMIER,
        "discord_role_id": 1547729760384319503,
    },
    {
        "name": "Cuervos",
        "tag": "CRV",
        "division": Division.PREMIER,
        "discord_role_id": 1547729760384319504,
    },
    # División ASCEND
    {
        "name": "Dragones",
        "tag": "DRG",
        "division": Division.ASCEND,
        "discord_role_id": 1548795784655405001,
    },
    {
        "name": "Fenix Ascend",
        "tag": "FXA",
        "division": Division.ASCEND,
        "discord_role_id": 1548795784655405002,
    },
    {
        "name": "Kraken Esports",
        "tag": "KRK",
        "division": Division.ASCEND,
        "discord_role_id": 1548795784655405003,
    },
    {
        "name": "Viper Gaming",
        "tag": "VIP",
        "division": Division.ASCEND,
        "discord_role_id": 1548795784655405004,
    },
)


def load_teams_from_file(file_path: Path) -> list[TeamSeedData]:
    """Carga y valida registros de equipos desde un archivo JSON o CSV."""
    if not file_path.exists():
        raise FileNotFoundError(f"No se encontró el archivo de datos: {file_path}")

    suffix = file_path.suffix.lower()
    teams: list[TeamSeedData] = []

    if suffix == ".json":
        with file_path.open("r", encoding="utf-8") as f:
            raw_data = json.load(f)
            if not isinstance(raw_data, list):
                raise ValueError("El archivo JSON debe contener una lista de objetos de equipos.")
            for i, item in enumerate(raw_data, start=1):
                teams.append(_parse_team_dict(item, source=f"Elemento {i}"))
    elif suffix == ".csv":
        with file_path.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=2):
                teams.append(_parse_team_dict(row, source=f"Fila {i}"))
    else:
        raise ValueError(f"Formato no soportado '{suffix}'. Debe ser .json o .csv")

    return teams


def _parse_team_dict(data: dict[str, Any], source: str) -> TeamSeedData:
    """Valida y convierte un diccionario arbitrario a TeamSeedData."""
    try:
        name = str(data["name"]).strip()
        tag = normalize_tag(str(data["tag"]))
        div_str = str(data["division"]).strip().upper()
        role_id = int(data["discord_role_id"])
    except KeyError as err:
        raise ValueError(f"{source}: Falta la propiedad requerida '{err.args[0]}'.") from err
    except (ValueError, TypeError) as err:
        raise ValueError(f"{source}: discord_role_id debe ser un entero válido.") from err

    if not name:
        raise ValueError(f"{source}: El nombre del equipo no puede estar vacío.")

    try:
        division = Division[div_str]
    except KeyError:
        allowed = list(Division.__members__.keys())
        raise ValueError(f"{source}: División '{div_str}' inválida. Opciones: {allowed}") from None

    return {
        "name": name,
        "tag": tag,
        "division": division,
        "discord_role_id": role_id,
    }


class SeedStats(TypedDict):
    created: int
    updated: int
    skipped: int
    total: int


async def seed_teams(
    session_factory: async_sessionmaker,
    teams_data: Sequence[TeamSeedData],
    division_filter: Division | None = None,
    clear_existing: bool = False,
) -> SeedStats:
    """
    Inserta o actualiza equipos en la base de datos de manera idempotente usando TeamRepository.
    """
    stats: SeedStats = {"created": 0, "updated": 0, "skipped": 0, "total": 0}

    filtered_teams = [
        t for t in teams_data if division_filter is None or t["division"] == division_filter
    ]

    async with transactional_session(session_factory) as session:
        repo = TeamRepository(session)

        if clear_existing:
            all_existing = await repo.list_all()
            for existing in all_existing:
                await repo.delete(existing)
            logger.info("Se eliminaron %d equipos existentes (flag --clear)", len(all_existing))

        for team_info in filtered_teams:
            stats["total"] += 1
            name = team_info["name"]
            tag = normalize_tag(team_info["tag"])
            slug = normalize_slug(name)
            division = team_info["division"]
            role_id = team_info["discord_role_id"]

            # Comprobación de idempotencia: buscar por role_id o por name
            existing = await repo.get_by_role_id(role_id)
            if existing is None:
                existing = await repo.get_by_name(name)

            if existing is None:
                # Creación
                await repo.create(
                    name=name,
                    tag=tag,
                    slug=slug,
                    division=division,
                    discord_role_id=role_id,
                )
                stats["created"] += 1
            else:
                # Verificar si requiere actualización
                needs_update = False
                if (
                    existing.name != name
                    or existing.tag != tag
                    or existing.division != division
                    or existing.discord_role_id != role_id
                ):
                    await repo.update(
                        existing,
                        name=name,
                        tag=tag,
                        slug=slug,
                        division=division,
                        discord_role_id=role_id,
                    )
                    stats["updated"] += 1
                    needs_update = True

                if not needs_update:
                    stats["skipped"] += 1

    return stats


async def run_seed_command(
    file_path: str | None = None,
    json_file: str | None = None,
    csv_file: str | None = None,
    division: str | None = None,
    clear: bool = False,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
) -> int:
    """Punto de ejecución asíncrono del comando seed-teams."""
    app_settings = settings or get_settings()
    created_engine = False
    target_engine = engine

    if target_engine is None:
        target_engine = await get_engine(app_settings)
        created_engine = True

    try:
        import liga_bot.models  # noqa: F401
        from liga_bot.models.base import Base

        # Garantizar que las tablas existen antes de poblar datos
        # (especialmente en bases de datos en memoria o recién creadas)
        async with target_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = get_session_factory(target_engine)

        selected_file = file_path or json_file or csv_file
        if selected_file:
            p = Path(selected_file)
            teams_data = load_teams_from_file(p)
            print(f"Cargados {len(teams_data)} equipos desde '{selected_file}'.")
        else:
            teams_data = list(CANONICAL_DEFAULT_TEAMS)
            print(f"Utilizando {len(teams_data)} equipos de la lista canónica predeterminada.")

        div_filter = Division[division.upper()] if division else None

        stats = await seed_teams(
            session_factory=session_factory,
            teams_data=teams_data,
            division_filter=div_filter,
            clear_existing=clear,
        )

        print(
            f"✅ Sembrado finalizado con éxito: "
            f"{stats['created']} creados, {stats['updated']} actualizados, "
            f"{stats['skipped']} sin cambios (Total procesados: {stats['total']})."
        )
        return 0
    except Exception as err:
        logger.exception("Error al sembrar equipos: %s", err)
        print(f"❌ Error al sembrar equipos: {err}", file=sys.stderr)
        return 1
    finally:
        if created_engine:
            await close_engine(target_engine)


def build_parser() -> argparse.ArgumentParser:
    """Construye el árbol de comandos CLI para liga-bot."""
    parser = argparse.ArgumentParser(
        prog="liga-bot",
        description="CLI de administración y gestión para LigaBot.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Comando a ejecutar")

    # Subcomando: seed-teams
    seed_parser = subparsers.add_parser(
        "seed-teams",
        help="Precarga e inserta equipos participantes en la base de datos de manera idempotente.",
    )
    seed_parser.add_argument(
        "-f",
        "--file",
        dest="file_path",
        type=str,
        default=None,
        help="Ruta a un archivo JSON o CSV con los datos de los equipos.",
    )
    seed_parser.add_argument(
        "--json-file",
        dest="json_file",
        type=str,
        default=None,
        help="Ruta a un archivo JSON con los datos de los equipos.",
    )
    seed_parser.add_argument(
        "--csv-file",
        dest="csv_file",
        type=str,
        default=None,
        help="Ruta a un archivo CSV con los datos de los equipos.",
    )
    seed_parser.add_argument(
        "-d",
        "--division",
        dest="division",
        choices=["PREMIER", "ASCEND", "premier", "ascend"],
        default=None,
        help="Filtra y siembra únicamente los equipos de la división especificada.",
    )
    seed_parser.add_argument(
        "--clear",
        dest="clear",
        action="store_true",
        default=False,
        help="Elimina todos los equipos existentes antes de sembrar.",
    )

    return parser


def main(args: list[str] | None = None) -> int:
    """Punto de entrada principal del CLI."""
    parser = build_parser()
    parsed_args = parser.parse_args(args)

    if parsed_args.subcommand == "seed-teams":
        return asyncio.run(
            run_seed_command(
                file_path=parsed_args.file_path,
                json_file=parsed_args.json_file,
                csv_file=parsed_args.csv_file,
                division=parsed_args.division,
                clear=parsed_args.clear,
            )
        )

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
