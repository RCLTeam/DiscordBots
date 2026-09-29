"""Repository for caster assignments and published match cards."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from liga_bot.models.caster import CasterRole, MatchCaster, MatchCasterCard
from liga_bot.models.match import Match
from liga_bot.repositories.base import BaseRepository


def _clean_uuid(val: UUID | str | None) -> UUID | None:
    """Parsea defensivamente un identificador a UUID o retorna None si es inválido."""
    if val is None:
        return None
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError):
        return None


def _clean_user_id(val: Any) -> int | None:
    """Parsea defensivamente un ID de usuario Discord a entero positivo o retorna None."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned:
            return None
        try:
            val_int = int(cleaned)
            return val_int if val_int > 0 else None
        except ValueError:
            return None
    try:
        val_int = int(val)
        return val_int if val_int > 0 else None
    except (ValueError, TypeError):
        return None


class CasterRepository(BaseRepository[MatchCaster]):
    """Repositorio asíncrono para la gestión de asignaciones de casters y tarjetas de partidos."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, MatchCaster)

    async def get_match_assignments(self, match_id: UUID | str) -> Sequence[MatchCaster]:
        """Recupera todas las asignaciones de un partido ordenadas cronológicamente."""
        clean_id = _clean_uuid(match_id)
        if clean_id is None:
            return []
        stmt = (
            select(MatchCaster)
            .where(MatchCaster.match_id == clean_id)
            .order_by(MatchCaster.created_at.asc())
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def get_user_assignment(self, match_id: UUID | str, user_id: Any) -> MatchCaster | None:
        """Recupera la asignación específica de un usuario en un partido."""
        clean_id = _clean_uuid(match_id)
        clean_uid = _clean_user_id(user_id)
        if clean_id is None or clean_uid is None:
            return None
        stmt = (
            select(MatchCaster)
            .where(
                MatchCaster.match_id == clean_id,
                MatchCaster.discord_user_id == clean_uid,
            )
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_streamer_assignment(self, match_id: UUID | str) -> MatchCaster | None:
        """Recupera la asignación activa que retransmite el partido (rol STREAMER o BOTH)."""
        clean_id = _clean_uuid(match_id)
        if clean_id is None:
            return None
        stmt = (
            select(MatchCaster)
            .where(
                MatchCaster.match_id == clean_id,
                MatchCaster.caster_role.in_([CasterRole.STREAMER, CasterRole.BOTH]),
            )
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def assign(
        self, match_id: UUID | str, user_id: Any, role: CasterRole | str
    ) -> MatchCaster | None:
        """Inserta o actualiza la asignación de rol de un usuario en un partido (upsert)."""
        clean_id = _clean_uuid(match_id)
        if clean_id is None:
            raise ValueError(f"Identificador de partido inválido: {match_id!r}")
        clean_uid = _clean_user_id(user_id)
        if clean_uid is None:
            return None

        role_enum = role if isinstance(role, CasterRole) else CasterRole(role)

        existing = await self.get_user_assignment(clean_id, clean_uid)
        if existing is not None:
            existing.caster_role = role_enum
            await self._session.flush()
            await self._session.refresh(existing)
            return existing

        stmt = (
            pg_insert(MatchCaster)
            .values(
                id=uuid.uuid4(),
                match_id=clean_id,
                discord_user_id=clean_uid,
                caster_role=role_enum,
            )
            .on_conflict_do_update(
                index_elements=["match_id", "discord_user_id"],
                set_={
                    "caster_role": role_enum,
                    "updated_at": func.now(),
                },
            )
            .returning(MatchCaster)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        entity = result.scalars().one()
        await self._session.refresh(entity)
        return entity

    async def remove(self, match_id: UUID | str, user_id: Any) -> bool:
        """Elimina la asignación de un usuario en un partido. Retorna True si existía."""
        clean_id = _clean_uuid(match_id)
        if clean_id is None:
            return False
        clean_uid = _clean_user_id(user_id)
        if clean_uid is None:
            return False
        existing = await self.get_user_assignment(clean_id, clean_uid)
        if existing is None:
            return False
        await self._session.delete(existing)
        await self._session.flush()
        return True

    # --- Métodos para MatchCasterCard (tarjetas publicadas) ---

    async def record_card(
        self, match_id: UUID | str, channel_id: int, message_id: int
    ) -> MatchCasterCard:
        """Registra o actualiza la tarjeta publicada para un partido en un canal (upsert)."""
        clean_id = _clean_uuid(match_id)
        if clean_id is None:
            raise ValueError(f"Identificador de partido inválido: {match_id!r}")

        existing = await self.get_card(clean_id, channel_id)
        if existing is not None:
            existing.message_id = message_id
            await self._session.flush()
            await self._session.refresh(existing)
            return existing

        stmt = (
            pg_insert(MatchCasterCard)
            .values(
                id=uuid.uuid4(),
                match_id=clean_id,
                channel_id=channel_id,
                message_id=message_id,
            )
            .on_conflict_do_update(
                index_elements=["match_id", "channel_id"],
                set_={
                    "message_id": message_id,
                    "updated_at": func.now(),
                },
            )
            .returning(MatchCasterCard)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        entity = result.scalars().one()
        await self._session.refresh(entity)
        return entity

    async def get_card(self, match_id: UUID | str, channel_id: int) -> MatchCasterCard | None:
        """Obtiene el registro de tarjeta publicada para un partido y canal."""
        clean_id = _clean_uuid(match_id)
        if clean_id is None:
            return None
        stmt = (
            select(MatchCasterCard)
            .where(
                MatchCasterCard.match_id == clean_id,
                MatchCasterCard.channel_id == channel_id,
            )
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def delete_card(self, match_id: UUID | str, channel_id: int) -> bool:
        """Elimina el registro de tarjeta si el mensaje fue borrado en Discord."""
        clean_id = _clean_uuid(match_id)
        if clean_id is None:
            return False
        card = await self.get_card(clean_id, channel_id)
        if card is None:
            return False
        await self._session.delete(card)
        await self._session.flush()
        return True

    async def list_cards_for_channel(self, channel_id: int) -> Sequence[MatchCasterCard]:
        """Lista todas las tarjetas publicadas en un canal específico."""
        stmt = (
            select(MatchCasterCard)
            .where(MatchCasterCard.channel_id == channel_id)
            .order_by(MatchCasterCard.created_at.asc())
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_unposted_matches(self, jornada: int, channel_id: int) -> Sequence[Match]:
        """Lista partidos de una jornada que aún no tienen tarjeta registrada en un canal."""
        subquery = select(MatchCasterCard.match_id).where(MatchCasterCard.channel_id == channel_id)
        stmt = (
            select(Match)
            .where(
                Match.jornada == jornada,
                Match.id.not_in(subquery),
            )
            .options(
                selectinload(Match.team1),
                selectinload(Match.team2),
                joinedload(Match.season_division),
                selectinload(Match.casters),
            )
            .order_by(Match.scheduled_at.asc().nulls_last(), Match.created_at.asc())
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()
