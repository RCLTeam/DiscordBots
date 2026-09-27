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
        """Actualiza los campos de un equipo existente.

        Permite invocar con o sin slug para compatibilidad.
        """
        slug = kwargs.pop("slug", None)
        team = await super().update(entity, **kwargs)
        if slug is not None and hasattr(team, "slug"):
            team.slug = slug
        return team

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
        """Lista los equipos de una división ordenados alfabéticamente por nombre."""
        div_val = division.value if hasattr(division, "value") else division
        stmt = (
            select(Team)
            .join(Team.season_division)
            .where(SeasonDivision.division_name == div_val)
            .order_by(Team.name.asc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_all(self) -> Sequence[Team]:
        """Lista todos los equipos ordenados alfabéticamente por nombre."""
        stmt = select(Team).order_by(Team.name.asc())
        result = await self._session.execute(stmt)
        return result.scalars().all()
