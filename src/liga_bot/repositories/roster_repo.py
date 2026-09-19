"""
Roster and team membership repositories for LigaBot.

Provides asynchronous data access, filtering, and mutations for:
1. TeamMembershipRepository (team_memberships)
2. RosterMovementRepository (roster_movements)
3. AuditLogRepository (audit_logs)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from liga_bot.models.enums import RosterMovementAction, RosterRole
from liga_bot.models.roster import AuditLog, RosterMovement, TeamMembership
from liga_bot.repositories.base import BaseRepository


def _clean_uuid(val: UUID | str | None) -> UUID | None:
    """Valida y convierte de forma segura un valor a UUID evitando DataError en PostgreSQL."""
    if val is None:
        return None
    if isinstance(val, UUID):
        return val
    if isinstance(val, str):
        try:
            return UUID(val.strip())
        except ValueError:
            return None
    return None


def _clean_user_id(val: str | int | None) -> str | None:
    """Normaliza un identificador de usuario de Discord eliminando espacios en blanco."""
    if val is None:
        return None
    cleaned = str(val).strip()
    return cleaned if cleaned else None


def _to_json_safe(value: Any) -> Any:
    """
    Normaliza recursivamente estructuras de datos para garantizar compatibilidad
    estricta con JSONB en PostgreSQL / PGlite.

    Reglas de conversión:
    - None, str, int, float, bool: Se conservan sin cambios.
    - UUID: Se convierte a string canónico de 36 caracteres (str(value)).
    - datetime / date: Se convierte a cadena ISO 8601 (value.isoformat()).
    - Enum: Se extrae su valor subyacente (value.value).
    - Decimal: Se convierte a float (float(value)).
    - Mapping (dict): Se genera un dict con claves str y valores normalizados recursivamente.
    - Sequence / Set (list, tuple, set): Se genera una list normalizada recursivamente.
    - Fallback: str(value).
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, Sequence)) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_to_json_safe(v) for v in value]
    return str(value)


class TeamMembershipRepository(BaseRepository[TeamMembership]):
    """Repositorio especializado en la gestión de membresías y plantillas de equipos."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, TeamMembership)

    async def get(
        self,
        team_id: UUID | str,
        discord_user_id: str | int,
        with_team: bool = False,
        with_user: bool = False,
    ) -> TeamMembership | None:
        """
        Recupera una membresía por su clave primaria compuesta (team_id, discord_user_id).
        Retorna None de forma segura ante identificadores malformados sin abortar transacciones.
        """
        clean_team_id = _clean_uuid(team_id)
        clean_user_id = _clean_user_id(discord_user_id)
        if clean_team_id is None or clean_user_id is None:
            return None

        stmt = select(TeamMembership).where(
            TeamMembership.team_id == clean_team_id,
            TeamMembership.discord_user_id == clean_user_id,
        )
        if with_team:
            stmt = stmt.options(selectinload(TeamMembership.team))
        if with_user:
            stmt = stmt.options(selectinload(TeamMembership.discord_user))

        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def list_by_user(
        self,
        discord_user_id: str | int,
        with_team: bool = False,
    ) -> Sequence[TeamMembership]:
        """
        Obtiene todas las membresías activas asociadas a un usuario en cualquier equipo.
        Ordenadas cronológicamente por fecha de registro (created_at).
        """
        clean_user_id = _clean_user_id(discord_user_id)
        if clean_user_id is None:
            return []

        stmt = select(TeamMembership).where(TeamMembership.discord_user_id == clean_user_id)
        if with_team:
            stmt = stmt.options(selectinload(TeamMembership.team))
        stmt = stmt.order_by(TeamMembership.created_at.asc())

        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_by_team(
        self,
        team_id: UUID | str,
        with_user: bool = False,
    ) -> Sequence[TeamMembership]:
        """
        Obtiene todos los integrantes y cuerpo técnico asignados a un equipo.
        Ordena primero capitanes, luego por rol y fecha de incorporación.
        """
        clean_team_id = _clean_uuid(team_id)
        if clean_team_id is None:
            return []

        stmt = select(TeamMembership).where(TeamMembership.team_id == clean_team_id)
        if with_user:
            stmt = stmt.options(selectinload(TeamMembership.discord_user))
        stmt = stmt.order_by(
            TeamMembership.is_captain.desc(),
            TeamMembership.role.asc(),
            TeamMembership.created_at.asc(),
        )

        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def get_competitive_membership(
        self,
        discord_user_id: str | int,
        with_team: bool = False,
        with_user: bool = False,
    ) -> TeamMembership | None:
        """
        Busca si el usuario ostenta actualmente un rol competitivo en cualquier equipo de la liga.

        Conforme a la regla invariante #6, un jugador solo puede ocupar una posición
        competitiva ('top', 'jungle', 'mid', 'adc', 'support', 'substitute') en como máximo
        1 equipo simultáneamente. Retorna None si no tiene rol competitivo activo.
        """
        clean_user_id = _clean_user_id(discord_user_id)
        if clean_user_id is None:
            return None

        competitive_roles = [r for r in RosterRole if r.is_competitive()]
        stmt = select(TeamMembership).where(
            TeamMembership.discord_user_id == clean_user_id,
            TeamMembership.role.in_(competitive_roles),
        )
        if with_team:
            stmt = stmt.options(selectinload(TeamMembership.team))
        if with_user:
            stmt = stmt.options(selectinload(TeamMembership.discord_user))

        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def create(  # type: ignore[override]
        self,
        team_id: UUID | str,
        discord_user_id: str | int,
        role: RosterRole | str,
        is_captain: bool = False,
    ) -> TeamMembership:
        """
        Registra y persiste una nueva membresía en la plantilla de un equipo.
        Sincroniza mediante flush y refresh sin cerrar la transacción.
        """
        clean_team_id = _clean_uuid(team_id)
        if clean_team_id is None:
            raise ValueError(f"Identificador de equipo inválido: {team_id!r}")

        clean_user_id = _clean_user_id(discord_user_id)
        if clean_user_id is None:
            raise ValueError("El discord_user_id no puede ser nulo ni vacío.")

        role_enum = role if isinstance(role, RosterRole) else RosterRole(role)

        membership = TeamMembership(
            team_id=clean_team_id,
            discord_user_id=clean_user_id,
            role=role_enum,
            is_captain=is_captain,
        )
        self._session.add(membership)
        await self._session.flush()
        await self._session.refresh(membership)
        return membership

    async def update_role(
        self,
        team_id: UUID | str,
        discord_user_id: str | int,
        new_role: RosterRole | str,
        is_captain: bool = False,
    ) -> TeamMembership:
        """
        Actualiza el rol de plantilla y estado de capitanía de un miembro existente.
        Lanza ValueError si la membresía no existe.
        """
        membership = await self.get(team_id, discord_user_id)
        if membership is None:
            raise ValueError(
                f"TeamMembership not found for team_id={team_id!r} "
                f"and discord_user_id={discord_user_id!r}."
            )

        role_enum = new_role if isinstance(new_role, RosterRole) else RosterRole(new_role)
        membership.role = role_enum
        membership.is_captain = is_captain

        await self._session.flush()
        await self._session.refresh(membership)
        return membership

    async def set_captain(
        self,
        team_id: UUID | str,
        discord_user_id: str | int,
        is_captain: bool,
    ) -> TeamMembership:
        """Actualiza exclusivamente el estado de capitanía de un miembro existente."""
        membership = await self.get(team_id, discord_user_id)
        if membership is None:
            raise ValueError(
                f"TeamMembership not found for team_id={team_id!r} "
                f"and discord_user_id={discord_user_id!r}."
            )

        membership.is_captain = is_captain
        await self._session.flush()
        await self._session.refresh(membership)
        return membership

    async def delete(  # type: ignore[override]
        self,
        team_id: UUID | str | TeamMembership,
        discord_user_id: str | int | None = None,
    ) -> bool:
        """
        Elimina una membresía por su clave compuesta o pasando la entidad directa.
        Retorna True si la entidad existía y fue eliminada, False si no existía.
        """
        if isinstance(team_id, TeamMembership):
            await self._session.delete(team_id)
            await self._session.flush()
            return True

        if discord_user_id is None:
            return False

        membership = await self.get(team_id, discord_user_id)
        if membership is None:
            return False

        await self._session.delete(membership)
        await self._session.flush()
        return True


class RosterMovementRepository(BaseRepository[RosterMovement]):
    """
    Repositorio especializado en el historial inmutable de movimientos de plantilla
    (roster_movements: altas, bajas, capitanías y cambios de posición/rol).
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RosterMovement)

    async def record_movement(
        self,
        team_id: UUID | str,
        discord_user_id: str | int,
        action: RosterMovementAction | str,
        role: RosterRole | str | None = None,
        actor_id: str | int | None = None,
        created_at: datetime | None = None,
    ) -> RosterMovement:
        """
        Registra y persiste un nuevo movimiento de plantilla.

        Parámetros:
        - team_id: Identificador UUID del equipo (UUID o string convertible).
        - discord_user_id: Discord Snowflake ID del usuario objeto del movimiento.
        - action: Acción efectuada (RosterMovementAction o su valor str).
        - role: Rol o posición asignada (RosterRole o str), o None si no aplica.
        - actor_id: Discord Snowflake ID del usuario o staff que ejecutó la acción (nullable).
        - created_at: Marca temporal opcional del movimiento (por defecto hora de servidor).

        Retorna:
        - Instancia de RosterMovement persistida con id y timestamps asignados.
        """
        clean_team_id = _clean_uuid(team_id)
        if clean_team_id is None:
            raise ValueError(f"Identificador de equipo inválido: {team_id!r}")

        clean_user_id = _clean_user_id(discord_user_id)
        if clean_user_id is None:
            raise ValueError("El discord_user_id no puede ser nulo ni vacío.")

        act: RosterMovementAction = (
            action if isinstance(action, RosterMovementAction) else RosterMovementAction(action)
        )
        r: RosterRole | None = (
            role if (role is None or isinstance(role, RosterRole)) else RosterRole(role)
        )
        act_id: str | None = _clean_user_id(actor_id)

        kwargs: dict[str, Any] = {
            "team_id": clean_team_id,
            "discord_user_id": clean_user_id,
            "action": act,
            "role": r,
            "actor_id": act_id,
        }
        if created_at is not None:
            kwargs["created_at"] = created_at

        movement = RosterMovement(**kwargs)
        self._session.add(movement)
        await self._session.flush()
        await self._session.refresh(movement)
        return movement

    async def list_by_team(
        self,
        team_id: UUID | str,
        limit: int | None = None,
    ) -> Sequence[RosterMovement]:
        """
        Recupera el historial de movimientos de un equipo, ordenado cronológicamente
        descendente (movimientos más recientes primero).

        Alineado con el índice DB: roster_movements_team_id_idx.
        Retorna lista vacía de forma segura si team_id no es un UUID válido.
        """
        clean_team_id = _clean_uuid(team_id)
        if clean_team_id is None:
            return []

        stmt = (
            select(RosterMovement)
            .where(RosterMovement.team_id == clean_team_id)
            .order_by(RosterMovement.created_at.desc())
        )
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)

        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_by_user(
        self,
        discord_user_id: str | int,
        limit: int | None = None,
    ) -> Sequence[RosterMovement]:
        """
        Recupera el historial de movimientos asociados a un usuario de Discord,
        ordenado cronológicamente descendente (más recientes primero).

        Alineado con el índice DB: roster_movements_discord_user_id_idx.
        Retorna lista vacía de forma segura si discord_user_id es nulo o vacío.
        """
        clean_user_id = _clean_user_id(discord_user_id)
        if clean_user_id is None:
            return []

        stmt = (
            select(RosterMovement)
            .where(RosterMovement.discord_user_id == clean_user_id)
            .order_by(RosterMovement.created_at.desc())
        )
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)

        result = await self._session.execute(stmt)
        return result.scalars().all()


class AuditLogRepository(BaseRepository[AuditLog]):
    """
    Repositorio especializado en el registro y consulta de eventos de auditoría
    transaccional (audit_logs) con almacenamiento JSONB estructurado.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, AuditLog)

    async def log(
        self,
        actor_discord_user_id: str | int | None,
        action: str,
        entity_type: str,
        entity_id: UUID | str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> AuditLog:
        """
        Registra una entrada de auditoría con serialización segura de campos JSONB.

        Parámetros:
        - actor_discord_user_id: Snowflake ID del actor (nullable en acciones automáticas).
        - action: Nombre descriptivo de la acción (ej. "roster.role_changed").
        - entity_type: Tipo de entidad afectada (ej. "team_membership", "team", "player").
        - entity_id: UUID de la entidad afectada (nullable si la entidad tiene clave compuesta).
        - before: Diccionario previo (normalizado recursivamente a tipos JSON seguros).
        - after: Diccionario posterior (normalizado recursivamente a tipos JSON seguros).
        - created_at: Marca temporal opcional del evento (por defecto hora de servidor).

        Retorna:
        - Instancia de AuditLog persistida.
        """
        clean_actor_id = _clean_user_id(actor_discord_user_id)
        clean_entity_id: UUID | None = None
        if entity_id is not None:
            clean_entity_id = _clean_uuid(entity_id)
            if clean_entity_id is None:
                raise ValueError(
                    f"entity_id must be a valid UUID or UUID string, got {entity_id!r}"
                )

        safe_before = _to_json_safe(before) if before is not None else None
        safe_after = _to_json_safe(after) if after is not None else None

        kwargs: dict[str, Any] = {
            "actor_discord_user_id": clean_actor_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": clean_entity_id,
            "before": safe_before,
            "after": safe_after,
        }
        if created_at is not None:
            kwargs["created_at"] = created_at

        entry = AuditLog(**kwargs)
        self._session.add(entry)
        await self._session.flush()
        await self._session.refresh(entry)
        return entry

    async def list_by_entity(
        self,
        entity_type: str,
        entity_id: UUID | str,
        limit: int | None = None,
    ) -> Sequence[AuditLog]:
        """
        Recupera los registros de auditoría de una entidad específica (tipo + UUID),
        ordenados cronológicamente descendente (más recientes primero).

        Alineado con el índice DB: audit_logs_entity_idx (entity_type, entity_id).
        Retorna lista vacía si entity_id no es un UUID válido.
        """
        clean_entity_id = _clean_uuid(entity_id)
        if clean_entity_id is None:
            return []

        stmt = (
            select(AuditLog)
            .where(
                AuditLog.entity_type == entity_type,
                AuditLog.entity_id == clean_entity_id,
            )
            .order_by(AuditLog.created_at.desc())
        )
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)

        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_by_actor(
        self,
        actor_discord_user_id: str | int,
        limit: int | None = None,
    ) -> Sequence[AuditLog]:
        """
        Recupera las acciones de auditoría ejecutadas por un actor de Discord,
        ordenadas cronológicamente descendente (más recientes primero).

        Alineado con el índice DB: audit_logs_actor_discord_user_id_idx.
        Retorna lista vacía de forma segura si actor_discord_user_id es nulo o vacío.
        """
        clean_actor_id = _clean_user_id(actor_discord_user_id)
        if clean_actor_id is None:
            return []

        stmt = (
            select(AuditLog)
            .where(AuditLog.actor_discord_user_id == clean_actor_id)
            .order_by(AuditLog.created_at.desc())
        )
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)

        result = await self._session.execute(stmt)
        return result.scalars().all()


__all__ = [
    "AuditLogRepository",
    "RosterMovementRepository",
    "TeamMembershipRepository",
]
