"""
Adversarial Empirical Challenge Suite for Milestone 3:
- Discord Role Event Hooks (handle_role_added, handle_role_removed).
- Rule 4 Invariant: Zero removal of team roles in Discord across multiple clubs.
- Rule 5 Invariant: Selective membership deletion strictly on explicit role removal.
- Rule 7 Invariant: Full audit and movement lifecycle with JSONB snapshots.
- Eager-loading contract in get_user_teams preventing greenlet / lazy loading errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.models.enums import AppRole, Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import AuditLog, DiscordUser, Team, TeamMembership
from liga_bot.repositories.roster_repo import (
    RosterMovementRepository,
    TeamMembershipRepository,
)
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    RosterSyncService,
)

# ---------------------------------------------------------------------------
# Helpers y Mocks de Discord
# ---------------------------------------------------------------------------


def create_mock_role(role_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.Role con atributos id, name y mention."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_member(
    user_id: int,
    name: str = "EmpiricalPlayer",
    global_name: str | None = "Empirical Player Global",
    roles: list[MagicMock] | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.Member registrando invocaciones a add/remove roles."""
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.global_name = global_name
    member.display_name = global_name or name
    member.avatar = MagicMock()
    member.avatar.key = f"avatar_{user_id}"
    member.roles = list(roles or [])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    return member


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_settings() -> Settings:
    return Settings(
        guild_id=1547725310508667010,
        staff_role_id=1547729760384319518,
    )


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def clean_roster_tables(migrated_db: AsyncEngine) -> AsyncGenerator[None, None]:
    """Aisla cada test limpiando las tablas compartidas."""
    async with migrated_db.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                "players, teams, discord_users CASCADE;"
            )
        )
        await conn.commit()
    yield
    async with migrated_db.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                "players, teams, discord_users CASCADE;"
            )
        )
        await conn.commit()


@pytest.fixture
def roster_service(
    session_factory: async_sessionmaker[AsyncSession], clean_settings: Settings
) -> RosterSyncService:
    return RosterSyncService(session_factory=session_factory, settings=clean_settings)


@pytest_asyncio.fixture
async def seed_three_teams(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Team, Team, Team]:
    """Siembra 3 equipos canónicos para pruebas de multi-membresía."""
    async with session_factory() as session:
        t1 = Team(
            name="Alpha Dragons",
            tag="ALP",
            slug="alpha-dragons",
            division=Division.PREMIER,
            discord_role_id=2001,
        )
        t2 = Team(
            name="Beta Wolves",
            tag="BET",
            slug="beta-wolves",
            division=Division.PREMIER,
            discord_role_id=2002,
        )
        t3 = Team(
            name="Gamma Eagles",
            tag="GAM",
            slug="gamma-eagles",
            division=Division.ASCEND,
            discord_role_id=2003,
        )
        session.add_all([t1, t2, t3])
        await session.commit()
        await session.refresh(t1)
        await session.refresh(t2)
        await session.refresh(t3)
        return t1, t2, t3


@pytest_asyncio.fixture
async def seed_actor_admin(session_factory: async_sessionmaker[AsyncSession]) -> DiscordUser:
    """Siembra un usuario administrador para actor de auditoría."""
    async with session_factory() as session:
        actor = DiscordUser(
            discord_id="999000",
            username="referee_admin",
            role=AppRole.ADMIN,
        )
        session.add(actor)
        await session.commit()
        await session.refresh(actor)
        return actor


# ===========================================================================
# Empirical Challenge Suite
# ===========================================================================


class TestEmpiricalRosterSyncLifecycle:
    """Pruebas empíricas adversariales sobre el ciclo de vida de roles y plantillas."""

    @pytest.mark.asyncio
    async def test_challenge_rapid_sequential_role_adds_across_three_teams(
        self,
        roster_service: RosterSyncService,
        seed_three_teams: tuple[Team, Team, Team],
        seed_actor_admin: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Desafío 1:
        - Incorporación secuencial rápida de roles de 3 equipos distintos para el mismo miembro.
        - Verificar que el usuario termina con 3 membresías activas en la BD.
        - Verificar empíricamente que member.remove_roles NUNCA fue invocado (Regla 4).
        - Verificar 3 movimientos 'JOINED' y 3 audit logs correspondientes creados.
        """
        team_alpha, team_beta, team_gamma = seed_three_teams
        member_id = 777001
        member = create_mock_member(user_id=member_id, name="MultiTeamMember")

        role_alpha = create_mock_role(team_alpha.discord_role_id, team_alpha.name)
        role_beta = create_mock_role(team_beta.discord_role_id, team_beta.name)
        role_gamma = create_mock_role(team_gamma.discord_role_id, team_gamma.name)

        # 1. Asignaciones rápidas secuenciales
        mem_alpha = await roster_service.handle_role_added(
            member=member, role=role_alpha, actor_id=seed_actor_admin.discord_id
        )
        mem_beta = await roster_service.handle_role_added(
            member=member, role=role_beta, actor_id=seed_actor_admin.discord_id
        )
        mem_gamma = await roster_service.handle_role_added(
            member=member, role=role_gamma, actor_id=seed_actor_admin.discord_id
        )

        assert mem_alpha is not None
        assert mem_beta is not None
        assert mem_gamma is not None

        # 2. Verificación Regla 4: remove_roles NUNCA llamado
        member.remove_roles.assert_not_called()
        assert member.remove_roles.call_count == 0

        # 3. Verificación en BD: 3 membresías activas
        async with session_factory() as session:
            membership_repo = TeamMembershipRepository(session)
            memberships = await membership_repo.list_by_user(str(member_id))
            assert len(memberships) == 3

            team_ids = {m.team_id for m in memberships}
            assert team_ids == {team_alpha.id, team_beta.id, team_gamma.id}

            for m in memberships:
                assert m.role == RosterRole.STAFF
                assert m.is_captain is False
                assert m.discord_user_id == str(member_id)

            # 4. Verificación de 3 movimientos JOINED
            movement_repo = RosterMovementRepository(session)
            movements = await movement_repo.list_by_user(str(member_id))
            assert len(movements) == 3

            movement_actions = [mov.action for mov in movements]
            assert movement_actions == [
                RosterMovementAction.JOINED,
                RosterMovementAction.JOINED,
                RosterMovementAction.JOINED,
            ]
            movement_teams = {mov.team_id for mov in movements}
            assert movement_teams == {team_alpha.id, team_beta.id, team_gamma.id}
            for mov in movements:
                assert mov.role == RosterRole.STAFF
                assert mov.actor_id == seed_actor_admin.discord_id

            # 5. Verificación de 3 audit_logs
            stmt = (
                select(AuditLog)
                .where(AuditLog.actor_discord_user_id == seed_actor_admin.discord_id)
                .order_by(AuditLog.created_at.asc())
            )
            res = await session.execute(stmt)
            logs = list(res.scalars().all())
            assert len(logs) == 3

            for log in logs:
                assert log.action == "roster.member_joined"
                assert log.entity_type == "team_membership"
                assert log.entity_id in {team_alpha.id, team_beta.id, team_gamma.id}
                assert log.before is None
                assert log.after is not None
                assert log.after["discord_user_id"] == str(member_id)
                assert log.after["role"] == "staff"
                assert log.after["is_captain"] is False

    @pytest.mark.asyncio
    async def test_challenge_selective_role_removed_preserves_other_memberships(
        self,
        roster_service: RosterSyncService,
        seed_three_teams: tuple[Team, Team, Team],
        seed_actor_admin: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Desafío 2:
        - Miembro con 3 membresías activas.
        - Retirar 1 de los 3 roles (Team Beta).
        - Verificar que ÚNICAMENTE se eliminó la membresía de Team Beta en BD (Regla 5).
        - Las otras 2 membresías (Team Alpha, Team Gamma) permanecen intactas y activas.
        - Verificar 1 movimiento 'LEFT' y 1 audit log con snapshot previo 'before'.
        """
        team_alpha, team_beta, team_gamma = seed_three_teams
        member_id = 777002
        member = create_mock_member(user_id=member_id, name="SelectiveRemovalUser")

        role_alpha = create_mock_role(team_alpha.discord_role_id, team_alpha.name)
        role_beta = create_mock_role(team_beta.discord_role_id, team_beta.name)
        role_gamma = create_mock_role(team_gamma.discord_role_id, team_gamma.name)

        # Incorporar a los 3 equipos
        await roster_service.handle_role_added(
            member=member, role=role_alpha, actor_id=seed_actor_admin.discord_id
        )
        await roster_service.handle_role_added(
            member=member, role=role_beta, actor_id=seed_actor_admin.discord_id
        )
        await roster_service.handle_role_added(
            member=member, role=role_gamma, actor_id=seed_actor_admin.discord_id
        )

        # Retirar ÚNICAMENTE el rol de Team Beta
        removed_ok = await roster_service.handle_role_removed(
            member=member, role=role_beta, actor_id=seed_actor_admin.discord_id
        )
        assert removed_ok is True

        async with session_factory() as session:
            membership_repo = TeamMembershipRepository(session)
            movement_repo = RosterMovementRepository(session)

            # Membresía de Beta debe haber sido eliminada
            beta_mem = await membership_repo.get(team_beta.id, str(member_id))
            assert beta_mem is None

            # Membresías de Alpha y Gamma deben seguir activas
            alpha_mem = await membership_repo.get(team_alpha.id, str(member_id))
            gamma_mem = await membership_repo.get(team_gamma.id, str(member_id))
            assert alpha_mem is not None
            assert gamma_mem is not None
            assert alpha_mem.role == RosterRole.STAFF
            assert gamma_mem.role == RosterRole.STAFF

            all_user_mems = await membership_repo.list_by_user(str(member_id))
            assert len(all_user_mems) == 2
            assert {m.team_id for m in all_user_mems} == {team_alpha.id, team_gamma.id}

            # Movimientos: 3 JOINED + 1 LEFT (total 4)
            movements = await movement_repo.list_by_user(str(member_id))
            assert len(movements) == 4
            left_movements = [m for m in movements if m.action == RosterMovementAction.LEFT]
            assert len(left_movements) == 1
            left_mov = left_movements[0]
            assert left_mov.team_id == team_beta.id
            assert left_mov.discord_user_id == str(member_id)
            assert left_mov.role == RosterRole.STAFF
            assert left_mov.actor_id == seed_actor_admin.discord_id

            # Audit logs: buscar la entrada de member_left
            stmt = select(AuditLog).where(
                AuditLog.actor_discord_user_id == seed_actor_admin.discord_id,
                AuditLog.action == "roster.member_left",
            )
            res = await session.execute(stmt)
            left_logs = list(res.scalars().all())
            assert len(left_logs) == 1
            log = left_logs[0]
            assert log.entity_type == "team_membership"
            assert log.entity_id == team_beta.id
            assert log.after is None
            assert log.before is not None
            assert log.before["team_id"] == str(team_beta.id)
            assert log.before["discord_user_id"] == str(member_id)
            assert log.before["role"] == "staff"
            assert log.before["is_captain"] is False

    @pytest.mark.asyncio
    async def test_challenge_get_user_teams_eager_loading_without_greenlet_errors(
        self,
        roster_service: RosterSyncService,
        seed_three_teams: tuple[Team, Team, Team],
        seed_actor_admin: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Desafío 3:
        - Con las 2 membresías restantes (Team Alpha y Team Gamma).
        - Invocar get_user_teams(discord_user_id).
        - Verificar que retorna exactamente los 2 equipos restantes.
        - Verificar que los objetos Team asociados están cargados ansiosamente (eager-loaded)
          y que acceder a sus atributos fuera de una sesión activa NO lanza MissingGreenlet.
        """
        team_alpha, team_beta, team_gamma = seed_three_teams
        member_id = 777003
        member = create_mock_member(user_id=member_id, name="EagerLoadingUser")

        role_alpha = create_mock_role(team_alpha.discord_role_id, team_alpha.name)
        role_beta = create_mock_role(team_beta.discord_role_id, team_beta.name)
        role_gamma = create_mock_role(team_gamma.discord_role_id, team_gamma.name)

        # Incorporar a 3 y retirar Beta
        await roster_service.handle_role_added(member=member, role=role_alpha)
        await roster_service.handle_role_added(member=member, role=role_beta)
        await roster_service.handle_role_added(member=member, role=role_gamma)
        await roster_service.handle_role_removed(member=member, role=role_beta)

        # Consulta get_user_teams
        user_teams = await roster_service.get_user_teams(member_id)
        assert len(user_teams) == 2

        returned_team_ids = {team.id for team, _ in user_teams}
        assert returned_team_ids == {team_alpha.id, team_gamma.id}

        # Prueba de fuego para Greenlet / Lazy Loading:
        # Acceder a propiedades de Team y de TeamMembership.team desvinculadas de sesión
        for team, membership in user_teams:
            assert isinstance(team, Team)
            assert isinstance(membership, TeamMembership)

            # Atributos de Team accesibles sin MissingGreenlet
            assert team.id in {team_alpha.id, team_gamma.id}
            assert team.name in {team_alpha.name, team_gamma.name}
            assert team.tag in {team_alpha.tag, team_gamma.tag}
            assert team.division in {Division.PREMIER, Division.ASCEND}
            assert team.discord_role_id in {2001, 2003}

            # Relación bidireccional membership.team ansiosa
            assert membership.team is not None
            assert membership.team.id == team.id
            assert membership.team.slug in {"alpha-dragons", "gamma-eagles"}

    @pytest.mark.asyncio
    async def test_challenge_concurrent_role_additions_stress(
        self,
        roster_service: RosterSyncService,
        seed_three_teams: tuple[Team, Team, Team],
        seed_actor_admin: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Desafío adicional (Stress Concurrencia):
        - Lanzar asyncio.gather con handle_role_added para los 3 equipos concurrentemente.
        - Verificar que la serialización de transacciones y el helper _ensure_discord_user
          no colisionan ni corrompen el estado.
        - Verificar 3 membresías y 3 movimientos.
        """
        team_alpha, team_beta, team_gamma = seed_three_teams
        member_id = 777004
        member = create_mock_member(user_id=member_id, name="ConcurrentUser")

        role_alpha = create_mock_role(team_alpha.discord_role_id, team_alpha.name)
        role_beta = create_mock_role(team_beta.discord_role_id, team_beta.name)
        role_gamma = create_mock_role(team_gamma.discord_role_id, team_gamma.name)

        results = await asyncio.gather(
            roster_service.handle_role_added(member=member, role=role_alpha, actor_id="999000"),
            roster_service.handle_role_added(member=member, role=role_beta, actor_id="999000"),
            roster_service.handle_role_added(member=member, role=role_gamma, actor_id="999000"),
        )

        assert all(r is not None for r in results)

        async with session_factory() as session:
            membership_repo = TeamMembershipRepository(session)
            mems = await membership_repo.list_by_user(str(member_id))
            assert len(mems) == 3

            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_user(str(member_id))
            assert len(movs) == 3

    @pytest.mark.asyncio
    async def test_challenge_idempotence_and_non_team_roles(
        self,
        roster_service: RosterSyncService,
        seed_three_teams: tuple[Team, Team, Team],
        seed_actor_admin: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Desafío adicional (Idempotencia y Roles Huérfanos):
        - Agregar repetidamente el mismo rol de equipo no duplica movimientos ni auditorías.
        - Agregar o retirar un rol de Discord que no pertenece a ningún club retorna None/False.
        """
        team_alpha, _, _ = seed_three_teams
        member_id = 777005
        member = create_mock_member(user_id=member_id, name="IdempotentUser")
        role_alpha = create_mock_role(team_alpha.discord_role_id, team_alpha.name)

        # 1. Primera asignación
        mem1 = await roster_service.handle_role_added(member=member, role=role_alpha)
        assert mem1 is not None

        # 2. Segunda asignación (idempotente)
        mem2 = await roster_service.handle_role_added(member=member, role=role_alpha)
        assert mem2 is not None
        assert mem1.team_id == mem2.team_id

        async with session_factory() as session:
            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_user(str(member_id))
            assert len(movs) == 1, "No debe registrar movimiento duplicado ante rol repetido"

            stmt = select(AuditLog).where(AuditLog.entity_id == team_alpha.id)
            res = await session.execute(stmt)
            logs = list(res.scalars().all())
            assert len(logs) == 1, "No debe duplicar entrada de auditoría"

        # 3. Rol ajeno a equipos
        orphan_role = create_mock_role(role_id=999999, name="RandomServerRole")
        res_added = await roster_service.handle_role_added(member=member, role=orphan_role)
        assert res_added is None

        res_removed = await roster_service.handle_role_removed(member=member, role=orphan_role)
        assert res_removed is False

    @pytest.mark.asyncio
    async def test_challenge_multi_team_with_custom_positions_and_selective_removal(
        self,
        roster_service: RosterSyncService,
        seed_three_teams: tuple[Team, Team, Team],
        seed_actor_admin: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Desafío integral (Multi-Equipo con Posiciones Diferenciadas):
        - Usuario pertenece a Alpha, Beta y Gamma.
        - En Alpha: promovido a MID con capitanía (rol competitivo titular).
        - En Beta: modificado a COACH (rol no competitivo).
        - En Gamma: permanece como STAFF.
        - Se intenta asignar TOP en Beta -> Falla con CompetitivePositionConflictError (Regla 6).
        - Se retira rol Alpha en Discord -> Solo Alpha removido con snapshot del rol previo MID.
        - Beta (COACH) y Gamma (STAFF) permanecen intactos.
        - get_user_teams devuelve Beta y Gamma con sus roles correspondientes.
        """
        team_alpha, team_beta, team_gamma = seed_three_teams
        member_id = 777006
        member = create_mock_member(user_id=member_id, name="ProPlayer")

        role_alpha = create_mock_role(team_alpha.discord_role_id, team_alpha.name)
        role_beta = create_mock_role(team_beta.discord_role_id, team_beta.name)
        role_gamma = create_mock_role(team_gamma.discord_role_id, team_gamma.name)

        # Incorporar a los 3 equipos
        await roster_service.handle_role_added(
            member=member, role=role_alpha, actor_id=seed_actor_admin.discord_id
        )
        await roster_service.handle_role_added(
            member=member, role=role_beta, actor_id=seed_actor_admin.discord_id
        )
        await roster_service.handle_role_added(
            member=member, role=role_gamma, actor_id=seed_actor_admin.discord_id
        )

        # Promover en Alpha a MID + Capitán
        await roster_service.change_player_position(
            discord_user_id=member_id,
            team_id=team_alpha.id,
            new_role=RosterRole.MID,
            actor_id=seed_actor_admin.discord_id,
            is_captain=True,
        )

        # Cambiar en Beta a COACH
        await roster_service.change_player_position(
            discord_user_id=member_id,
            team_id=team_beta.id,
            new_role=RosterRole.COACH,
            actor_id=seed_actor_admin.discord_id,
            is_captain=False,
        )

        # Intento de violar la Regla 6: asignar TOP en Beta teniendo MID en Alpha
        with pytest.raises(CompetitivePositionConflictError) as exc_info:
            await roster_service.change_player_position(
                discord_user_id=member_id,
                team_id=team_beta.id,
                new_role=RosterRole.TOP,
                actor_id=seed_actor_admin.discord_id,
            )
        assert exc_info.value.existing_role == "mid"
        assert exc_info.value.attempted_role == "top"

        # Retirar rol de Alpha en Discord
        removed = await roster_service.handle_role_removed(
            member=member, role=role_alpha, actor_id=seed_actor_admin.discord_id
        )
        assert removed is True

        # Verificar en BD
        async with session_factory() as session:
            membership_repo = TeamMembershipRepository(session)
            remaining = await membership_repo.list_by_user(str(member_id))
            assert len(remaining) == 2

            m_beta = await membership_repo.get(team_beta.id, str(member_id))
            m_gamma = await membership_repo.get(team_gamma.id, str(member_id))
            assert m_beta is not None
            assert m_beta.role == RosterRole.COACH
            assert m_gamma is not None
            assert m_gamma.role == RosterRole.STAFF

            # Verificar auditoría de salida de Alpha: captured role was 'mid' and was_captain=True
            stmt = select(AuditLog).where(
                AuditLog.entity_id == team_alpha.id,
                AuditLog.action == "roster.member_left",
            )
            res = await session.execute(stmt)
            alpha_left_log = res.scalars().first()
            assert alpha_left_log is not None
            assert alpha_left_log.before["role"] == "mid"
            assert alpha_left_log.before["is_captain"] is True

        # get_user_teams devuelve Beta y Gamma
        user_teams = await roster_service.get_user_teams(member_id)
        assert len(user_teams) == 2
        teams_map = {t.id: m.role for t, m in user_teams}
        assert teams_map[team_beta.id] == RosterRole.COACH
        assert teams_map[team_gamma.id] == RosterRole.STAFF
