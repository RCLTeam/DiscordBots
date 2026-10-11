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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory, transactional_session
from liga_bot.models.enums import AppRole, RosterMovementAction, RosterRole
from liga_bot.models.roster import DiscordUser, Player, Team, TeamMembership
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
)
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.utils.formatting import apply_team_tag, strip_team_tag
from liga_bot.utils.ids import clean_user_id_str, clean_uuid

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

            user_id_str = clean_user_id_str(member.id)
            if user_id_str is None:
                return None

            clean_actor_id = clean_user_id_str(actor_id)

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

            # 4. Insertar atómicamente si no existe (evita colisiones concurrentes)
            assigned_role = self.default_join_role
            membership = await membership_repo.insert_if_not_exists(
                team_id=team.id,
                discord_user_id=user_id_str,
                role=assigned_role,
                is_captain=False,
            )
            if membership is None:
                # Ocurrió un conflicto concurrente; recuperamos la membresía existente sin mutar
                existing = await membership_repo.get(team.id, user_id_str)
                return existing

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

            user_id_str = clean_user_id_str(member.id)
            if user_id_str is None:
                return False

            clean_actor_id = clean_user_id_str(actor_id)

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

    async def ensure_player(
        self,
        member: discord.Member,
        game_name: str,
        riot_tag: str | None = None,
        session: AsyncSession | None = None,
    ) -> Player:
        """
        Garantiza la cuenta de juego del miembro en la tabla players:
        - Asegura primero su registro en discord_users (integridad referencial).
        - Reutiliza la cuenta existente con el mismo (game_name, riot_tag), vinculándola
          al usuario si estaba suelta; si no existe, la crea.
        - La primera cuenta de un usuario queda marcada como principal (is_main).
        """

        async def _do_ensure(s: AsyncSession) -> Player:
            user_id_str = clean_user_id_str(member.id)
            if user_id_str is None:
                raise RosterSyncError("Identificador de usuario de Discord inválido.")

            await self._ensure_discord_user(
                session=s,
                discord_id=user_id_str,
                username=member.name,
                global_name=member.global_name,
            )

            clean_name = game_name.strip()
            if not clean_name:
                raise RosterSyncError("El nombre de invocador no puede estar vacío.")
            clean_tag = (riot_tag or "").strip() or None

            existing = await s.execute(
                select(Player).where(
                    Player.game_name == clean_name,
                    Player.riot_tag == clean_tag,
                )
            )
            player = existing.scalars().first()

            if player is not None:
                if player.discord_user_id != user_id_str:
                    player.discord_user_id = user_id_str
                    await s.flush()
                return player

            owned = await s.execute(select(Player).where(Player.discord_user_id == user_id_str))
            is_first = owned.scalars().first() is None

            player = Player(
                discord_user_id=user_id_str,
                game_name=clean_name,
                riot_tag=clean_tag,
                is_main=is_first,
            )
            s.add(player)
            await s.flush()
            await s.refresh(player)
            return player

        if session is not None:
            return await _do_ensure(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_ensure(s)

    async def list_team_tags(self, session: AsyncSession | None = None) -> list[str]:
        """Tags de todos los equipos registrados, para normalizar apodos."""

        async def _do_list(s: AsyncSession) -> list[str]:
            return [team.tag for team in await TeamRepository(s).list_all()]

        if session is not None:
            return await _do_list(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_list(s)

    async def transfer_player(
        self,
        member: discord.Member,
        team_role: discord.Role,
        new_position: RosterRole | str,
        actor_id: str | int | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[TeamMembership, Team, Team | None]:
        """
        Traspasa a un jugador al equipo asociado al rol de Discord indicado:
        - Resuelve el equipo destino por discord_role_id (RosterSyncError si no existe).
        - Libera la membresía previa en el equipo destino, si la tuviera.
        - Si la nueva posición es competitiva, libera también la que ocupe en cualquier
          otro club, porque un jugador solo puede tener una posición competitiva en la liga.
          Las posiciones no competitivas (coach, staff, partners) se conservan.
        - Crea la nueva membresía y registra movimientos (LEFT/JOINED) y auditoría.
        - Retorna la nueva membresía, el equipo destino y el de procedencia
          (None si no cambió de club).
        """
        position = (
            new_position if isinstance(new_position, RosterRole) else RosterRole(new_position)
        )

        async def _do_transfer(s: AsyncSession) -> tuple[TeamMembership, Team, Team | None]:
            team = await TeamRepository(s).get_by_role_id(team_role.id)
            if team is None:
                raise RosterSyncError(
                    f"El rol '{team_role.name}' no corresponde a ningún equipo registrado."
                )

            user_id_str = clean_user_id_str(member.id)
            if user_id_str is None:
                raise RosterSyncError("Identificador de usuario de Discord inválido.")

            clean_actor_id = clean_user_id_str(actor_id)

            await self._ensure_discord_user(
                session=s,
                discord_id=user_id_str,
                username=member.name,
                global_name=member.global_name,
            )
            if clean_actor_id is not None:
                await self._ensure_discord_user(session=s, discord_id=clean_actor_id)

            membership_repo = TeamMembershipRepository(s)
            movement_repo = RosterMovementRepository(s)
            audit_repo = AuditLogRepository(s)

            # Membresías que deben liberarse antes de crear la nueva
            to_release: list[TeamMembership] = []
            current_in_team = await membership_repo.get(team.id, user_id_str)
            if current_in_team is not None:
                to_release.append(current_in_team)
            if position.is_competitive():
                competitive = await membership_repo.get_competitive_membership(
                    user_id_str, with_team=True
                )
                if competitive is not None and competitive.team_id != team.id:
                    to_release.append(competitive)

            previous_team: Team | None = None
            for old in to_release:
                old_team_id = old.team_id
                old_role = old.role
                if old_team_id != team.id:
                    previous_team = old.team

                before_payload = {
                    "team_id": str(old_team_id),
                    "discord_user_id": user_id_str,
                    "role": old_role.value if isinstance(old_role, RosterRole) else str(old_role),
                    "is_captain": old.is_captain,
                }
                await membership_repo.delete(old)
                await movement_repo.record_movement(
                    team_id=old_team_id,
                    discord_user_id=user_id_str,
                    action=RosterMovementAction.LEFT,
                    role=old_role,
                    actor_id=clean_actor_id,
                )
                await audit_repo.log(
                    actor_discord_user_id=clean_actor_id,
                    action="roster.member_transferred_out",
                    entity_type="team_membership",
                    entity_id=old_team_id,
                    before=before_payload,
                    after=None,
                )

            membership = await membership_repo.upsert(
                team_id=team.id,
                discord_user_id=user_id_str,
                role=position,
                is_captain=False,
            )
            await movement_repo.record_movement(
                team_id=team.id,
                discord_user_id=user_id_str,
                action=RosterMovementAction.JOINED,
                role=position,
                actor_id=clean_actor_id,
            )
            await audit_repo.log(
                actor_discord_user_id=clean_actor_id,
                action="roster.member_transferred_in",
                entity_type="team_membership",
                entity_id=team.id,
                before=None,
                after={
                    "team_id": str(team.id),
                    "discord_user_id": user_id_str,
                    "role": position.value,
                    "is_captain": False,
                },
            )

            return membership, team, previous_team

        if session is not None:
            return await _do_transfer(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_transfer(s)

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
        clean_user_id = clean_user_id_str(discord_user_id)
        if clean_user_id is None:
            raise ValueError("El discord_user_id no puede ser nulo ni vacío.")

        clean_actor_id = clean_user_id_str(actor_id)
        if clean_actor_id is None:
            raise ValueError("El actor_id no puede ser nulo ni vacío.")

        clean_team_id = clean_uuid(team_id)
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
        clean_user_id = clean_user_id_str(discord_user_id)
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

    async def resolve_canonical_nick(
        self,
        discord_user_id: str | int,
        base_name: str,
        session: AsyncSession | None = None,
    ) -> str:
        """
        Resuelve el apodo canónico de un usuario según la prioridad competitiva.

        - Si el usuario ostenta una posición competitiva en algún equipo de la liga,
          se antepone el tag de dicho equipo (<TAG> <clean_base>).
        - Si solo posee roles no competitivos (coach, staff, partners) o ninguno,
          se eliminan los tags conocidos dejando el nombre limpio.
        - Se acota estrictamente a 32 caracteres (límite de apodo en Discord).
        """
        user_id_str = clean_user_id_str(discord_user_id)
        if not user_id_str:
            return base_name.strip()[:32].rstrip()

        async def _do_resolve(s: AsyncSession) -> str:
            known_tags = [t.tag for t in await TeamRepository(s).list_all()]
            comp = await TeamMembershipRepository(s).get_competitive_membership(
                user_id_str, with_team=True
            )
            clean_base = strip_team_tag(base_name, known_tags)
            if comp is not None and comp.team is not None and comp.team.tag:
                return apply_team_tag(clean_base, comp.team.tag, known_tags)[:32].rstrip()
            return clean_base[:32].rstrip()

        if session is not None:
            return await _do_resolve(session)

        async with transactional_session(self.session_factory) as s:
            return await _do_resolve(s)


__all__ = [
    "CompetitivePositionConflictError",
    "InvalidCaptainRoleError",
    "PlayerNotTeamMemberError",
    "RosterSyncError",
    "RosterSyncService",
]
