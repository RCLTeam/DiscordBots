"""
Empirical Adversarial Challenge Suite: Competitive Invariant & Captaincy Stress on PGlite.

Directly tests and challenges RosterSyncService.change_player_position against:
1. Multi-team competitive conflict permutations:
   - User is member of Team A and Team B.
   - User is assigned competitive role in Team A ('mid').
   - Attempt to assign competitive role in Team B
     ('top', 'jungle', 'mid', 'adc', 'support', 'substitute').
     Verify CompetitivePositionConflictError is raised with descriptive message
     for every competitive role.
   - Attempt to assign non-competitive role in Team B ('coach', 'staff', 'partners').
     Verify all succeed without error.
   - Change user role in Team A to non-competitive ('staff').
     Now attempt to assign competitive role in Team B. Verify it succeeds.
2. Captaincy constraint stress:
   - Attempt to set is_captain=True with non-starter role 'substitute'.
     Verify InvalidCaptainRoleError is raised.
   - Attempt to set is_captain=True with non-competitive roles
     'coach', 'staff', 'partners'. Verify InvalidCaptainRoleError is raised.
   - Set is_captain=True with starter role 'top'.
     Verify it succeeds and records PROMOTED_TO_CAPTAIN movement and audit log.
3. Advanced edge cases & transactional survivability:
   - Within-team competitive role switching (mid -> top) without conflict.
   - 3-team chain conflict and transfer.
   - Transactional rollback on conflict (zero orphan movements or audit logs).
   - String role coercion ('top', 'coach', etc.).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.models.enums import AppRole, Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import DiscordUser, Team
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
)
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    InvalidCaptainRoleError,
    PlayerNotTeamMemberError,
    RosterSyncService,
)

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
    """Limpia las tablas compartidas antes y después de cada test."""
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
def roster_sync_service(
    session_factory: async_sessionmaker[AsyncSession], clean_settings: Settings
) -> RosterSyncService:
    return RosterSyncService(session_factory=session_factory, settings=clean_settings)


@pytest_asyncio.fixture
async def seed_teams(session_factory: async_sessionmaker[AsyncSession]) -> tuple[Team, Team, Team]:
    """Siembra 3 equipos en base de datos."""
    async with session_factory() as session:
        t1 = Team(
            name="Alpha Dragons",
            tag="ADG",
            slug="alpha-dragons",
            division=Division.PREMIER,
            discord_role_id=2001,
        )
        t2 = Team(
            name="Beta Phoenix",
            tag="BPX",
            slug="beta-phoenix",
            division=Division.PREMIER,
            discord_role_id=2002,
        )
        t3 = Team(
            name="Gamma Krakens",
            tag="GKR",
            slug="gamma-krakens",
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
async def seed_actor(session_factory: async_sessionmaker[AsyncSession]) -> DiscordUser:
    """Siembra un actor de staff para trazabilidad de auditoría."""
    async with session_factory() as session:
        actor = DiscordUser(
            discord_id="999888777",
            username="referee_admin",
            role=AppRole.ADMIN,
        )
        session.add(actor)
        await session.commit()
        await session.refresh(actor)
        return actor


@pytest_asyncio.fixture
async def seed_dual_member(
    session_factory: async_sessionmaker[AsyncSession],
    seed_teams: tuple[Team, Team, Team],
) -> DiscordUser:
    """Siembra un usuario que ya pertenece tanto a Team A como a Team B."""
    team_a, team_b, _ = seed_teams
    async with session_factory() as session:
        user = DiscordUser(
            discord_id="111222333444",
            username="dual_club_player",
            role=AppRole.VIEWER,
        )
        session.add(user)
        await session.flush()

        m_repo = TeamMembershipRepository(session)
        # En Team A empieza como MID (competitivo)
        await m_repo.create(team_a.id, user.discord_id, RosterRole.MID, is_captain=False)
        # En Team B empieza como STAFF (no competitivo)
        await m_repo.create(team_b.id, user.discord_id, RosterRole.STAFF, is_captain=False)
        await session.commit()
        await session.refresh(user)
        return user


# ===========================================================================
# 1. Multi-team Competitive Conflict Permutations
# ===========================================================================


class TestMultiTeamCompetitiveConflictPermutations:
    """
    Stress-testing de la invariante deportiva de posición única entre múltiples clubes:
    Un jugador con rol competitivo en Team A ('mid') debe ver rechazado cualquier
    intento de asignarle un rol competitivo en Team B, pero debe permitir roles
    no competitivos y desbloquearse si se demueve en Team A.
    """

    COMPETITIVE_ROLES = [
        RosterRole.TOP,
        RosterRole.JUNGLE,
        RosterRole.MID,
        RosterRole.ADC,
        RosterRole.SUPPORT,
        RosterRole.SUBSTITUTE,
    ]

    NON_COMPETITIVE_ROLES = [
        RosterRole.COACH,
        RosterRole.STAFF,
        RosterRole.PARTNERS,
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "comp_role",
        [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
            RosterRole.SUBSTITUTE,
        ],
        ids=["top", "jungle", "mid", "adc", "support", "substitute"],
    )
    async def test_attempt_assign_competitive_role_in_team_b_raises_conflict(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        seed_dual_member: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
        comp_role: RosterRole,
    ):
        """
        Para CADA rol competitivo, intentar asignarlo en Team B cuando el usuario
        ya es MID en Team A debe lanzar CompetitivePositionConflictError con mensaje descriptivo.
        """
        team_a, team_b, _ = seed_teams

        with pytest.raises(CompetitivePositionConflictError) as exc_info:
            await roster_sync_service.change_player_position(
                discord_user_id=seed_dual_member.discord_id,
                team_id=team_b.id,
                new_role=comp_role,
                actor_id=seed_actor.discord_id,
            )

        err = exc_info.value
        # Verificación forense de atributos de la excepción
        assert err.discord_user_id == seed_dual_member.discord_id
        assert err.existing_team_id == team_a.id
        assert err.new_team_id == team_b.id
        assert err.existing_role == "mid"
        assert err.attempted_role == comp_role.value

        # Verificación del mensaje descriptivo
        msg = str(err)
        assert "Conflicto de posición competitiva" in msg
        assert seed_dual_member.discord_id in msg
        assert "mid" in msg
        assert comp_role.value in msg
        assert str(team_a.id) in msg
        assert str(team_b.id) in msg

        # Verificación de integridad en BD: la membresía en Team B sigue inalterada
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            mem_b = await m_repo.get(team_b.id, seed_dual_member.discord_id)
            assert mem_b is not None
            assert mem_b.role == RosterRole.STAFF
            assert mem_b.is_captain is False

            # Verificar que no se creó ningún movimiento ni registro de auditoría en Team B
            mov_repo = RosterMovementRepository(session)
            movs_b = await mov_repo.list_by_team(team_b.id)
            assert len(movs_b) == 0

            audit_repo = AuditLogRepository(session)
            audits_b = await audit_repo.list_by_entity("team_membership", team_b.id)
            assert len(audits_b) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "non_comp_role",
        [
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
        ],
        ids=["coach", "staff", "partners"],
    )
    async def test_attempt_assign_non_competitive_role_in_team_b_succeeds(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        seed_dual_member: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
        non_comp_role: RosterRole,
    ):
        """
        Asignar cualquier rol no competitivo ('coach', 'staff', 'partners') en Team B
        cuando el usuario es MID en Team A debe ejecutarse con éxito sin lanzar excepción.
        """
        team_a, team_b, _ = seed_teams

        updated = await roster_sync_service.change_player_position(
            discord_user_id=seed_dual_member.discord_id,
            team_id=team_b.id,
            new_role=non_comp_role,
            actor_id=seed_actor.discord_id,
        )

        assert updated.team_id == team_b.id
        assert updated.discord_user_id == seed_dual_member.discord_id
        assert updated.role == non_comp_role
        assert updated.is_captain is False

        # Verificar persistencia en base de datos
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            mem_b = await m_repo.get(team_b.id, seed_dual_member.discord_id)
            assert mem_b is not None
            assert mem_b.role == non_comp_role

            # La membresía en Team A debe seguir siendo MID intacta
            mem_a = await m_repo.get(team_a.id, seed_dual_member.discord_id)
            assert mem_a is not None
            assert mem_a.role == RosterRole.MID

            # Movimiento registrado en Team B
            mov_repo = RosterMovementRepository(session)
            movs_b = await mov_repo.list_by_team(team_b.id)
            assert len(movs_b) == 1
            assert movs_b[0].action == RosterMovementAction.ROLE_CHANGED
            assert movs_b[0].role == non_comp_role
            assert movs_b[0].actor_id == seed_actor.discord_id

            # Trazabilidad en audit_logs
            audit_repo = AuditLogRepository(session)
            audits_b = await audit_repo.list_by_entity("team_membership", team_b.id)
            assert len(audits_b) == 1
            assert audits_b[0].before["role"] == "staff"
            assert audits_b[0].after["role"] == non_comp_role.value
            assert audits_b[0].actor_discord_user_id == seed_actor.discord_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target_comp_role",
        [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
            RosterRole.SUBSTITUTE,
        ],
        ids=["top", "jungle", "mid", "adc", "support", "substitute"],
    )
    async def test_demoting_team_a_to_non_competitive_unblocks_competitive_in_team_b(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        seed_dual_member: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
        target_comp_role: RosterRole,
    ):
        """
        Cambiar el rol del usuario en Team A de 'mid' a no competitivo ('staff')
        debe permitir inmediatamente asignar cualquier rol competitivo en Team B.
        """
        team_a, team_b, _ = seed_teams

        # 1. Demover rol en Team A a STAFF
        demoted_a = await roster_sync_service.change_player_position(
            discord_user_id=seed_dual_member.discord_id,
            team_id=team_a.id,
            new_role=RosterRole.STAFF,
            actor_id=seed_actor.discord_id,
        )
        assert demoted_a.role == RosterRole.STAFF

        # 2. Ahora asignar rol competitivo en Team B debe triunfar
        assigned_b = await roster_sync_service.change_player_position(
            discord_user_id=seed_dual_member.discord_id,
            team_id=team_b.id,
            new_role=target_comp_role,
            actor_id=seed_actor.discord_id,
        )
        assert assigned_b.role == target_comp_role

        # 3. Validar estado general con TeamMembershipRepository
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            comp_mem = await m_repo.get_competitive_membership(seed_dual_member.discord_id)
            assert comp_mem is not None
            assert comp_mem.team_id == team_b.id
            assert comp_mem.role == target_comp_role

            mem_a = await m_repo.get(team_a.id, seed_dual_member.discord_id)
            assert mem_a is not None
            assert mem_a.role == RosterRole.STAFF


# ===========================================================================
# 2. Captaincy Constraint Stress
# ===========================================================================


class TestCaptaincyConstraintStress:
    """
    Stress-testing de las restricciones de capitanía:
    Solo las posiciones titulares ('top', 'jungle', 'mid', 'adc', 'support') pueden
    ostentar is_captain=True. Suplentes ('substitute') y roles no competitivos
    ('coach', 'staff', 'partners') deben ser rechazados rigurosamente con InvalidCaptainRoleError.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_cap_role",
        [
            RosterRole.SUBSTITUTE,
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
        ],
        ids=["substitute", "coach", "staff", "partners"],
    )
    async def test_attempt_set_captain_with_non_starter_roles_raises_invalid_captain_role_error(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
        invalid_cap_role: RosterRole,
    ):
        """
        Intentar asignar is_captain=True con roles no titulares ('substitute', 'coach',
        'staff', 'partners') debe fallar con InvalidCaptainRoleError antes de realizar mutaciones.
        """
        team_a, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="555444333222", username="aspiring_captain")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_a.id, user.discord_id, RosterRole.STAFF, is_captain=False)
            await session.commit()

        with pytest.raises(InvalidCaptainRoleError) as exc_info:
            await roster_sync_service.change_player_position(
                discord_user_id="555444333222",
                team_id=team_a.id,
                new_role=invalid_cap_role,
                actor_id=seed_actor.discord_id,
                is_captain=True,
            )

        err = exc_info.value
        assert err.role == invalid_cap_role.value
        msg = str(err)
        assert invalid_cap_role.value in msg
        assert "no es una posición titular permitida para la capitanía" in msg
        assert "top', 'jungle', 'mid', 'adc', 'support" in msg

        # Verificar que la base de datos no fue mutada
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            mem = await m_repo.get(team_a.id, "555444333222")
            assert mem is not None
            assert mem.role == RosterRole.STAFF
            assert mem.is_captain is False

            # Cero movimientos y cero auditorías añadidas
            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_team(team_a.id)
            assert len(movs) == 0

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "starter_role",
        [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
        ],
        ids=["top", "jungle", "mid", "adc", "support"],
    )
    async def test_set_captain_true_with_starter_roles_succeeds_and_records_promotion(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
        starter_role: RosterRole,
    ):
        """
        Asignar is_captain=True con cualquier posición titular ('top', 'jungle', 'mid',
        'adc', 'support') debe completarse con éxito y registrar movimiento de PROMOTED_TO_CAPTAIN.
        """
        team_a, _, _ = seed_teams
        user_id = f"777000{starter_role.value}"
        async with session_factory() as session:
            user = DiscordUser(discord_id=user_id, username=f"cap_{starter_role.value}")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            # Inicialmente tiene el rol starter pero no es capitán
            await m_repo.create(team_a.id, user.discord_id, starter_role, is_captain=False)
            await session.commit()

        # Ascender a capitán manteniendo la misma posición titular
        updated = await roster_sync_service.change_player_position(
            discord_user_id=user_id,
            team_id=team_a.id,
            new_role=starter_role,
            actor_id=seed_actor.discord_id,
            is_captain=True,
        )

        assert updated.role == starter_role
        assert updated.is_captain is True

        # Verificar BD y trazabilidad completa
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            mem = await m_repo.get(team_a.id, user_id)
            assert mem is not None
            assert mem.role == starter_role
            assert mem.is_captain is True

            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_team(team_a.id)
            assert len(movs) == 1
            assert movs[0].action == RosterMovementAction.PROMOTED_TO_CAPTAIN
            assert movs[0].role == starter_role
            assert movs[0].actor_id == seed_actor.discord_id

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == 1
            assert audits[0].before["is_captain"] is False
            assert audits[0].after["is_captain"] is True
            assert audits[0].actor_discord_user_id == seed_actor.discord_id


# ===========================================================================
# 3. Advanced Edge Cases & Fuzzing Probes
# ===========================================================================


class TestAdvancedCompetitiveEdgeCases:
    """
    Casos límite adversariales adicionales:
    - Cambio de posición competitiva dentro del mismo equipo.
    - Cadena de tres equipos (Team A -> Team B -> Team C).
    - Intento de mutación en equipo donde no es miembro.
    - Soporte de strings en lugar de Enums para duck-typing.
    - Idempotencia en asignación repetida con mismos valores.
    """

    @pytest.mark.asyncio
    async def test_within_team_competitive_role_switch_allowed(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        seed_dual_member: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        El usuario es MID en Team A y STAFF en Team B.
        Cambiar de MID a TOP dentro de Team A NO viola la regla competitiva,
        ya que el rol previo pertenece al mismo club.
        """
        team_a, _, _ = seed_teams

        updated = await roster_sync_service.change_player_position(
            discord_user_id=seed_dual_member.discord_id,
            team_id=team_a.id,
            new_role=RosterRole.TOP,
            actor_id=seed_actor.discord_id,
        )

        assert updated.team_id == team_a.id
        assert updated.role == RosterRole.TOP

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            comp_mem = await m_repo.get_competitive_membership(seed_dual_member.discord_id)
            assert comp_mem is not None
            assert comp_mem.team_id == team_a.id
            assert comp_mem.role == RosterRole.TOP

    @pytest.mark.asyncio
    async def test_three_team_conflict_and_transfer_chain(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Usuario pertenece a Team A, Team B y Team C simultáneamente:
        - Team A: ADC
        - Team B: STAFF
        - Team C: COACH
        1. Intento de asignar JUNGLE en Team B -> Falla por conflicto con Team A.
        2. Intento de asignar SUPPORT en Team C -> Falla por conflicto con Team A.
        3. En Team A se pasa a COACH.
        4. Ahora se asigna SUPPORT en Team C -> Triunfa.
        5. Ahora se intenta JUNGLE en Team B -> Falla por conflicto con Team C.
        """
        team_a, team_b, team_c = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="999111222", username="tri_club_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_a.id, user.discord_id, RosterRole.ADC)
            await m_repo.create(team_b.id, user.discord_id, RosterRole.STAFF)
            await m_repo.create(team_c.id, user.discord_id, RosterRole.COACH)
            await session.commit()

        # 1. Team B falla
        with pytest.raises(CompetitivePositionConflictError) as exc_b:
            await roster_sync_service.change_player_position(
                discord_user_id="999111222",
                team_id=team_b.id,
                new_role=RosterRole.JUNGLE,
                actor_id=seed_actor.discord_id,
            )
        assert exc_b.value.existing_team_id == team_a.id

        # 2. Team C falla
        with pytest.raises(CompetitivePositionConflictError) as exc_c:
            await roster_sync_service.change_player_position(
                discord_user_id="999111222",
                team_id=team_c.id,
                new_role=RosterRole.SUPPORT,
                actor_id=seed_actor.discord_id,
            )
        assert exc_c.value.existing_team_id == team_a.id

        # 3. Demover en Team A
        await roster_sync_service.change_player_position(
            discord_user_id="999111222",
            team_id=team_a.id,
            new_role=RosterRole.COACH,
            actor_id=seed_actor.discord_id,
        )

        # 4. Asignar en Team C triunfa
        up_c = await roster_sync_service.change_player_position(
            discord_user_id="999111222",
            team_id=team_c.id,
            new_role=RosterRole.SUPPORT,
            actor_id=seed_actor.discord_id,
        )
        assert up_c.role == RosterRole.SUPPORT

        # 5. Team B ahora falla por conflicto con Team C
        with pytest.raises(CompetitivePositionConflictError) as exc_b2:
            await roster_sync_service.change_player_position(
                discord_user_id="999111222",
                team_id=team_b.id,
                new_role=RosterRole.JUNGLE,
                actor_id=seed_actor.discord_id,
            )
        assert exc_b2.value.existing_team_id == team_c.id
        assert exc_b2.value.existing_role == "support"
        assert exc_b2.value.attempted_role == "jungle"

    @pytest.mark.asyncio
    async def test_non_member_raises_player_not_team_member_error(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
    ):
        """Intentar cambiar posición a un usuario no registrado lanza PlayerNotTeamMemberError."""
        team_a, _, _ = seed_teams
        with pytest.raises(PlayerNotTeamMemberError) as exc_info:
            await roster_sync_service.change_player_position(
                discord_user_id="999999999999",
                team_id=team_a.id,
                new_role=RosterRole.TOP,
                actor_id=seed_actor.discord_id,
            )
        assert exc_info.value.discord_user_id == "999999999999"
        assert exc_info.value.team_id == team_a.id

    @pytest.mark.asyncio
    async def test_string_role_coercion_supports_valid_inputs(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """change_player_position debe aceptar strings para new_role ('top', 'coach')."""
        team_a, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="888111222", username="string_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_a.id, user.discord_id, RosterRole.STAFF)
            await session.commit()

        # String 'top'
        updated = await roster_sync_service.change_player_position(
            discord_user_id="888111222",
            team_id=team_a.id,
            new_role="top",
            actor_id=seed_actor.discord_id,
        )
        assert updated.role == RosterRole.TOP

        # String 'coach'
        updated2 = await roster_sync_service.change_player_position(
            discord_user_id="888111222",
            team_id=team_a.id,
            new_role="coach",
            actor_id=seed_actor.discord_id,
        )
        assert updated2.role == RosterRole.COACH

    @pytest.mark.asyncio
    async def test_invalid_uuid_or_user_id_raises_value_error(
        self,
        roster_sync_service: RosterSyncService,
        seed_actor: DiscordUser,
    ):
        """Argumentos no parseables deben levantar ValueError temprano."""
        with pytest.raises(ValueError, match="Identificador de equipo inválido"):
            await roster_sync_service.change_player_position(
                discord_user_id="123456",
                team_id="invalid-uuid-string",
                new_role=RosterRole.TOP,
                actor_id=seed_actor.discord_id,
            )

        with pytest.raises(ValueError, match="discord_user_id no puede ser nulo"):
            await roster_sync_service.change_player_position(
                discord_user_id="",
                team_id=uuid.uuid4(),
                new_role=RosterRole.TOP,
                actor_id=seed_actor.discord_id,
            )

        with pytest.raises(ValueError, match="actor_id no puede ser nulo"):
            await roster_sync_service.change_player_position(
                discord_user_id="123456",
                team_id=uuid.uuid4(),
                new_role=RosterRole.TOP,
                actor_id="",
            )

    @pytest.mark.asyncio
    async def test_demote_captain_records_demoted_from_captain_action(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Remover capitanía manteniendo la misma posición titular genera DEMOTED_FROM_CAPTAIN."""
        team_a, _, _ = seed_teams
        user_id = "555666777"
        async with session_factory() as session:
            user = DiscordUser(discord_id=user_id, username="active_captain")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_a.id, user.discord_id, RosterRole.TOP, is_captain=True)
            await session.commit()

        updated = await roster_sync_service.change_player_position(
            discord_user_id=user_id,
            team_id=team_a.id,
            new_role=RosterRole.TOP,
            actor_id=seed_actor.discord_id,
            is_captain=False,
        )

        assert updated.is_captain is False
        assert updated.role == RosterRole.TOP

        async with session_factory() as session:
            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_team(team_a.id)
            assert len(movs) == 1
            assert movs[0].action == RosterMovementAction.DEMOTED_FROM_CAPTAIN
            assert movs[0].role == RosterRole.TOP
            assert movs[0].actor_id == seed_actor.discord_id

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == 1
            assert audits[0].before["is_captain"] is True
            assert audits[0].after["is_captain"] is False

    @pytest.mark.asyncio
    async def test_role_change_while_remaining_captain_records_role_changed_action(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Cambiar de TOP a MID manteniendo is_captain=True genera ROLE_CHANGED
        y conserva la capitanía en el registro.
        """
        team_a, _, _ = seed_teams
        user_id = "444333222"
        async with session_factory() as session:
            user = DiscordUser(discord_id=user_id, username="versatile_captain")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_a.id, user.discord_id, RosterRole.TOP, is_captain=True)
            await session.commit()

        updated = await roster_sync_service.change_player_position(
            discord_user_id=user_id,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=seed_actor.discord_id,
            is_captain=True,
        )

        assert updated.role == RosterRole.MID
        assert updated.is_captain is True

        async with session_factory() as session:
            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_team(team_a.id)
            assert len(movs) == 1
            assert movs[0].action == RosterMovementAction.ROLE_CHANGED
            assert movs[0].role == RosterRole.MID

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == 1
            assert audits[0].before["role"] == "top"
            assert audits[0].after["role"] == "mid"
            assert audits[0].after["is_captain"] is True
