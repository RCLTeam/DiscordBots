"""Team repository for LigaBot."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division
from liga_bot.models.roster import SeasonDivision
from liga_bot.models.team import Team
from liga_bot.repositories.base import BaseRepository


class TeamRepository(BaseRepository[Team]):
    """Repositorio especializado en la entidad Team."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Team)

    async def create(self, entity: Team | None = None, **kwargs: Any) -> Team:
        """Persiste un nuevo equipo en la base de datos.

        Permite invocar con o sin slug para compatibilidad.
        """
        slug = kwargs.pop("slug", None)
        team = await super().create(entity=entity, **kwargs)
        if slug is not None and hasattr(team, "slug"):
            team.slug = slug
        return team

    async def update(self, entity: Team, **kwargs: Any) -> Team:
        """Actualiza un equipo; ``division`` lo mueve a otra fila de ``seasons_divisions``.

        El bot no modifica ``seasons_divisions`` (la gestiona la web): para cambiar la
        división se reasigna el equipo a la fila de la misma temporada cuyo nombre
        corresponde a la división pedida. Acepta ``slug`` por compatibilidad.
        """
        slug = kwargs.pop("slug", None)
        division = kwargs.pop("division", None)
        if division is not None:
            await self._assign_division(entity, _require_division(division))
        team = await super().update(entity, **kwargs)
        if slug is not None and hasattr(team, "slug"):
            team.slug = slug
        return team

    async def _assign_division(self, team: Team, division: Division) -> None:
        current = team.season_division
        if current is not None and Division.from_name(current.division_name) == division:
            return

        stmt = select(SeasonDivision).where(
            func.upper(func.trim(SeasonDivision.division_name)) == division.value
        )
        if current is not None:
            stmt = stmt.where(SeasonDivision.season_name == current.season_name)
        else:
            stmt = stmt.order_by(SeasonDivision.created_at.desc())
        target = (await self._session.execute(stmt)).scalars().first()
        if target is None:
            season = current.season_name if current is not None else "?"
            raise ValueError(
                f"No hay ninguna división {division.value} en la temporada {season!r} "
                "de seasons_divisions."
            )
        team.season_division = target

    async def get_by_name(self, name: str, case_sensitive: bool = False) -> Team | None:
        """
        Recupera un equipo por su nombre.
        Por defecto realiza búsqueda insensible a mayúsculas/minúsculas.
        """
        cleaned_name = name.strip()
        if case_sensitive:
            stmt = select(Team).where(Team.name == cleaned_name)
        else:
            stmt = select(Team).where(func.lower(Team.name) == cleaned_name.lower())
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_by_tag(self, tag: str) -> Team | None:
        """Recupera un equipo por su tag de hasta 4 caracteres (insensible a mayúsculas)."""
        cleaned_tag = tag.strip().upper()
        stmt = select(Team).where(func.upper(Team.tag) == cleaned_tag)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_by_role_id(self, role_id: int) -> Team | None:
        """Recupera un equipo por el ID del rol de Discord asignado."""
        stmt = select(Team).where(Team.discord_role_id == role_id)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def list_by_division(self, division: Division | str) -> Sequence[Team]:
        """Lista los equipos de una división ordenados alfabéticamente por nombre.

        Compara el nombre de ``seasons_divisions`` sin distinguir mayúsculas ni espacios,
        porque la web lo guarda con su propio formato (por ejemplo ``Ascend``).
        """
        div = _require_division(division)
        stmt = (
            select(Team)
            .join(Team.season_division)
            .where(func.upper(func.trim(SeasonDivision.division_name)) == div.value)
            .order_by(Team.name.asc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_all(self) -> Sequence[Team]:
        """Lista todos los equipos ordenados alfabéticamente por nombre."""
        stmt = select(Team).order_by(Team.name.asc())
        result = await self._session.execute(stmt)
        return result.scalars().all()


def _require_division(division: Division | str) -> Division:
    """Convierte el filtro en ``Division`` o lanza ``ValueError`` si no es válido."""
    parsed = Division.from_name(division)
    if parsed is None:
        raise ValueError(f"{division!r} no es una división válida")
    return parsed
