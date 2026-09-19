"""RoleRequest repository for LigaBot."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import RoleRequestStatus
from liga_bot.models.role_request import RoleRequest
from liga_bot.repositories.base import BaseRepository


class RoleRequestRepository(BaseRepository[RoleRequest]):
    """Repositorio especializado en la entidad RoleRequest."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RoleRequest)

    async def create_request(
        self,
        user_id: int,
        nombre_lol: str,
        riot_tag: str,
        equipo: str,
        canal_id: int | None = None,
    ) -> RoleRequest:
        """
        Crea y persiste una nueva solicitud de rol en estado PENDING.
        Ejecuta flush y refresh para sincronizar los identificadores y valores por defecto.
        """
        req = RoleRequest(
            user_id=user_id,
            nombre_lol=nombre_lol,
            riot_tag=riot_tag,
            equipo=equipo,
            canal_id=canal_id,
            estado=RoleRequestStatus.PENDING,
        )
        self._session.add(req)
        await self._session.flush()
        await self._session.refresh(req)
        return req

    async def get_by_channel_id(self, canal_id: int) -> RoleRequest | None:
        """
        Recupera una solicitud asociada al ID de un canal de ticket de Discord.
        Retorna None si no existe ninguna solicitud para el canal indicado.
        """
        stmt = (
            select(RoleRequest)
            .where(RoleRequest.canal_id == canal_id)
            .order_by(RoleRequest.id.desc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_active_by_user(self, user_id: int) -> RoleRequest | None:
        """
        Recupera la solicitud activa (en estado PENDING) de un usuario.
        Retorna None si el usuario no tiene ninguna solicitud en estado pendiente.
        """
        stmt = (
            select(RoleRequest)
            .where(
                RoleRequest.user_id == user_id,
                RoleRequest.estado == RoleRequestStatus.PENDING,
            )
            .order_by(RoleRequest.id.desc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def update_status(
        self,
        request_id: int,
        estado: RoleRequestStatus,
        staff_id: int | None = None,
    ) -> RoleRequest | None:
        """
        Actualiza el estado de una solicitud y opcionalmente registra el staff que resolvió.
        Ejecuta flush y refresh para sincronizar los cambios en la sesión activa.
        Retorna la entidad actualizada o None si la solicitud no fue encontrada.
        """
        req = await self.get_by_id(request_id)
        if req is None:
            return None

        if isinstance(estado, str) and not isinstance(estado, RoleRequestStatus):
            estado = RoleRequestStatus(estado)

        req.estado = estado
        if staff_id is not None:
            req.staff_id = staff_id

        await self._session.flush()
        await self._session.refresh(req)
        return req


__all__ = [
    "RoleRequestRepository",
]
