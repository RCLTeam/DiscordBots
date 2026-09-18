"""Match repository for LigaBot."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from liga_bot.models.enums import Division, MatchStatus
from liga_bot.models.match import Match
from liga_bot.repositories.base import BaseRepository


class MatchRepository(BaseRepository[Match]):
    """Repositorio especializado en la entidad Match."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Match)

    def _apply_eager_teams(self, stmt: Any, with_teams: bool) -> Any:
        if with_teams:
            return stmt.options(selectinload(Match.team1), selectinload(Match.team2))
        return stmt

    async def get_by_id(self, match_id: UUID | str, with_teams: bool = False) -> Match | None:
        """Obtiene un partido por ID con carga eager opcional de equipos."""
        u_id: UUID
        if isinstance(match_id, UUID):
            u_id = match_id
        elif isinstance(match_id, str):
            try:
                u_id = UUID(match_id)
            except ValueError:
                return None
        else:
            return None

        if not with_teams:
            return await super().get_by_id(u_id)
        stmt = select(Match).where(Match.id == u_id)
        stmt = self._apply_eager_teams(stmt, with_teams=True)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_by_jornada_and_teams(
        self,
        jornada: int,
        team1_id: UUID | str,
        team2_id: UUID | str,
        exact_order: bool = False,
        with_teams: bool = False,
    ) -> Match | None:
        """
        Recupera el partido de una jornada entre dos equipos.
        Si exact_order=False (por defecto), comprueba en ambas direcciones (t1 vs t2 o t2 vs t1).
        """
        try:
            u1 = team1_id if isinstance(team1_id, UUID) else UUID(str(team1_id))
            u2 = team2_id if isinstance(team2_id, UUID) else UUID(str(team2_id))
        except ValueError:
            return None

        if exact_order:
            condition = (Match.team1_id == u1) & (Match.team2_id == u2)
        else:
            condition = ((Match.team1_id == u1) & (Match.team2_id == u2)) | (
                (Match.team1_id == u2) & (Match.team2_id == u1)
            )

        stmt = select(Match).where(Match.jornada == jornada, condition)
        stmt = self._apply_eager_teams(stmt, with_teams)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def list_by_jornada(
        self,
        jornada: int,
        division: Division | str | None = None,
        with_teams: bool = False,
    ) -> Sequence[Match]:
        """Lista partidos de una jornada con filtrado opcional por división."""
        stmt = select(Match).where(Match.jornada == jornada)
        if division is not None:
            div_val = division.value if hasattr(division, "value") else division
            stmt = stmt.where(Match.division == div_val)
        stmt = self._apply_eager_teams(stmt, with_teams)
        stmt = stmt.order_by(Match.scheduled_at.asc().nulls_last(), Match.created_at.asc())
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_by_status(
        self, status: MatchStatus | str, with_teams: bool = False
    ) -> Sequence[Match]:
        """Lista partidos filtrados por su estado operativo."""
        stat_val = status.value if hasattr(status, "value") else status
        stmt = select(Match).where(Match.status == stat_val)
        stmt = self._apply_eager_teams(stmt, with_teams)
        stmt = stmt.order_by(Match.jornada.asc(), Match.created_at.asc())
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def get_by_channel_id(self, channel_id: int, with_teams: bool = False) -> Match | None:
        """Recupera un partido a partir del snowflake del canal de Discord."""
        stmt = select(Match).where(Match.discord_channel_id == channel_id)
        stmt = self._apply_eager_teams(stmt, with_teams)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def update_status(
        self, match_or_id: Match | UUID | str, status: MatchStatus | str
    ) -> Match | None:
        """Actualiza el estado de un partido existente."""
        match: Match | None
        if isinstance(match_or_id, (UUID, str)):
            match = await self.get_by_id(match_or_id)
            if match is None:
                return None
        else:
            match = match_or_id

        stat_val = status if isinstance(status, MatchStatus) else MatchStatus(status)
        match.status = stat_val
        await self._session.flush()
        await self._session.refresh(match)
        return match

    async def update_channel_id(
        self, match_or_id: Match | UUID | str, channel_id: int | None
    ) -> Match | None:
        """Asigna o actualiza el ID del canal de Discord asociado al partido."""
        match: Match | None
        if isinstance(match_or_id, (UUID, str)):
            match = await self.get_by_id(match_or_id)
            if match is None:
                return None
        else:
            match = match_or_id

        match.discord_channel_id = channel_id
        await self._session.flush()
        await self._session.refresh(match)
        return match
