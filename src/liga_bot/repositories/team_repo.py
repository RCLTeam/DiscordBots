"""Team repository for LigaBot."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division
from liga_bot.models.roster import SeasonDivision
from liga_bot.models.team import Team
from liga_bot.repositories.base import BaseRepository
from liga_bot.repositories.season_repo import current_season_name_select


class TeamRepository(BaseRepository[Team]):
    """Repositorio especializado en la entidad Team."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Team)

    async def create(self, entity: Team | None = None, **kwargs: Any) -> Team:
        """Persiste un nuevo equipo en la base de datos.

        Permite invocar con o sin slug para compatibilidad.
        """
        slug = kwargs.pop("slug", None)
        if entity is None:
            await self._resolve_season_name(kwargs)
        else:
            # BaseRepository.create ignora kwargs cuando recibe una entidad, así que
            # hay que trasladarlos a ella o season_name y su división acabarían
            # descuadradas y la clave foránea compuesta rechazaría el INSERT.
            await self._apply_season_division(entity, kwargs)
        team = await super().create(entity=entity, **kwargs)
        await self._sync_season_name(team)
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
        # La temporada se resuelve antes del flush de super().update: la clave foránea
        # compuesta rechaza un season_name que no sea el de su seasons_divisions.
        await self._apply_season_division(entity, kwargs)
        team = await super().update(entity, **kwargs)
        await self._sync_season_name(team)
        if slug is not None and hasattr(team, "slug"):
            team.slug = slug
        return team

    async def _apply_season_division(self, entity: Team, kwargs: dict[str, Any]) -> None:
        """Deja coherentes season_division, season_division_id y season_name.

        Lo que llega en ``kwargs`` manda sobre lo que el equipo tenga cargado: con
        ``lazy="joined"`` la relación conserva la división anterior aunque se esté
        cambiando el ``season_division_id``.
        """
        season_division = kwargs.pop("season_division", None)
        season_division_id = kwargs.get("season_division_id")
        division = kwargs.pop("division", None)

        if season_division is None and season_division_id is not None:
            season_division = await self._session.get(SeasonDivision, season_division_id)
            if season_division is None:
                raise ValueError(
                    f"No existe la división de temporada {season_division_id!r} "
                    "en seasons_divisions."
                )

        if season_division is not None:
            entity.season_division = season_division
            entity.season_division_id = season_division.id
            entity.season_name = season_division.season_name
            kwargs.pop("season_division_id", None)
            kwargs.pop("season_name", None)
            return

        if division is not None:
            await self._assign_division(entity, _require_division(division))
            return

        # Sin cambio de división: basta con que el equipo mutado a mano quede coherente.
        await self._align_season_name(entity)

    async def _align_season_name(self, entity: Team) -> None:
        """Alinea ``season_name`` con la división que el equipo tiene asignada."""
        season_division = entity.season_division
        state = inspect(entity)
        sd_changed_only = (
            state.attrs.season_division.history.has_changes()
            and not state.attrs.season_division_id.history.has_changes()
        )
        if season_division is None or (
            entity.season_division_id is not None
            and season_division.id != entity.season_division_id
            and not sd_changed_only
        ):
            season_division_id = entity.season_division_id
            if season_division_id is not None:
                season_division = await self._session.get(SeasonDivision, season_division_id)
            if season_division is None:
                season_division = await self._find_current_season_division(entity.division)
            if season_division is None:
                return
            entity.season_division = season_division
            entity.season_division_id = season_division.id
        elif sd_changed_only and season_division is not None and season_division.id is not None:
            entity.season_division_id = season_division.id
        if entity.season_name != season_division.season_name:
            entity.season_name = season_division.season_name

    async def _resolve_season_name(self, kwargs: dict[str, Any]) -> None:
        """Rellena ``kwargs["season_name"]`` desde la división indicada en ``kwargs``.

        Solo para la construcción desde argumentos sueltos: la tabla compartida ata
        ambas columnas con una clave foránea compuesta, así que el valor por omisión
        del modelo no sirve para divisiones de otra temporada.
        """
        season_division = kwargs.get("season_division")
        if season_division is not None:
            kwargs["season_name"] = season_division.season_name
            return

        season_division_id = kwargs.get("season_division_id")
        if season_division_id is not None:
            result = await self._session.execute(
                select(SeasonDivision.season_name).where(SeasonDivision.id == season_division_id)
            )
            resolved = result.scalars().first()
            if resolved is not None:
                kwargs["season_name"] = resolved
            return

        division_arg = kwargs.get("division")
        division_enum = (
            _require_division(division_arg) if division_arg is not None else Division.PREMIER
        )
        target = await self._find_current_season_division(division_enum)
        if target is not None:
            kwargs["season_division"] = target
            kwargs["season_division_id"] = target.id
            kwargs["season_name"] = target.season_name

    async def _find_current_season_division(self, division: Division) -> SeasonDivision | None:
        """Busca la fila de ``seasons_divisions`` de la división en la temporada vigente."""
        stmt = (
            select(SeasonDivision)
            .where(func.upper(func.trim(SeasonDivision.division_name)) == division.value)
            .order_by(
                (SeasonDivision.season_name == current_season_name_select().scalar_subquery())
                .desc()
                .nullslast(),
                SeasonDivision.created_at.desc(),
            )
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def _sync_season_name(self, team: Team) -> None:
        """Mantiene teams.season_name igual que la de su fila de seasons_divisions.

        La tabla compartida las ata con una clave foránea compuesta, así que una
        discrepancia rechaza la escritura.
        """
        season_division = team.season_division
        if season_division is None:
            return
        if team.season_name != season_division.season_name:
            team.season_name = season_division.season_name
            await self._session.flush()

    async def _assign_division(self, team: Team, division: Division) -> None:
        current = team.season_division
        if current is not None and Division.from_name(current.division_name) == division:
            return

        if current is not None:
            stmt = select(SeasonDivision).where(
                func.upper(func.trim(SeasonDivision.division_name)) == division.value,
                SeasonDivision.season_name == current.season_name,
            )
            target = (await self._session.execute(stmt)).scalars().first()
        else:
            # Sin división previa, la de la temporada vigente; el orden solo desempata.
            target = await self._find_current_season_division(division)
        if target is None:
            season = current.season_name if current is not None else "?"
            raise ValueError(
                f"No hay ninguna división {division.value} en la temporada {season!r} "
                "de seasons_divisions."
            )
        team.season_division = target
        team.season_division_id = target.id
        team.season_name = target.season_name

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
