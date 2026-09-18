"""TicketNotice repository for LigaBot."""

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.ticket_notice import TicketNotice
from liga_bot.repositories.base import BaseRepository


class TicketNoticeRepository(BaseRepository[TicketNotice]):
    """Repositorio especializado en la entidad TicketNotice."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, TicketNotice)

    async def get_by_channel_id(self, channel_id: int) -> TicketNotice | None:
        """Obtiene la ficha de seguimiento de un canal de ticket."""
        stmt = select(TicketNotice).where(TicketNotice.discord_channel_id == channel_id)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def record_alert(
        self,
        channel_id: int,
        alert_time: datetime | None = None,
        category_name: str | None = None,
        is_pending_staff: bool = True,
    ) -> TicketNotice:
        """
        Registra un aviso de inactividad de forma idempotente (Upsert).
        Si ya existe una entrada para el canal, actualiza last_alert_sent_at.
        Si no existe, crea una nueva entidad.
        """
        now = alert_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        notice = await self.get_by_channel_id(channel_id)
        if notice is not None:
            notice.last_alert_sent_at = now
            if category_name is not None:
                notice.category_name = category_name
            notice.is_pending_staff = is_pending_staff
        else:
            notice = TicketNotice(
                discord_channel_id=channel_id,
                category_name=category_name,
                last_alert_sent_at=now,
                is_pending_staff=is_pending_staff,
            )
            self._session.add(notice)

        await self._session.flush()
        await self._session.refresh(notice)
        return notice

    async def record_staff_response(
        self, channel_id: int, response_time: datetime | None = None
    ) -> TicketNotice | None:
        """Registra que un miembro del Staff ha interactuado en el ticket."""
        notice = await self.get_by_channel_id(channel_id)
        if notice is None:
            return None

        now = response_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        notice.last_staff_message_at = now
        notice.is_pending_staff = False
        await self._session.flush()
        await self._session.refresh(notice)
        return notice

    async def list_pending_staff(self) -> Sequence[TicketNotice]:
        """Retorna todos los tickets marcados como pendientes de intervención del staff."""
        stmt = (
            select(TicketNotice)
            .where(TicketNotice.is_pending_staff.is_(True))
            .order_by(TicketNotice.last_alert_sent_at.desc().nulls_last())
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def delete_by_channel_id(self, channel_id: int) -> bool:
        """Elimina la ficha de ticket asociada a un canal de Discord."""
        notice = await self.get_by_channel_id(channel_id)
        if notice is None:
            return False
        await self.delete(notice)
        return True


# Alias defensivo para compatibilidad con servicios y pruebas
TicketRepository = TicketNoticeRepository
