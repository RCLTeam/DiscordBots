"""
Pruebas exhaustivas para la capa de repositorios de plantillas y auditoría:
- TeamMembershipRepository (team_memberships)
- RosterMovementRepository (roster_movements)
- AuditLogRepository (audit_logs)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import (
    AuditLog,
    DiscordUser,
    RosterMovement,
    Team,
    TeamMembership,
)
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
    _to_json_safe,
)

# ---------------------------------------------------------------------------
# Fixtures de repositorios y datos de prueba
# ---------------------------------------------------------------------------


@pytest.fixture
def team_membership_repo(session: AsyncSession) -> TeamMembershipRepository:
    """Instancia de TeamMembershipRepository vinculada a la sesión del test."""
    return TeamMembershipRepository(session)


@pytest.fixture
def roster_movement_repo(session: AsyncSession) -> RosterMovementRepository:
    """Instancia de RosterMovementRepository vinculada a la sesión del test."""
    return RosterMovementRepository(session)


@pytest.fixture
def audit_log_repo(session: AsyncSession) -> AuditLogRepository:
    """Instancia de AuditLogRepository vinculada a la sesión del test."""
    return AuditLogRepository(session)


@pytest.fixture
async def seed_team(session: AsyncSession) -> Team:
    """Crea un equipo canónico de prueba."""
    team = Team(
        name="Fnatic",
        tag="FNC",
        slug="fnatic",
        division=Division.PREMIER,
        discord_role_id=111222333444,
    )
    session.add(team)
    await session.flush()
    return team


@pytest.fixture
async def seed_teams(session: AsyncSession) -> tuple[Team, Team, Team]:
    """Crea tres equipos de prueba para verificar aislamiento entre clubes."""
    t1 = Team(
        name="Fnatic",
        tag="FNC",
        slug="fnatic",
        division=Division.PREMIER,
        discord_role_id=101,
    )
    t2 = Team(
        name="G2 Esports",
        tag="G2",
        slug="g2-esports",
        division=Division.PREMIER,
        discord_role_id=102,
    )
    t3 = Team(
        name="KOI",
        tag="KOI",
        slug="koi",
        division=Division.ASCEND,
        discord_role_id=103,
    )
    session.add_all([t1, t2, t3])
    await session.flush()
    return t1, t2, t3


@pytest.fixture
async def seed_user(session: AsyncSession) -> DiscordUser:
    """Crea un usuario estándar de Discord."""
    user = DiscordUser(
        discord_id="10001",
        username="caps_player",
        global_name="Rasmus Winther",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def seed_users(
    session: AsyncSession,
) -> tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser]:
    """Crea múltiples usuarios de Discord para membresías y roles."""
    u1 = DiscordUser(discord_id="20001", username="top_laner")
    u2 = DiscordUser(discord_id="20002", username="jungler")
    u3 = DiscordUser(discord_id="20003", username="coach_lead")
    actor = DiscordUser(discord_id="99001", username="staff_manager")
    session.add_all([u1, u2, u3, actor])
    await session.flush()
    return u1, u2, u3, actor


# ===========================================================================
# 1. Pruebas para TeamMembershipRepository
# ===========================================================================


class TestTeamMembershipRepositoryCRUD:
    """Pruebas CRUD, claves compuestas y validación de entrada en TeamMembershipRepository."""

    @pytest.mark.asyncio
    async def test_init(self, session: AsyncSession):
        """Verifica que el repositorio se inicialice con el modelo TeamMembership."""
        repo = TeamMembershipRepository(session)
        assert repo.session is session
        assert repo.model_cls is TeamMembership

    @pytest.mark.asyncio
    async def test_create_and_get_by_composite_pk(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica creación y recuperación exacta por clave compuesta (team_id, user_id)."""
        created = await team_membership_repo.create(
            team_id=seed_team.id,
            discord_user_id=seed_user.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )
        assert created.team_id == seed_team.id
        assert created.discord_user_id == seed_user.discord_id
        assert created.role == RosterRole.MID
        assert created.is_captain is False
        assert created.created_at is not None

        fetched = await team_membership_repo.get(
            team_id=seed_team.id,
            discord_user_id=seed_user.discord_id,
        )
        assert fetched is not None
        assert fetched.team_id == seed_team.id
        assert fetched.discord_user_id == seed_user.discord_id
        assert fetched.role == RosterRole.MID

    @pytest.mark.asyncio
    async def test_create_with_string_arguments(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica creación convirtiendo strings válidos a UUID y RosterRole."""
        created = await team_membership_repo.create(
            team_id=str(seed_team.id),
            discord_user_id=int(seed_user.discord_id),
            role="mid",
            is_captain=False,
        )
        assert created.team_id == seed_team.id
        assert created.discord_user_id == seed_user.discord_id
        assert created.role == RosterRole.MID

    @pytest.mark.asyncio
    async def test_create_invalid_team_id_raises_value_error(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_user: DiscordUser,
    ):
        """Verifica que un identificador de equipo malformado lance ValueError."""
        with pytest.raises(ValueError, match="Identificador de equipo inválido"):
            await team_membership_repo.create(
                team_id="invalid-uuid-format",
                discord_user_id=seed_user.discord_id,
                role=RosterRole.MID,
            )

    @pytest.mark.asyncio
    async def test_create_empty_user_id_raises_value_error(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
    ):
        """Verifica que un discord_user_id vacío lance ValueError."""
        with pytest.raises(ValueError, match="discord_user_id no puede ser nulo"):
            await team_membership_repo.create(
                team_id=seed_team.id,
                discord_user_id="   ",
                role=RosterRole.MID,
            )

    @pytest.mark.asyncio
    async def test_create_with_captain_flag(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica creación exitosa de un titular como capitán."""
        created = await team_membership_repo.create(
            team_id=seed_team.id,
            discord_user_id=seed_user.discord_id,
            role=RosterRole.TOP,
            is_captain=True,
        )
        assert created.is_captain is True

        fetched = await team_membership_repo.get(seed_team.id, seed_user.discord_id)
        assert fetched is not None
        assert fetched.is_captain is True

    @pytest.mark.asyncio
    async def test_create_duplicate_composite_pk_raises_integrity_error(
        self,
        session: AsyncSession,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que la clave primaria compuesta impida duplicados en el mismo equipo."""
        await team_membership_repo.create(
            team_id=seed_team.id,
            discord_user_id=seed_user.discord_id,
            role=RosterRole.TOP,
        )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await team_membership_repo.create(
                    team_id=seed_team.id,
                    discord_user_id=seed_user.discord_id,
                    role=RosterRole.MID,
                )

    @pytest.mark.asyncio
    async def test_captain_check_constraint_rejected_for_substitute(
        self,
        session: AsyncSession,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que un substitute no pueda ser capitán (CheckConstraint)."""
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin_nested():
                await team_membership_repo.create(
                    team_id=seed_team.id,
                    discord_user_id=seed_user.discord_id,
                    role=RosterRole.SUBSTITUTE,
                    is_captain=True,
                )

    @pytest.mark.parametrize(
        "non_competitive_role",
        [RosterRole.COACH, RosterRole.STAFF, RosterRole.PARTNERS],
    )
    @pytest.mark.asyncio
    async def test_captain_check_constraint_rejected_for_non_competitive_roles(
        self,
        session: AsyncSession,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
        non_competitive_role: RosterRole,
    ):
        """Verifica que roles no competitivos no puedan ser capitanes."""
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin_nested():
                await team_membership_repo.create(
                    team_id=seed_team.id,
                    discord_user_id=seed_user.discord_id,
                    role=non_competitive_role,
                    is_captain=True,
                )

    @pytest.mark.asyncio
    async def test_partial_unique_index_single_captain_per_team(
        self,
        session: AsyncSession,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_users: tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser],
    ):
        """Verifica que el índice único parcial impida más de un capitán en un equipo."""
        u1, u2, _, _ = seed_users
        await team_membership_repo.create(
            team_id=seed_team.id,
            discord_user_id=u1.discord_id,
            role=RosterRole.TOP,
            is_captain=True,
        )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await team_membership_repo.create(
                    team_id=seed_team.id,
                    discord_user_id=u2.discord_id,
                    role=RosterRole.MID,
                    is_captain=True,
                )

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que buscar combinaciones inexistentes retorne None sin error."""
        assert await team_membership_repo.get(uuid.uuid4(), seed_user.discord_id) is None
        assert await team_membership_repo.get(seed_team.id, "nonexistent_user") is None
        assert await team_membership_repo.get(uuid.uuid4(), "nonexistent_user") is None

    @pytest.mark.asyncio
    async def test_get_invalid_uuid_string_returns_none_safely(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_user: DiscordUser,
    ):
        """Verifica que un UUID inválido retorne None sin abortar la transacción."""
        res = await team_membership_repo.get(
            team_id="not-a-valid-uuid",
            discord_user_id=seed_user.discord_id,
        )
        assert res is None

    @pytest.mark.asyncio
    async def test_get_empty_user_id_returns_none_safely(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
    ):
        """Verifica que un usuario vacío retorne None sin abortar la transacción."""
        res = await team_membership_repo.get(
            team_id=seed_team.id,
            discord_user_id="   ",
        )
        assert res is None


class TestTeamMembershipCompetitiveInvariant:
    """Pruebas del invariante de posición competitiva única (Domain Invariant #6)."""

    @pytest.mark.asyncio
    async def test_get_competitive_membership_none_when_empty(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_user: DiscordUser,
    ):
        """Verifica que un usuario sin membresías retorne None."""
        res = await team_membership_repo.get_competitive_membership(seed_user.discord_id)
        assert res is None

    @pytest.mark.parametrize(
        "role",
        [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
            RosterRole.SUBSTITUTE,
        ],
    )
    @pytest.mark.asyncio
    async def test_get_competitive_membership_returns_for_all_competitive_roles(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
        role: RosterRole,
    ):
        """Verifica que cada uno de los 6 roles competitivos sea detectado."""
        await team_membership_repo.create(
            team_id=seed_team.id,
            discord_user_id=seed_user.discord_id,
            role=role,
        )
        membership = await team_membership_repo.get_competitive_membership(seed_user.discord_id)
        assert membership is not None
        assert membership.team_id == seed_team.id
        assert membership.role == role

    @pytest.mark.asyncio
    async def test_get_competitive_membership_none_when_only_non_competitive_roles(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_teams: tuple[Team, Team, Team],
        seed_user: DiscordUser,
    ):
        """Verifica que roles no competitivos no disparen un falso positivo."""
        t1, t2, t3 = seed_teams
        await team_membership_repo.create(t1.id, seed_user.discord_id, RosterRole.COACH)
        await team_membership_repo.create(t2.id, seed_user.discord_id, RosterRole.STAFF)
        await team_membership_repo.create(t3.id, seed_user.discord_id, RosterRole.PARTNERS)

        res = await team_membership_repo.get_competitive_membership(seed_user.discord_id)
        assert res is None

    @pytest.mark.asyncio
    async def test_get_competitive_membership_mixed_roles_returns_competitive_one(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_teams: tuple[Team, Team, Team],
        seed_user: DiscordUser,
    ):
        """Verifica que entre roles mixtos se recupere la única membresía competitiva."""
        t1, t2, t3 = seed_teams
        await team_membership_repo.create(t1.id, seed_user.discord_id, RosterRole.COACH)
        await team_membership_repo.create(t2.id, seed_user.discord_id, RosterRole.MID)
        await team_membership_repo.create(t3.id, seed_user.discord_id, RosterRole.STAFF)

        res = await team_membership_repo.get_competitive_membership(seed_user.discord_id)
        assert res is not None
        assert res.team_id == t2.id
        assert res.role == RosterRole.MID

    @pytest.mark.asyncio
    async def test_get_competitive_membership_after_role_updated(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que al cambiar a coach, ya no se detecte membresía competitiva."""
        await team_membership_repo.create(seed_team.id, seed_user.discord_id, RosterRole.ADC)
        assert (
            await team_membership_repo.get_competitive_membership(seed_user.discord_id) is not None
        )

        await team_membership_repo.update_role(
            seed_team.id, seed_user.discord_id, new_role=RosterRole.COACH
        )
        assert await team_membership_repo.get_competitive_membership(seed_user.discord_id) is None

    @pytest.mark.asyncio
    async def test_get_competitive_membership_after_deletion(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que al eliminar la membresía competitiva retorne None."""
        await team_membership_repo.create(seed_team.id, seed_user.discord_id, RosterRole.TOP)
        deleted = await team_membership_repo.delete(seed_team.id, seed_user.discord_id)
        assert deleted is True

        res = await team_membership_repo.get_competitive_membership(seed_user.discord_id)
        assert res is None


class TestTeamMembershipListAndMutations:
    """Pruebas para list_by_user, list_by_team, update_role, set_captain y delete."""

    @pytest.mark.asyncio
    async def test_list_by_user_empty_and_multiple(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_teams: tuple[Team, Team, Team],
        seed_user: DiscordUser,
    ):
        """Verifica list_by_user para un usuario sin equipos y con múltiples equipos."""
        assert await team_membership_repo.list_by_user(seed_user.discord_id) == []

        t1, t2, t3 = seed_teams
        await team_membership_repo.create(t1.id, seed_user.discord_id, RosterRole.COACH)
        await team_membership_repo.create(t2.id, seed_user.discord_id, RosterRole.MID)
        await team_membership_repo.create(t3.id, seed_user.discord_id, RosterRole.STAFF)

        memberships = await team_membership_repo.list_by_user(seed_user.discord_id)
        assert len(memberships) == 3
        team_ids = {m.team_id for m in memberships}
        assert team_ids == {t1.id, t2.id, t3.id}

    @pytest.mark.asyncio
    async def test_list_by_user_invalid_input_returns_empty(
        self,
        team_membership_repo: TeamMembershipRepository,
    ):
        """Verifica que list_by_user con entrada vacía retorne lista vacía."""
        assert await team_membership_repo.list_by_user("   ") == []

    @pytest.mark.asyncio
    async def test_list_by_user_isolation(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_users: tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser],
    ):
        """Verifica que list_by_user no filtre ni devuelva membresías de otros usuarios."""
        u1, u2, _, _ = seed_users
        await team_membership_repo.create(seed_team.id, u1.discord_id, RosterRole.TOP)
        await team_membership_repo.create(seed_team.id, u2.discord_id, RosterRole.JUNGLE)

        res_u1 = await team_membership_repo.list_by_user(u1.discord_id)
        assert len(res_u1) == 1
        assert res_u1[0].discord_user_id == u1.discord_id

    @pytest.mark.asyncio
    async def test_list_by_team_empty_and_full_roster(
        self,
        session: AsyncSession,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
    ):
        """Verifica list_by_team con plantilla completa."""
        assert await team_membership_repo.list_by_team(seed_team.id) == []

        roles = [
            (RosterRole.TOP, True),
            (RosterRole.JUNGLE, False),
            (RosterRole.MID, False),
            (RosterRole.ADC, False),
            (RosterRole.SUPPORT, False),
            (RosterRole.SUBSTITUTE, False),
            (RosterRole.COACH, False),
            (RosterRole.STAFF, False),
        ]
        users = [
            DiscordUser(discord_id=f"u_full_{i}", username=f"player_full_{i}")
            for i in range(len(roles))
        ]
        session.add_all(users)
        await session.flush()

        for user, (role, is_cap) in zip(users, roles, strict=True):
            await team_membership_repo.create(
                seed_team.id, user.discord_id, role, is_captain=is_cap
            )

        members = await team_membership_repo.list_by_team(seed_team.id)
        assert len(members) == 8
        assert all(m.team_id == seed_team.id for m in members)
        # Capitán debe estar en primera posición
        assert members[0].is_captain is True

    @pytest.mark.asyncio
    async def test_list_by_team_invalid_input_returns_empty(
        self,
        team_membership_repo: TeamMembershipRepository,
    ):
        """Verifica que list_by_team con UUID inválido retorne lista vacía."""
        assert await team_membership_repo.list_by_team("invalid-uuid") == []

    @pytest.mark.asyncio
    async def test_list_by_team_isolation(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_teams: tuple[Team, Team, Team],
        seed_users: tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser],
    ):
        """Verifica aislamiento estricto entre equipos en list_by_team."""
        t1, t2, _ = seed_teams
        u1, u2, _, _ = seed_users
        await team_membership_repo.create(t1.id, u1.discord_id, RosterRole.TOP)
        await team_membership_repo.create(t2.id, u2.discord_id, RosterRole.MID)

        members_t1 = await team_membership_repo.list_by_team(t1.id)
        assert len(members_t1) == 1
        assert members_t1[0].team_id == t1.id

    @pytest.mark.asyncio
    async def test_update_role_position_and_captaincy(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica actualización de rol y ascenso a capitán."""
        await team_membership_repo.create(
            seed_team.id,
            seed_user.discord_id,
            role=RosterRole.SUBSTITUTE,
            is_captain=False,
        )

        updated = await team_membership_repo.update_role(
            seed_team.id,
            seed_user.discord_id,
            new_role=RosterRole.MID,
            is_captain=True,
        )
        assert updated.role == RosterRole.MID
        assert updated.is_captain is True

        fetched = await team_membership_repo.get(seed_team.id, seed_user.discord_id)
        assert fetched is not None
        assert fetched.role == RosterRole.MID
        assert fetched.is_captain is True

    @pytest.mark.asyncio
    async def test_update_role_demote_captain(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica degradación de capitán preservando el rol de juego."""
        await team_membership_repo.create(
            seed_team.id,
            seed_user.discord_id,
            role=RosterRole.TOP,
            is_captain=True,
        )

        updated = await team_membership_repo.update_role(
            seed_team.id,
            seed_user.discord_id,
            new_role=RosterRole.TOP,
            is_captain=False,
        )
        assert updated.is_captain is False

    @pytest.mark.asyncio
    async def test_set_captain_helper(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica helper set_captain para cambiar estado de capitán."""
        await team_membership_repo.create(
            seed_team.id,
            seed_user.discord_id,
            role=RosterRole.TOP,
            is_captain=False,
        )

        promoted = await team_membership_repo.set_captain(
            seed_team.id, seed_user.discord_id, is_captain=True
        )
        assert promoted.is_captain is True

        demoted = await team_membership_repo.set_captain(
            seed_team.id, seed_user.discord_id, is_captain=False
        )
        assert demoted.is_captain is False

    @pytest.mark.asyncio
    async def test_set_captain_nonexistent_raises_value_error(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
    ):
        """Verifica que set_captain en membresía inexistente lance ValueError."""
        with pytest.raises(ValueError):
            await team_membership_repo.set_captain(seed_team.id, "unknown_user", is_captain=True)

    @pytest.mark.asyncio
    async def test_update_role_nonexistent_raises_value_error(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
    ):
        """Verifica que actualizar una membresía inexistente lance ValueError."""
        with pytest.raises(ValueError):
            await team_membership_repo.update_role(
                seed_team.id,
                "unknown_user",
                new_role=RosterRole.MID,
            )

    @pytest.mark.asyncio
    async def test_update_role_violates_captain_constraint(
        self,
        session: AsyncSession,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que no se pueda actualizar rol a substitute con is_captain=True."""
        await team_membership_repo.create(
            seed_team.id,
            seed_user.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin_nested():
                await team_membership_repo.update_role(
                    seed_team.id,
                    seed_user.discord_id,
                    new_role=RosterRole.SUBSTITUTE,
                    is_captain=True,
                )

    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica eliminación de membresía existente."""
        await team_membership_repo.create(seed_team.id, seed_user.discord_id, RosterRole.SUPPORT)
        res = await team_membership_repo.delete(seed_team.id, seed_user.discord_id)
        assert res is True
        assert await team_membership_repo.get(seed_team.id, seed_user.discord_id) is None

    @pytest.mark.asyncio
    async def test_delete_by_entity_instance(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica eliminación pasando la entidad TeamMembership directamente."""
        created = await team_membership_repo.create(
            seed_team.id, seed_user.discord_id, RosterRole.SUPPORT
        )
        res = await team_membership_repo.delete(created)
        assert res is True
        assert await team_membership_repo.get(seed_team.id, seed_user.discord_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(
        self,
        team_membership_repo: TeamMembershipRepository,
    ):
        """Verifica que eliminar una entidad inexistente retorne False de forma idempotente."""
        res = await team_membership_repo.delete(uuid.uuid4(), "ghost_user")
        assert res is False

    @pytest.mark.asyncio
    async def test_delete_preserves_other_team_memberships(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_teams: tuple[Team, Team, Team],
        seed_user: DiscordUser,
    ):
        """Verifica que eliminar la membresía de un equipo no afecte a otros clubes."""
        t1, t2, _ = seed_teams
        await team_membership_repo.create(t1.id, seed_user.discord_id, RosterRole.COACH)
        await team_membership_repo.create(t2.id, seed_user.discord_id, RosterRole.MID)

        deleted = await team_membership_repo.delete(t1.id, seed_user.discord_id)
        assert deleted is True

        assert await team_membership_repo.get(t1.id, seed_user.discord_id) is None
        assert await team_membership_repo.get(t2.id, seed_user.discord_id) is not None

    @pytest.mark.asyncio
    async def test_eager_loading_relationships(
        self,
        team_membership_repo: TeamMembershipRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que with_team y with_user carguen las relaciones sin MissingGreenlet."""
        await team_membership_repo.create(seed_team.id, seed_user.discord_id, RosterRole.MID)
        membership = await team_membership_repo.get(
            seed_team.id,
            seed_user.discord_id,
            with_team=True,
            with_user=True,
        )
        assert membership is not None
        assert membership.team.name == "Fnatic"
        assert membership.discord_user.username == "caps_player"

        user_list = await team_membership_repo.list_by_user(seed_user.discord_id, with_team=True)
        assert len(user_list) == 1
        assert user_list[0].team.name == "Fnatic"

        team_list = await team_membership_repo.list_by_team(seed_team.id, with_user=True)
        assert len(team_list) == 1
        assert team_list[0].discord_user.username == "caps_player"

        comp = await team_membership_repo.get_competitive_membership(
            seed_user.discord_id, with_team=True, with_user=True
        )
        assert comp is not None
        assert comp.team.name == "Fnatic"
        assert comp.discord_user.username == "caps_player"


# ===========================================================================
# 2. Pruebas para RosterMovementRepository
# ===========================================================================


class TestRosterMovementRepository:
    """Pruebas para el registro histórico de movimientos de plantilla."""

    @pytest.mark.asyncio
    async def test_init(self, session: AsyncSession):
        """Verifica que el repositorio se inicialice con el modelo RosterMovement."""
        repo = RosterMovementRepository(session)
        assert repo.session is session
        assert repo.model_cls is RosterMovement

    @pytest.mark.parametrize(
        ("action", "role"),
        [
            (RosterMovementAction.JOINED, RosterRole.MID),
            (RosterMovementAction.LEFT, RosterRole.MID),
            (RosterMovementAction.PROMOTED_TO_CAPTAIN, RosterRole.MID),
            (RosterMovementAction.DEMOTED_FROM_CAPTAIN, RosterRole.MID),
            (RosterMovementAction.ROLE_CHANGED, RosterRole.TOP),
        ],
    )
    @pytest.mark.asyncio
    async def test_record_movement_all_actions(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_users: tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser],
        action: RosterMovementAction,
        role: RosterRole,
    ):
        """Verifica persistencia de cada una de las 5 acciones de movimiento."""
        target_user, _, _, actor = seed_users
        movement = await roster_movement_repo.record_movement(
            team_id=seed_team.id,
            discord_user_id=target_user.discord_id,
            action=action,
            role=role,
            actor_id=actor.discord_id,
        )
        assert isinstance(movement.id, uuid.UUID)
        assert movement.team_id == seed_team.id
        assert movement.discord_user_id == target_user.discord_id
        assert movement.action == action
        assert movement.role == role
        assert movement.actor_id == actor.discord_id
        assert movement.created_at is not None

    @pytest.mark.asyncio
    async def test_record_movement_with_string_arguments(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica coerción de tipos desde cadenas de texto."""
        movement = await roster_movement_repo.record_movement(
            team_id=str(seed_team.id),
            discord_user_id=int(seed_user.discord_id),
            action="joined",
            role="top",
            actor_id=None,
        )
        assert movement.team_id == seed_team.id
        assert movement.discord_user_id == seed_user.discord_id
        assert movement.action == RosterMovementAction.JOINED
        assert movement.role == RosterRole.TOP

    @pytest.mark.asyncio
    async def test_record_movement_invalid_inputs_raise(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que entradas malformadas lancen ValueError."""
        with pytest.raises(ValueError, match="Identificador de equipo inválido"):
            await roster_movement_repo.record_movement(
                team_id="not-a-uuid",
                discord_user_id=seed_user.discord_id,
                action=RosterMovementAction.JOINED,
            )

        with pytest.raises(ValueError, match="discord_user_id no puede ser nulo"):
            await roster_movement_repo.record_movement(
                team_id=seed_team.id,
                discord_user_id="   ",
                action=RosterMovementAction.JOINED,
            )

    @pytest.mark.asyncio
    async def test_record_movement_nullable_actor_and_role(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica persistencia con actor_id y role nulos para eventos automáticos o genéricos."""
        movement = await roster_movement_repo.record_movement(
            team_id=seed_team.id,
            discord_user_id=seed_user.discord_id,
            action=RosterMovementAction.LEFT,
            role=None,
            actor_id=None,
        )
        assert movement.actor_id is None
        assert movement.role is None

    @pytest.mark.asyncio
    async def test_record_movement_foreign_key_violation(
        self,
        session: AsyncSession,
        roster_movement_repo: RosterMovementRepository,
        seed_user: DiscordUser,
    ):
        """Verifica que un team_id inexistente dispare violación de clave foránea."""
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await roster_movement_repo.record_movement(
                    team_id=uuid.uuid4(),
                    discord_user_id=seed_user.discord_id,
                    action=RosterMovementAction.JOINED,
                )

    @pytest.mark.asyncio
    async def test_list_by_team_empty_and_populated_ordering(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_users: tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser],
    ):
        """Verifica list_by_team ordenado de forma cronológica descendente."""
        assert await roster_movement_repo.list_by_team(seed_team.id) == []

        u1, u2, _, actor = seed_users
        m1 = await roster_movement_repo.record_movement(
            seed_team.id,
            u1.discord_id,
            RosterMovementAction.JOINED,
            RosterRole.TOP,
            created_at=datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
        )
        m2 = await roster_movement_repo.record_movement(
            seed_team.id,
            u2.discord_id,
            RosterMovementAction.JOINED,
            RosterRole.MID,
            created_at=datetime(2026, 9, 19, 11, 0, 0, tzinfo=timezone.utc),
        )
        m3 = await roster_movement_repo.record_movement(
            seed_team.id,
            u1.discord_id,
            RosterMovementAction.PROMOTED_TO_CAPTAIN,
            actor_id=actor.discord_id,
            created_at=datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc),
        )

        history = await roster_movement_repo.list_by_team(seed_team.id)
        assert len(history) == 3
        # Orden descendente: el más reciente primero
        assert history[0].id == m3.id
        assert history[1].id == m2.id
        assert history[2].id == m1.id

    @pytest.mark.asyncio
    async def test_list_by_team_with_limit(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica limitación de resultados en list_by_team."""
        for _ in range(5):
            await roster_movement_repo.record_movement(
                seed_team.id, seed_user.discord_id, RosterMovementAction.JOINED
            )

        limited = await roster_movement_repo.list_by_team(seed_team.id, limit=2)
        assert len(limited) == 2

    @pytest.mark.asyncio
    async def test_list_by_team_invalid_uuid_safe(
        self,
        roster_movement_repo: RosterMovementRepository,
    ):
        """Verifica que list_by_team con UUID inválido retorne lista vacía sin error."""
        assert await roster_movement_repo.list_by_team("not-a-uuid") == []

    @pytest.mark.asyncio
    async def test_list_by_team_isolation(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_teams: tuple[Team, Team, Team],
        seed_user: DiscordUser,
    ):
        """Verifica que list_by_team solo retorne movimientos del equipo indicado."""
        t1, t2, _ = seed_teams
        await roster_movement_repo.record_movement(
            t1.id, seed_user.discord_id, RosterMovementAction.JOINED
        )
        await roster_movement_repo.record_movement(
            t2.id, seed_user.discord_id, RosterMovementAction.JOINED
        )

        res_t1 = await roster_movement_repo.list_by_team(t1.id)
        assert len(res_t1) == 1
        assert res_t1[0].team_id == t1.id

    @pytest.mark.asyncio
    async def test_list_by_user_empty_and_populated_ordering(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_teams: tuple[Team, Team, Team],
        seed_user: DiscordUser,
    ):
        """Verifica list_by_user para historial multiequipo del usuario."""
        assert await roster_movement_repo.list_by_user(seed_user.discord_id) == []

        t1, t2, _ = seed_teams
        m1 = await roster_movement_repo.record_movement(
            t1.id,
            seed_user.discord_id,
            RosterMovementAction.JOINED,
            RosterRole.TOP,
            created_at=datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
        )
        m2 = await roster_movement_repo.record_movement(
            t1.id,
            seed_user.discord_id,
            RosterMovementAction.LEFT,
            created_at=datetime(2026, 9, 19, 11, 0, 0, tzinfo=timezone.utc),
        )
        m3 = await roster_movement_repo.record_movement(
            t2.id,
            seed_user.discord_id,
            RosterMovementAction.JOINED,
            RosterRole.MID,
            created_at=datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc),
        )

        user_history = await roster_movement_repo.list_by_user(seed_user.discord_id)
        assert len(user_history) == 3
        assert [m.id for m in user_history] == [m3.id, m2.id, m1.id]

    @pytest.mark.asyncio
    async def test_list_by_user_with_limit_and_empty_id(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica límite de resultados y control de ID vacío en list_by_user."""
        for _ in range(4):
            await roster_movement_repo.record_movement(
                seed_team.id, seed_user.discord_id, RosterMovementAction.JOINED
            )

        limited = await roster_movement_repo.list_by_user(seed_user.discord_id, limit=2)
        assert len(limited) == 2

        assert await roster_movement_repo.list_by_user("   ") == []

    @pytest.mark.asyncio
    async def test_list_by_user_isolation(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_users: tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser],
    ):
        """Verifica que list_by_user no filtre movimientos de otros usuarios."""
        u1, u2, _, _ = seed_users
        await roster_movement_repo.record_movement(
            seed_team.id, u1.discord_id, RosterMovementAction.JOINED
        )
        await roster_movement_repo.record_movement(
            seed_team.id, u2.discord_id, RosterMovementAction.JOINED
        )

        res = await roster_movement_repo.list_by_user(u1.discord_id)
        assert len(res) == 1
        assert res[0].discord_user_id == u1.discord_id

    @pytest.mark.asyncio
    async def test_roster_movement_base_crud(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica métodos base heredados (get_by_id, count, list_all, delete_by_id)."""
        initial_count = await roster_movement_repo.count()

        movement = await roster_movement_repo.record_movement(
            seed_team.id, seed_user.discord_id, RosterMovementAction.JOINED
        )
        assert await roster_movement_repo.count() == initial_count + 1

        found = await roster_movement_repo.get_by_id(movement.id)
        assert found is not None
        assert found.id == movement.id

        all_items = await roster_movement_repo.list_all()
        assert any(item.id == movement.id for item in all_items)

        deleted = await roster_movement_repo.delete_by_id(movement.id)
        assert deleted is True
        assert await roster_movement_repo.get_by_id(movement.id) is None


# ===========================================================================
# 3. Pruebas para AuditLogRepository
# ===========================================================================


class TestAuditLogRepository:
    """Pruebas para registro de auditoría y serialización JSONB."""

    @pytest.mark.asyncio
    async def test_init(self, session: AsyncSession):
        """Verifica que el repositorio se inicialice con el modelo AuditLog."""
        repo = AuditLogRepository(session)
        assert repo.session is session
        assert repo.model_cls is AuditLog

    @pytest.mark.asyncio
    async def test_to_json_safe_helper(self):
        """Verifica que _to_json_safe normalice tipos complejos a tipos compatibles con JSON."""
        test_uuid = uuid.uuid4()
        test_dt = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
        test_date = date(2026, 9, 19)

        payload = {
            "uuid": test_uuid,
            "datetime": test_dt,
            "date": test_date,
            "role": RosterRole.TOP,
            "action": RosterMovementAction.JOINED,
            "decimal": Decimal("42.50"),
            "tuple_vals": (1, "dos", test_uuid),
            "set_vals": {1, 2},
            "nested": {
                "inner_uuid": test_uuid,
            },
        }

        safe = _to_json_safe(payload)
        assert safe["uuid"] == str(test_uuid)
        assert safe["datetime"] == test_dt.isoformat()
        assert safe["date"] == test_date.isoformat()
        assert safe["role"] == "top"
        assert safe["action"] == "joined"
        assert safe["decimal"] == 42.5
        assert safe["tuple_vals"] == [1, "dos", str(test_uuid)]
        assert sorted(safe["set_vals"]) == [1, 2]
        assert safe["nested"]["inner_uuid"] == str(test_uuid)

    @pytest.mark.asyncio
    async def test_log_full_payload(
        self,
        audit_log_repo: AuditLogRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica creación de registro de auditoría con carga completa."""
        before = {"role": "substitute", "is_captain": False}
        after = {"role": "top", "is_captain": True}
        entry = await audit_log_repo.log(
            actor_discord_user_id=seed_user.discord_id,
            action="roster.role_changed",
            entity_type="team_membership",
            entity_id=seed_team.id,
            before=before,
            after=after,
        )
        assert isinstance(entry.id, uuid.UUID)
        assert entry.actor_discord_user_id == seed_user.discord_id
        assert entry.action == "roster.role_changed"
        assert entry.entity_type == "team_membership"
        assert entry.entity_id == seed_team.id
        assert entry.before == before
        assert entry.after == after
        assert entry.created_at is not None

    @pytest.mark.asyncio
    async def test_log_serialization_safety_with_complex_types(
        self,
        audit_log_repo: AuditLogRepository,
        seed_team: Team,
        seed_user: DiscordUser,
    ):
        """Verifica que pasar UUIDs, datetimes y Enums en before/after no lance TypeError."""
        before = {
            "team_id": seed_team.id,
            "role": RosterRole.SUBSTITUTE,
            "timestamp": datetime.now(timezone.utc),
            "flags": {"starter", "captain"},
        }
        after = {
            "team_id": seed_team.id,
            "role": RosterRole.TOP,
            "score": Decimal("99.9"),
        }

        entry = await audit_log_repo.log(
            actor_discord_user_id=seed_user.discord_id,
            action="roster.role_changed",
            entity_type="team_membership",
            entity_id=str(seed_team.id),
            before=before,
            after=after,
        )

        fetched = await audit_log_repo.get_by_id(entry.id)
        assert fetched is not None
        assert fetched.before["team_id"] == str(seed_team.id)
        assert fetched.before["role"] == "substitute"
        assert sorted(fetched.before["flags"]) == ["captain", "starter"]
        assert fetched.after["score"] == 99.9

    @pytest.mark.asyncio
    async def test_log_invalid_entity_id_raises_value_error(
        self,
        audit_log_repo: AuditLogRepository,
    ):
        """Verifica que un entity_id inválido lance ValueError."""
        with pytest.raises(ValueError, match="entity_id must be a valid UUID"):
            await audit_log_repo.log(
                actor_discord_user_id=None,
                action="test.action",
                entity_type="team",
                entity_id="not-a-valid-uuid",
            )

    @pytest.mark.asyncio
    async def test_log_deep_jsonb_structures(
        self,
        audit_log_repo: AuditLogRepository,
        seed_team: Team,
    ):
        """Verifica integridad de estructuras complejas y anidadas en JSONB."""
        complex_before: dict[str, Any] = {
            "version": 1,
            "flags": ["active", "starter"],
            "stats": {"games": 12, "winrate": 0.75},
            "nullable_field": None,
            "tag": "España 🇪🇸",
        }
        complex_after: dict[str, Any] = {
            "version": 2,
            "flags": ["benched"],
            "stats": {"games": 13, "winrate": 0.69},
            "nullable_field": "restored",
            "tag": "España 🇪🇸",
        }
        entry = await audit_log_repo.log(
            actor_discord_user_id=None,
            action="team.stats_synced",
            entity_type="team",
            entity_id=seed_team.id,
            before=complex_before,
            after=complex_after,
        )

        fetched = await audit_log_repo.get_by_id(entry.id)
        assert fetched is not None
        assert fetched.before == complex_before
        assert fetched.after == complex_after
        assert fetched.before["flags"] == ["active", "starter"]
        assert fetched.after["stats"]["winrate"] == 0.69

    @pytest.mark.asyncio
    async def test_log_nullable_fields(
        self,
        audit_log_repo: AuditLogRepository,
    ):
        """Verifica que campos nulos se almacenen correctamente sin error."""
        entry = await audit_log_repo.log(
            actor_discord_user_id=None,
            action="system.heartbeat",
            entity_type="system",
            entity_id=None,
            before=None,
            after=None,
        )
        assert entry.actor_discord_user_id is None
        assert entry.entity_id is None
        assert entry.before is None
        assert entry.after is None

    @pytest.mark.asyncio
    async def test_log_fk_actor_constraint(
        self,
        session: AsyncSession,
        audit_log_repo: AuditLogRepository,
    ):
        """Verifica que un actor inexistente dispare violación de clave foránea."""
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await audit_log_repo.log(
                    actor_discord_user_id="nonexistent_actor_id",
                    action="test.action",
                    entity_type="general",
                    entity_id=uuid.uuid4(),
                )

    @pytest.mark.asyncio
    async def test_get_by_id_found_and_none(
        self,
        audit_log_repo: AuditLogRepository,
    ):
        """Verifica recuperación puntual por identificador primario UUID."""
        entry = await audit_log_repo.log(
            actor_discord_user_id=None,
            action="test.point_query",
            entity_type="point",
            entity_id=None,
        )
        found = await audit_log_repo.get_by_id(entry.id)
        assert found is not None
        assert found.id == entry.id

        not_found = await audit_log_repo.get_by_id(uuid.uuid4())
        assert not_found is None

    @pytest.mark.asyncio
    async def test_list_by_entity_filtering_and_ordering(
        self,
        audit_log_repo: AuditLogRepository,
    ):
        """Verifica que list_by_entity filtre conjuntamente por entity_type y entity_id."""
        target_id = uuid.uuid4()
        other_id = uuid.uuid4()

        e1 = await audit_log_repo.log(
            None,
            "action.1",
            "target_type",
            target_id,
            before={"step": 1},
            created_at=datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
        )
        e2 = await audit_log_repo.log(
            None,
            "action.2",
            "target_type",
            target_id,
            before={"step": 2},
            created_at=datetime(2026, 9, 19, 11, 0, 0, tzinfo=timezone.utc),
        )
        await audit_log_repo.log(None, "action.3", "other_type", target_id)
        await audit_log_repo.log(None, "action.4", "target_type", other_id)

        results = await audit_log_repo.list_by_entity("target_type", target_id)
        assert len(results) == 2
        # Orden descendente
        assert results[0].id == e2.id
        assert results[1].id == e1.id

    @pytest.mark.asyncio
    async def test_list_by_entity_with_limit_and_invalid_uuid(
        self,
        audit_log_repo: AuditLogRepository,
    ):
        """Verifica límite de resultados y validación segura de UUID en list_by_entity."""
        target_id = uuid.uuid4()
        for i in range(5):
            await audit_log_repo.log(None, f"action.{i}", "demo_entity", target_id)

        limited = await audit_log_repo.list_by_entity("demo_entity", target_id, limit=2)
        assert len(limited) == 2

        assert await audit_log_repo.list_by_entity("demo_entity", "not-a-valid-uuid") == []

    @pytest.mark.asyncio
    async def test_list_by_actor_filtering_and_ordering(
        self,
        audit_log_repo: AuditLogRepository,
        seed_users: tuple[DiscordUser, DiscordUser, DiscordUser, DiscordUser],
    ):
        """Verifica que list_by_actor filtre estrictamente por actor y ordene descendentemente."""
        _, _, _, actor = seed_users
        other_user = seed_users[0]

        a1 = await audit_log_repo.log(
            actor.discord_id,
            "act.1",
            "demo",
            None,
            created_at=datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
        )
        a2 = await audit_log_repo.log(
            actor.discord_id,
            "act.2",
            "demo",
            None,
            created_at=datetime(2026, 9, 19, 11, 0, 0, tzinfo=timezone.utc),
        )
        await audit_log_repo.log(other_user.discord_id, "act.3", "demo", None)

        actor_logs = await audit_log_repo.list_by_actor(actor.discord_id)
        assert len(actor_logs) == 2
        assert actor_logs[0].id == a2.id
        assert actor_logs[1].id == a1.id

    @pytest.mark.asyncio
    async def test_list_by_actor_with_limit_and_empty_id(
        self,
        audit_log_repo: AuditLogRepository,
        seed_user: DiscordUser,
    ):
        """Verifica límite de resultados y control de actor vacío en list_by_actor."""
        for i in range(4):
            await audit_log_repo.log(seed_user.discord_id, f"act.{i}", "demo", None)

        limited = await audit_log_repo.list_by_actor(seed_user.discord_id, limit=2)
        assert len(limited) == 2

        assert await audit_log_repo.list_by_actor("   ") == []

    @pytest.mark.asyncio
    async def test_audit_log_base_crud(
        self,
        audit_log_repo: AuditLogRepository,
    ):
        """Verifica operaciones base heredadas (get_by_id, count, list_all, delete_by_id)."""
        initial_count = await audit_log_repo.count()

        entry = await audit_log_repo.log(None, "base.crud", "test_type", None)
        assert await audit_log_repo.count() == initial_count + 1

        found = await audit_log_repo.get_by_id(entry.id)
        assert found is not None
        assert found.id == entry.id

        deleted = await audit_log_repo.delete_by_id(entry.id)
        assert deleted is True
        assert await audit_log_repo.get_by_id(entry.id) is None
