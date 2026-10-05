"""Domain service for caster assignments, match broadcast panels, and card idempotency."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload

from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory, transactional_session
from liga_bot.models.caster import CasterRole, MatchCaster, MatchCasterCard
from liga_bot.models.match import Match
from liga_bot.repositories.caster_repo import CasterRepository
from liga_bot.utils.ids import clean_user_id_int, clean_uuid

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MatchCastersData:
    """Estructura de datos con el estado de casteo y retransmisión de un partido."""

    has_streamer: bool
    streamer: MatchCaster | None
    casters: list[MatchCaster] = field(default_factory=list)


@dataclass(slots=True)
class CasterAssignmentResult:
    """Resultado de una operación de asignación o desasignación de casters."""

    success: bool
    action: str  # "assign" o "remove"
    data: MatchCastersData | None = None
    error: str | None = None


class CasterService:
    """Servicio de dominio para gestionar casters, retransmisiones y cartelera de partidos."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        bot: Any | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.settings = settings or get_settings()
        self.bot = bot

    async def _build_casters_data(self, repo: CasterRepository, match_id: UUID) -> MatchCastersData:
        """Construye la proyección de estado MatchCastersData a partir de las asignaciones."""
        assignments = await repo.get_match_assignments(match_id)
        streamer = next(
            (c for c in assignments if c.caster_role in (CasterRole.STREAMER, CasterRole.BOTH)),
            None,
        )
        has_streamer = streamer is not None
        casters = [c for c in assignments if c.caster_role in (CasterRole.CASTER, CasterRole.BOTH)]
        return MatchCastersData(
            has_streamer=has_streamer,
            streamer=streamer,
            casters=casters,
        )

    async def get_match_casters_data(self, match_id: UUID | str) -> MatchCastersData:
        """Obtiene el estado actual de casters y streamer para un partido."""
        clean_id = clean_uuid(match_id)
        if clean_id is None:
            return MatchCastersData(has_streamer=False, streamer=None, casters=[])

        async with transactional_session(self.session_factory) as session:
            repo = CasterRepository(session)
            return await self._build_casters_data(repo, clean_id)

    async def assign_caster(
        self, match_id: UUID | str, user_id: Any, role: CasterRole | str
    ) -> CasterAssignmentResult:
        """Asigna un usuario a un rol de casteo en un partido con verificación de exclusividad."""
        clean_id = clean_uuid(match_id)
        if clean_id is None:
            return CasterAssignmentResult(
                success=False,
                action="assign",
                data=None,
                error="Identificador de partido inválido.",
            )

        clean_uid = clean_user_id_int(user_id)
        if clean_uid is None:
            current_data = await self.get_match_casters_data(clean_id)
            return CasterAssignmentResult(
                success=False,
                action="assign",
                data=current_data,
                error="ID de usuario de Discord inválido.",
            )

        try:
            role_enum = role if isinstance(role, CasterRole) else CasterRole(role)
        except ValueError:
            return CasterAssignmentResult(
                success=False,
                action="assign",
                data=None,
                error=f"Rol de casteo inválido: {role}",
            )

        try:
            async with transactional_session(self.session_factory) as session:
                repo = CasterRepository(session)

                # Si solicita retransmitir (STREAMER o BOTH), verificar exclusividad
                if role_enum in (CasterRole.STREAMER, CasterRole.BOTH):
                    existing_streamer = await repo.get_streamer_assignment(clean_id)
                    if (
                        existing_streamer is not None
                        and existing_streamer.discord_user_id != clean_uid
                    ):
                        current_data = await self._build_casters_data(repo, clean_id)
                        return CasterAssignmentResult(
                            success=False,
                            action="assign",
                            data=current_data,
                            error="Ya hay una persona asignada a la retransmisión de este partido.",
                        )

                await repo.assign(clean_id, clean_uid, role_enum)
                new_data = await self._build_casters_data(repo, clean_id)
                return CasterAssignmentResult(
                    success=True,
                    action="assign",
                    data=new_data,
                )
        except IntegrityError as exc:
            # Caso de condición de carrera concurrente interceptada por la restricción única de BD
            current_data = await self.get_match_casters_data(clean_id)
            orig = getattr(exc, "orig", None)
            pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
            exc_str = str(exc).lower()

            if (
                "uq_match_casters_single_streamer" in exc_str
                or "single_streamer" in exc_str
                or pgcode == "23505"
            ):
                return CasterAssignmentResult(
                    success=False,
                    action="assign",
                    data=current_data,
                    error="Ya hay una persona asignada a la retransmisión de este partido.",
                )

            if (
                "fk_match_casters_match_id" in exc_str
                or "foreign key" in exc_str
                or pgcode == "23503"
            ):
                return CasterAssignmentResult(
                    success=False,
                    action="assign",
                    data=current_data,
                    error="El partido especificado no existe.",
                )

            return CasterAssignmentResult(
                success=False,
                action="assign",
                data=current_data,
                error="Error de integridad en la asignación.",
            )

    async def remove_caster(self, match_id: UUID | str, user_id: Any) -> CasterAssignmentResult:
        """Desasigna a un usuario del partido y devuelve el nuevo estado."""
        clean_id = clean_uuid(match_id)
        if clean_id is None:
            return CasterAssignmentResult(
                success=False,
                action="remove",
                data=None,
                error="Identificador de partido inválido.",
            )

        clean_uid = clean_user_id_int(user_id)
        if clean_uid is None:
            current_data = await self.get_match_casters_data(clean_id)
            return CasterAssignmentResult(
                success=False,
                action="remove",
                data=current_data,
                error="ID de usuario de Discord inválido.",
            )

        async with transactional_session(self.session_factory) as session:
            repo = CasterRepository(session)
            await repo.remove(clean_id, clean_uid)
            new_data = await self._build_casters_data(repo, clean_id)
            return CasterAssignmentResult(
                success=True,
                action="remove",
                data=new_data,
            )

    async def get_active_jornada(self) -> int | None:
        """Obtiene la jornada máxima activa registrada en la base de datos."""
        async with transactional_session(self.session_factory) as session:
            stmt = select(func.max(Match.jornada))
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_matches_for_jornada(self, jornada: int | None = None) -> Sequence[Match]:
        """Obtiene los partidos de una jornada con equipos y casters cargados eager.

        Si jornada es None, resuelve automáticamente la última jornada activa.
        Si no hay partidos ni jornada activa, retorna una lista vacía.
        """
        target_jornada = jornada
        if target_jornada is None:
            target_jornada = await self.get_active_jornada()
            if target_jornada is None:
                return []

        async with transactional_session(self.session_factory) as session:
            stmt = (
                select(Match)
                .where(Match.jornada == target_jornada)
                .options(
                    selectinload(Match.team1),
                    selectinload(Match.team2),
                    joinedload(Match.season_division),
                    selectinload(Match.casters),
                )
                .order_by(Match.scheduled_at.asc().nulls_last(), Match.created_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_match(self, match_id: UUID | str) -> Match | None:
        """Obtiene un partido por su ID con equipos, división y casters cargados eager."""
        clean_id = clean_uuid(match_id)
        if clean_id is None:
            return None

        async with transactional_session(self.session_factory) as session:
            stmt = (
                select(Match)
                .where(Match.id == clean_id)
                .options(
                    selectinload(Match.team1),
                    selectinload(Match.team2),
                    joinedload(Match.season_division),
                    selectinload(Match.casters),
                )
            )
            result = await session.execute(stmt)
            return result.scalars().first()

    # --- Métodos de idempotencia de tarjetas de publicación ---

    async def record_card(
        self, match_id: UUID | str, channel_id: int, message_id: int
    ) -> MatchCasterCard:
        """Registra o actualiza una tarjeta publicada para un partido en un canal."""
        async with transactional_session(self.session_factory) as session:
            repo = CasterRepository(session)
            return await repo.record_card(match_id, channel_id, message_id)

    async def get_card(self, match_id: UUID | str, channel_id: int) -> MatchCasterCard | None:
        """Obtiene el registro de tarjeta publicada de un partido en un canal."""
        async with transactional_session(self.session_factory) as session:
            repo = CasterRepository(session)
            return await repo.get_card(match_id, channel_id)

    async def delete_card(self, match_id: UUID | str, channel_id: int) -> bool:
        """Elimina el registro de tarjeta publicada de un partido en un canal."""
        async with transactional_session(self.session_factory) as session:
            repo = CasterRepository(session)
            return await repo.delete_card(match_id, channel_id)

    async def list_cards_for_channel(self, channel_id: int) -> Sequence[MatchCasterCard]:
        """Lista todas las tarjetas publicadas en un canal."""
        async with transactional_session(self.session_factory) as session:
            repo = CasterRepository(session)
            return await repo.list_cards_for_channel(channel_id)

    async def get_unposted_matches(self, jornada: int, channel_id: int) -> Sequence[Match]:
        """Obtiene los partidos de la jornada que no tienen tarjeta publicada en el canal."""
        async with transactional_session(self.session_factory) as session:
            repo = CasterRepository(session)
            return await repo.list_unposted_matches(jornada, channel_id)
