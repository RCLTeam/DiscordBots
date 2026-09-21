"""
Servicio de dominio para la sincronización de plantillas de equipos,
gestión de posiciones y preservación de invariantes deportivas con RCL-Next.

Orquesta:
- Asignación de membresía por incorporación de rol de equipo en Discord (handle_role_added).
- Baja de membresía por remoción explícita de rol de equipo en Discord (handle_role_removed).
- Modificación de posición de plantilla con validación competitiva (change_player_position).
- Consulta de membresías y equipos asociados a un usuario con eager loading (get_user_teams).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory, transactional_session
from liga_bot.models.enums import AppRole, RosterMovementAction, RosterRole
from liga_bot.models.roster import DiscordUser, Team, TeamMembership
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
    _clean_user_id,
    _clean_uuid,
)
from liga_bot.repositories.team_repo import TeamRepository

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Jerarquía de Excepciones de Dominio
# ---------------------------------------------------------------------------


class RosterSyncError(Exception):
    """Excepción base para errores de sincronización y gestión de plantillas."""


class CompetitivePositionConflictError(RosterSyncError):
    """
    Lanzada cuando se intenta asignar una posición competitiva a un jugador
    que ya ostenta un rol competitivo en otro equipo de la liga.

    Invariante: Un usuario solo puede ocupar un rol competitivo
    ('top', 'jungle', 'mid', 'adc', 'support', 'substitute') en como máximo 1 equipo.
    """

    def __init__(
        self,
        message: str | None = None,
        discord_user_id: str | None = None,
        existing_team_id: UUID | str | None = None,
        new_team_id: UUID | str | None = None,
        existing_role: RosterRole | str | None = None,
        attempted_role: RosterRole | str | None = None,
    ) -> None:
        self.discord_user_id = str(discord_user_id) if discord_user_id is not None else None
        self.existing_team_id = existing_team_id
        self.new_team_id = new_team_id
        self.existing_role = (
            existing_role.value if isinstance(existing_role, RosterRole) else existing_role
        )
        self.attempted_role = (
            attempted_role.value if isinstance(attempted_role, RosterRole) else attempted_role
        )

        if message is None:
            message = (
                f"Conflicto de posición competitiva: El usuario '{self.discord_user_id}' "
                f"ya ostenta el rol competitivo '{self.existing_role}' "
                f"en el equipo '{self.existing_team_id}'. No puede ocupar la posición "
                f"competitiva '{self.attempted_role}' en el equipo '{self.new_team_id}'."
            )
        super().__init__(message)


class PlayerNotTeamMemberError(RosterSyncError):
    """
    Lanzada cuando se intenta modificar la posición o consultar el rol de un usuario
    que no posee membresía activa en el equipo objetivo.
    """

    def __init__(
        self,
        message: str | None = None,
        discord_user_id: str | None = None,
        team_id: UUID | str | None = None,
    ) -> None:
        self.discord_user_id = str(discord_user_id) if discord_user_id is not None else None
        self.team_id = team_id
        if message is None:
            message = (
                f"El usuario '{self.discord_user_id}' no pertenece a la "
                f"plantilla del equipo '{self.team_id}'."
            )
        super().__init__(message)


class InvalidCaptainRoleError(RosterSyncError):
    """
    Lanzada cuando se intenta asignar capitanía (is_captain=True) a un miembro
    cuyo rol no corresponde a una de las 5 posiciones titulares.

    Alineada con el check constraint:
    team_memberships_captain_role_check:
    is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support').
    """

    def __init__(
        self,
        message: str | None = None,
        role: RosterRole | str | None = None,
    ) -> None:
        self.role = role.value if isinstance(role, RosterRole) else role
        if message is None:
            message = (
                f"El rol '{self.role}' no es una posición titular permitida para la capitanía. "
                "Solo las posiciones titulares ('top', 'jungle', 'mid', 'adc', 'support') "
                "pueden ser capitanes."
            )
        super().__init__(message)


# ---------------------------------------------------------------------------
# Servicio de Dominio: RosterSyncService
# ---------------------------------------------------------------------------


class RosterSyncService:
    """
    Servicio de dominio encargado de sincronizar membresías y posiciones de plantilla
    entre roles de Discord y la base de datos compartida de RCL.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        bot: commands.Bot | discord.Client | None = None,
        default_join_role: RosterRole = RosterRole.STAFF,
    ) -> None:
        self.session_factory: async_sessionmaker[AsyncSession] = (
            session_factory or get_session_factory()
        )
        self.settings: Settings = settings or get_settings()
        self.bot: commands.Bot | discord.Client | None = bot

        if default_join_role.is_competitive():
            raise ValueError(
                "default_join_role debe ser un rol no competitivo, "
                f"recibido: {default_join_role.value}"
            )
        self.default_join_role: RosterRole = default_join_role

    async def _ensure_discord_user(
        self,
        session: AsyncSession,
        discord_id: str,
        username: str | None = None,
        global_name: str | None = None,
        avatar_hash: str | None = None,
    ) -> DiscordUser:
        """Garantiza la existencia y actualización del usuario de Discord en la base de datos."""
        user = await session.get(DiscordUser, discord_id)
        if user is None:
            user = DiscordUser(
                discord_id=discord_id,
                username=username or f"user_{discord_id}",
                global_name=global_name,
                avatar_hash=avatar_hash,
                role=AppRole.VIEWER,
            )
            session.add(user)
            await session.flush()
        else:
            updated = False
            if username is not None and user.username != username:
                user.username = username
                updated = True
            if global_name is not None and user.global_name != global_name:
                user.global_name = global_name
                updated = True
            if avatar_hash is not None and user.avatar_hash != avatar_hash:
                user.avatar_hash = avatar_hash
                updated = True
            if updated:
                await session.flush()
        return user

    async def handle_role_added(
        self,
        member: discord.Member,
        role: discord.Role,
        actor_id: str | int | None = None,
        session: AsyncSession | None = None,
    ) -> TeamMembership | None:
        """
        Gestiona la incorporación de un rol de equipo a un miembro en Discord:
        - Si el rol no pertenece a ningún club en BD, retorna None sin efectos secundarios.
        - Asegura la existencia del registro DiscordUser para el miembro y el actor.
        - Si el miembro ya pertenece al equipo, retorna la membresía existente (idempotente).
        - Asigna el rol no competitivo por defecto (default_join_role) con is_captain=False.
        - Registra movimiento en roster_movements (action=JOINED) y entrada en audit_logs.
        - NUNCA retira roles de otros equipos en Discord (un usuario puede estar en varios clubes).
        """

        async def _do_added(s: AsyncSession) -> TeamMembership | None:
            team_repo = TeamRepository(s)
            team = await team_repo.get_by_role_id(role.id)
            if team is None:
                return None

            user_id_str = _clean_user_id(member.id)
            if user_id_str is None:
                return None

            clean_actor_id = _clean_user_id(actor_id)

            avatar_key: str | None = None
            if getattr(member, "avatar", None) is not None and hasattr(member.avatar, "key"):
                avatar_key = str(member.avatar.key)

            # 1. Asegurar registro del usuario
            await self._ensure_discord_user(
                session=s,
                discord_id=user_id_str,
                username=member.name,
                global_name=member.global_name,
                avatar_hash=avatar_key,
            )

            # 2. Asegurar registro del actor si se proporcionó
            if clean_actor_id is not None:
                await self._ensure_discord_user(
                    session=s,
                    discord_id=clean_actor_id,
                )

            # 3. Idempotencia: comprobar si ya es miembro
            membership_repo = TeamMembershipRepository(s)
            existing = await membership_repo.get(team.id, user_id_str)
            if existing is not None:
                return existing

            # 4. Crear membresía con rol no competitivo por defecto
            assigned_role = self.default_join_role
            membership = await membership_repo.create(
                team_id=team.id,
                discord_user_id=user_id_str,
                role=assigned_role,
                is_captain=False,
            )

            # 5. Registrar movimiento en roster_movements
            movement_repo = RosterMovementRepository(s)
            await movement_repo.record_movement(
                team_id=team.id,
                discord_user_id=user_id_str,
                action=RosterMovementAction.JOINED,
                role=assigned_role,
                actor_id=clean_actor_id,
            )

            # 6. Registrar entrada en audit_logs
            audit_repo = AuditLogRepository(s)
            after_payload = {
                "team_id": str(team.id),
                "discord_user_id": user_id_str,
                "role": assigned_role.value,
                "is_captain": False,
            }
            await audit_repo.log(
                actor_discord_user_id=clean_actor_id,
                action="roster.member_joined",
                entity_type="team_membership",
                entity_id=team.id,
                before=None,
                after=after_payload,
            )

            return membership

        if session is not None:
            return await _do_added(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_added(s)

    async def handle_role_removed(
        self,
        member: discord.Member,
        role: discord.Role,
        actor_id: str | int | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """
        Gestiona la remoción de un rol de equipo a un miembro en Discord:
        - Si el rol no pertenece a ningún club en BD, retorna False sin mutaciones.
        - Si el miembro no poseía membresía activa en dicho club, retorna False.
        - Asegura la existencia del registro DiscordUser para el actor si se proporcionó.
        - Captura snapshot previo, elimina la membresía y registra movimiento y audit_logs.
        - Retorna True si la baja fue procesada exitosamente.
        """

        async def _do_removed(s: AsyncSession) -> bool:
            team_repo = TeamRepository(s)
            team = await team_repo.get_by_role_id(role.id)
            if team is None:
                return False

            user_id_str = _clean_user_id(member.id)
            if user_id_str is None:
                return False

            clean_actor_id = _clean_user_id(actor_id)

            membership_repo = TeamMembershipRepository(s)
            membership = await membership_repo.get(team.id, user_id_str)
            if membership is None:
                return False

            # Asegurar existencia del actor para integridad referencial de auditoría
            if clean_actor_id is not None:
                await self._ensure_discord_user(
                    session=s,
                    discord_id=clean_actor_id,
                )

            before_payload = {
                "team_id": str(team.id),
                "discord_user_id": user_id_str,
                "role": (
                    membership.role.value
                    if isinstance(membership.role, RosterRole)
                    else str(membership.role)
                ),
                "is_captain": membership.is_captain,
            }
            previous_role = membership.role

            # Eliminar membresía
            await membership_repo.delete(team.id, user_id_str)

            # Registrar movimiento de salida
            movement_repo = RosterMovementRepository(s)
            await movement_repo.record_movement(
                team_id=team.id,
                discord_user_id=user_id_str,
                action=RosterMovementAction.LEFT,
                role=previous_role,
                actor_id=clean_actor_id,
            )

            # Registrar auditoría
            audit_repo = AuditLogRepository(s)
            await audit_repo.log(
                actor_discord_user_id=clean_actor_id,
                action="roster.member_left",
                entity_type="team_membership",
                entity_id=team.id,
                before=before_payload,
                after=None,
            )

            return True

        if session is not None:
            return await _do_removed(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_removed(s)

    async def change_player_position(
        self,
        discord_user_id: str | int,
        team_id: UUID | str,
        new_role: RosterRole | str,
        actor_id: str | int,
        is_captain: bool = False,
        session: AsyncSession | None = None,
    ) -> TeamMembership:
        """
        Modifica la posición de plantilla y capitanía de un miembro en un equipo:
        - Valida que discord_user_id, team_id y actor_id sean válidos.
        - Valida que si is_captain=True, el nuevo rol sea una de las 5 posiciones titulares.
        - Comprueba que el usuario sea miembro activo del equipo (PlayerNotTeamMemberError).
        - Si new_role es competitivo, valida la invariante de posición única en toda la liga
          (CompetitivePositionConflictError).
        - Actualiza la membresía, registra el movimiento correspondiente en roster_movements
          y almacena la trazabilidad en audit_logs con snapshots before/after.
        """
        clean_user_id = _clean_user_id(discord_user_id)
        if clean_user_id is None:
            raise ValueError("El discord_user_id no puede ser nulo ni vacío.")

        clean_actor_id = _clean_user_id(actor_id)
        if clean_actor_id is None:
            raise ValueError("El actor_id no puede ser nulo ni vacío.")

        clean_team_id = _clean_uuid(team_id)
        if clean_team_id is None:
            raise ValueError(f"Identificador de equipo inválido: {team_id!r}")

        role_enum = new_role if isinstance(new_role, RosterRole) else RosterRole(new_role)

        if is_captain and not role_enum.is_starter():
            raise InvalidCaptainRoleError(role=role_enum)

        async def _do_change(s: AsyncSession) -> TeamMembership:
            membership_repo = TeamMembershipRepository(s)
            movement_repo = RosterMovementRepository(s)
            audit_repo = AuditLogRepository(s)

            # Asegurar existencia del actor en discord_users para FK
            await self._ensure_discord_user(
                session=s,
                discord_id=clean_actor_id,
            )

            # 1. Comprobar membresía en el equipo
            membership = await membership_repo.get(clean_team_id, clean_user_id)
            if membership is None:
                raise PlayerNotTeamMemberError(
                    discord_user_id=clean_user_id,
                    team_id=clean_team_id,
                )

            # 2. Invariante de Posición Única: Rol competitivo único en toda la liga
            if role_enum.is_competitive():
                comp_membership = await membership_repo.get_competitive_membership(clean_user_id)
                if comp_membership is not None and comp_membership.team_id != clean_team_id:
                    raise CompetitivePositionConflictError(
                        discord_user_id=clean_user_id,
                        existing_team_id=comp_membership.team_id,
                        new_team_id=clean_team_id,
                        existing_role=comp_membership.role,
                        attempted_role=role_enum,
                    )

            # 3. Snapshot previo
            previous_role = membership.role
            was_captain = membership.is_captain
            before_payload = {
                "team_id": str(membership.team_id),
                "discord_user_id": membership.discord_user_id,
                "role": (
                    membership.role.value
                    if isinstance(membership.role, RosterRole)
                    else str(membership.role)
                ),
                "is_captain": was_captain,
            }

            # 4. Actualizar rol en repositorio
            updated = await membership_repo.update_role(
                team_id=clean_team_id,
                discord_user_id=clean_user_id,
                new_role=role_enum,
                is_captain=is_captain,
            )

            # 5. Snapshot posterior
            after_payload = {
                "team_id": str(updated.team_id),
                "discord_user_id": updated.discord_user_id,
                "role": (
                    updated.role.value
                    if isinstance(updated.role, RosterRole)
                    else str(updated.role)
                ),
                "is_captain": updated.is_captain,
            }

            # 6. Determinar acción de movimiento
            if previous_role == role_enum:
                if not was_captain and is_captain:
                    action = RosterMovementAction.PROMOTED_TO_CAPTAIN
                elif was_captain and not is_captain:
                    action = RosterMovementAction.DEMOTED_FROM_CAPTAIN
                else:
                    action = RosterMovementAction.ROLE_CHANGED
            else:
                action = RosterMovementAction.ROLE_CHANGED

            # 7. Registrar movimiento
            await movement_repo.record_movement(
                team_id=clean_team_id,
                discord_user_id=clean_user_id,
                action=action,
                role=role_enum,
                actor_id=clean_actor_id,
            )

            # 8. Registrar auditoría
            await audit_repo.log(
                actor_discord_user_id=clean_actor_id,
                action="roster.role_changed",
                entity_type="team_membership",
                entity_id=clean_team_id,
                before=before_payload,
                after=after_payload,
            )

            return updated

        if session is not None:
            return await _do_change(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_change(s)

    async def get_user_teams(
        self,
        discord_user_id: str | int,
        session: AsyncSession | None = None,
    ) -> list[tuple[Team, TeamMembership]]:
        """
        Recupera todos los equipos a los que pertenece un usuario con sus membresías asociadas,
        cargando de forma ansiosa la relación Team para prevenir errores fuera de sesión.
        """
        clean_user_id = _clean_user_id(discord_user_id)
        if clean_user_id is None:
            return []

        async def _do_get(s: AsyncSession) -> list[tuple[Team, TeamMembership]]:
            membership_repo = TeamMembershipRepository(s)
            memberships = await membership_repo.list_by_user(clean_user_id, with_team=True)
            return [(m.team, m) for m in memberships if m.team is not None]

        if session is not None:
            return await _do_get(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_get(s)


__all__ = [
    "CompetitivePositionConflictError",
    "InvalidCaptainRoleError",
    "PlayerNotTeamMemberError",
    "RosterSyncError",
    "RosterSyncService",
]
