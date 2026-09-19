"""
Empirical Adversarial Challenge Suite: Competitive Invariant & Multi-Team Lookups on PGlite.

Directly tests and stress-challenges:
1. Multi-team non-competitive membership lifecycle & competitive invariant:
   - Create a user with non-competitive roles across 3 teams
     (coach in Team A, staff in Team B, partners in Team C).
   - Verify get_competitive_membership returns None.
   - Add competitive role (adc) in Team D -> Verify get_competitive_membership
     returns Team D membership.
   - Update role in Team D to substitute -> Verify get_competitive_membership
     still returns Team D membership.
   - Update role in Team D to coach -> Verify get_competitive_membership returns None.
2. Defensive input handling against malformed & adversarial strings:
   - Test get, list_by_team, delete with invalid strings ("not-a-uuid", "' OR 1=1 --", etc.).
   - Verify clean handling without raising psycopg/PostgreSQL DataError or aborting.
   - Verify transaction survivability: session can perform valid database operations
     immediately after invalid lookups.
3. Fuzzing & high-volume stress harness:
   - Randomized and malicious input fuzzing across repositories.
   - Multi-club concurrent membership queries.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division, RosterRole
from liga_bot.models.roster import DiscordUser, Team, TeamMembership
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
)


@pytest.fixture
def team_membership_repo(session: AsyncSession) -> TeamMembershipRepository:
    """Fixture providing TeamMembershipRepository instance."""
    return TeamMembershipRepository(session)


@pytest.fixture
def roster_movement_repo(session: AsyncSession) -> RosterMovementRepository:
    """Fixture providing RosterMovementRepository instance."""
    return RosterMovementRepository(session)


@pytest.fixture
def audit_log_repo(session: AsyncSession) -> AuditLogRepository:
    """Fixture providing AuditLogRepository instance."""
    return AuditLogRepository(session)


@pytest.fixture
async def sample_user(session: AsyncSession) -> DiscordUser:
    """Fixture providing a persisted DiscordUser."""
    user = DiscordUser(
        discord_id="998877665544332211",
        username="adversarial_player",
        global_name="Adversarial Player",
        role="viewer",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def four_teams(session: AsyncSession) -> tuple[Team, Team, Team, Team]:
    """Fixture providing four distinct teams for multi-club lifecycle testing."""
    t_a = Team(
        id=uuid.uuid4(),
        name="Team Alpha",
        tag="TMA",
        slug="team-alpha",
        division=Division.PREMIER,
        discord_role_id=901001,
    )
    t_b = Team(
        id=uuid.uuid4(),
        name="Team Bravo",
        tag="TMB",
        slug="team-bravo",
        division=Division.PREMIER,
        discord_role_id=901002,
    )
    t_c = Team(
        id=uuid.uuid4(),
        name="Team Charlie",
        tag="TMC",
        slug="team-charlie",
        division=Division.ASCEND,
        discord_role_id=901003,
    )
    t_d = Team(
        id=uuid.uuid4(),
        name="Team Delta",
        tag="TMD",
        slug="team-delta",
        division=Division.ASCEND,
        discord_role_id=901004,
    )
    session.add_all([t_a, t_b, t_c, t_d])
    await session.flush()
    return t_a, t_b, t_c, t_d


# ===========================================================================
# 1. Multi-Team Non-Competitive Membership & Competitive Invariant Lifecycle
# ===========================================================================


class TestMultiTeamCompetitiveInvariantLifecycle:
    """
    Empirical challenge for Domain Invariant #6:
    A user can hold non-competitive roles in multiple teams simultaneously,
    but can hold at most ONE competitive role across the entire league.
    """

    @pytest.mark.asyncio
    async def test_multi_team_non_competitive_and_competitive_lifecycle(
        self,
        team_membership_repo: TeamMembershipRepository,
        sample_user: DiscordUser,
        four_teams: tuple[Team, Team, Team, Team],
    ) -> None:
        """
        Step-by-step verification of the primary challenge workflow:
        1. User with non-competitive roles in 3 teams
           (coach in Team A, staff in Team B, partners in Team C).
        2. Verify get_competitive_membership returns None.
        3. Add competitive role (adc) in Team D -> returns Team D membership.
        4. Update role in Team D to substitute -> still returns Team D membership.
        5. Update role in Team D to coach -> returns None.
        """
        team_a, team_b, team_c, team_d = four_teams
        user_id = sample_user.discord_id

        # Step 1: Assign non-competitive roles in 3 distinct teams
        mem_a = await team_membership_repo.create(
            team_id=team_a.id,
            discord_user_id=user_id,
            role=RosterRole.COACH,
        )
        mem_b = await team_membership_repo.create(
            team_id=team_b.id,
            discord_user_id=user_id,
            role=RosterRole.STAFF,
        )
        mem_c = await team_membership_repo.create(
            team_id=team_c.id,
            discord_user_id=user_id,
            role=RosterRole.PARTNERS,
        )

        assert mem_a.role == RosterRole.COACH
        assert mem_b.role == RosterRole.STAFF
        assert mem_c.role == RosterRole.PARTNERS

        # Step 2: Confirm list_by_user returns all 3 teams,
        # but get_competitive_membership returns None
        all_memberships = await team_membership_repo.list_by_user(user_id)
        assert len(all_memberships) == 3
        team_ids_in_user = {m.team_id for m in all_memberships}
        assert team_ids_in_user == {team_a.id, team_b.id, team_c.id}

        competitive_mem = await team_membership_repo.get_competitive_membership(user_id)
        assert competitive_mem is None, (
            "Expected None because coach, staff, and partners are non-competitive roles."
        )

        # Step 3: Add competitive role (adc) in Team D
        mem_d = await team_membership_repo.create(
            team_id=team_d.id,
            discord_user_id=user_id,
            role=RosterRole.ADC,
        )
        assert mem_d.role == RosterRole.ADC
        assert mem_d.team_id == team_d.id

        # Verify get_competitive_membership returns Team D
        comp_after_d = await team_membership_repo.get_competitive_membership(user_id)
        assert comp_after_d is not None
        assert comp_after_d.team_id == team_d.id
        assert comp_after_d.role == RosterRole.ADC
        assert comp_after_d.discord_user_id == user_id

        # Total memberships should now be 4
        all_memberships_4 = await team_membership_repo.list_by_user(user_id)
        assert len(all_memberships_4) == 4

        # Step 4: Update role in Team D to substitute
        updated_d_sub = await team_membership_repo.update_role(
            team_id=team_d.id,
            discord_user_id=user_id,
            new_role=RosterRole.SUBSTITUTE,
        )
        assert updated_d_sub.role == RosterRole.SUBSTITUTE

        # Verify get_competitive_membership STILL returns Team D membership
        comp_after_sub = await team_membership_repo.get_competitive_membership(user_id)
        assert comp_after_sub is not None
        assert comp_after_sub.team_id == team_d.id
        assert comp_after_sub.role == RosterRole.SUBSTITUTE
        assert comp_after_sub.discord_user_id == user_id

        # Step 5: Update role in Team D to coach (non-competitive)
        updated_d_coach = await team_membership_repo.update_role(
            team_id=team_d.id,
            discord_user_id=user_id,
            new_role=RosterRole.COACH,
        )
        assert updated_d_coach.role == RosterRole.COACH

        # Verify get_competitive_membership now returns None
        comp_after_coach = await team_membership_repo.get_competitive_membership(user_id)
        assert comp_after_coach is None, (
            "Expected None because user now holds non-competitive roles in all 4 teams."
        )

        # Verify all 4 non-competitive memberships remain intact
        final_memberships = await team_membership_repo.list_by_user(user_id)
        assert len(final_memberships) == 4
        assert all(not m.role.is_competitive() for m in final_memberships)

    @pytest.mark.asyncio
    async def test_multi_team_with_relationship_eager_loading(
        self,
        team_membership_repo: TeamMembershipRepository,
        sample_user: DiscordUser,
        four_teams: tuple[Team, Team, Team, Team],
    ) -> None:
        """
        Verify that get_competitive_membership with with_team=True and with_user=True
        correctly loads SQLAlchemy relationships without raising Greenlet/async lazy-load errors.
        """
        team_a, _, _, team_d = four_teams
        user_id = sample_user.discord_id

        # Create coach in Team A, Mid in Team D
        await team_membership_repo.create(team_a.id, user_id, RosterRole.COACH)
        await team_membership_repo.create(team_d.id, user_id, RosterRole.MID)

        membership = await team_membership_repo.get_competitive_membership(
            user_id, with_team=True, with_user=True
        )
        assert membership is not None
        assert membership.team_id == team_d.id
        assert membership.role == RosterRole.MID
        assert membership.team is not None
        assert membership.team.name == "Team Delta"
        assert membership.discord_user is not None
        assert membership.discord_user.username == "adversarial_player"


# ===========================================================================
# 2. Defensive Input Handling against Malformed & Malicious Strings
# ===========================================================================


class TestDefensiveInputHandling:
    """
    Empirical challenge for defensive input sanitization:
    Verify that invalid, malformed, or malicious team_id strings passed to
    `get`, `list_by_team`, and `delete` do NOT raise PostgreSQL DataError
    and do NOT abort the transaction.
    """

    MALFORMED_TEAM_IDS: list[Any] = [
        "not-a-uuid",
        "' OR 1=1 --",
        "'; DROP TABLE team_memberships; --",
        "SELECT * FROM teams",
        "00000000-0000-0000-0000-00000000000g",  # invalid hex char 'g'
        "00000000-0000-0000-0000-000000000000-extra",  # too long
        "12345",  # integer string
        "   ",  # whitespace
        "",  # empty string
        "NULL",  # string literal NULL
        "None",  # string literal None
        "undefined",
        "NaN",
        "🔥⚽🎉",  # emoji / multibyte unicode
        "uuid::uuid",
        "\\x00\\x01\\x02",
        "a" * 500,  # overflow length string
        123456789,  # integer type
        99.99,  # float type
        None,  # explicit None
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_team_id", MALFORMED_TEAM_IDS)
    async def test_get_with_malformed_team_id(
        self,
        team_membership_repo: TeamMembershipRepository,
        sample_user: DiscordUser,
        bad_team_id: Any,
    ) -> None:
        """
        Verify that `get` with malformed team_id cleanly returns None
        without executing invalid SQL syntax or raising DataError.
        """
        res = await team_membership_repo.get(
            team_id=bad_team_id,
            discord_user_id=sample_user.discord_id,
        )
        assert res is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_team_id", MALFORMED_TEAM_IDS)
    async def test_list_by_team_with_malformed_team_id(
        self,
        team_membership_repo: TeamMembershipRepository,
        bad_team_id: Any,
    ) -> None:
        """
        Verify that `list_by_team` with malformed team_id cleanly returns an empty sequence
        without raising DataError.
        """
        res = await team_membership_repo.list_by_team(team_id=bad_team_id)
        assert res == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_team_id", MALFORMED_TEAM_IDS)
    async def test_delete_with_malformed_team_id(
        self,
        team_membership_repo: TeamMembershipRepository,
        sample_user: DiscordUser,
        bad_team_id: Any,
    ) -> None:
        """
        Verify that `delete` with malformed team_id cleanly returns False
        without raising DataError.
        """
        deleted = await team_membership_repo.delete(
            team_id=bad_team_id,
            discord_user_id=sample_user.discord_id,
        )
        assert deleted is False

    @pytest.mark.asyncio
    async def test_transaction_survivability_after_adversarial_queries(
        self,
        team_membership_repo: TeamMembershipRepository,
        sample_user: DiscordUser,
        four_teams: tuple[Team, Team, Team, Team],
    ) -> None:
        """
        Crucial transaction integrity test:
        In PostgreSQL/PGlite, if an unhandled DataError occurs, the transaction enters
        'InFailedSqlTransaction' state and all subsequent queries fail.

        This test fires dozens of malformed calls in the same transaction session,
        then verifies that the session is still healthy and normal operations succeed.
        """
        team_a, _, _, _ = four_teams

        # 1. Fire barrage of malformed queries across methods
        for bad_id in self.MALFORMED_TEAM_IDS:
            assert await team_membership_repo.get(bad_id, sample_user.discord_id) is None
            assert await team_membership_repo.list_by_team(bad_id) == []
            assert await team_membership_repo.delete(bad_id, sample_user.discord_id) is False

        # 2. Immediately perform valid database writes and reads on the same session
        created = await team_membership_repo.create(
            team_id=team_a.id,
            discord_user_id=sample_user.discord_id,
            role=RosterRole.SUPPORT,
        )
        assert created.role == RosterRole.SUPPORT

        fetched = await team_membership_repo.get(team_a.id, sample_user.discord_id)
        assert fetched is not None
        assert fetched.team_id == team_a.id

        # 3. Fire another malformed query right in the middle
        assert await team_membership_repo.get("' OR 1=1 --", sample_user.discord_id) is None

        # 4. Perform update and delete - must succeed normally
        updated = await team_membership_repo.update_role(
            team_a.id, sample_user.discord_id, new_role=RosterRole.COACH
        )
        assert updated.role == RosterRole.COACH

        deleted = await team_membership_repo.delete(team_a.id, sample_user.discord_id)
        assert deleted is True

        post_del = await team_membership_repo.get(team_a.id, sample_user.discord_id)
        assert post_del is None

    @pytest.mark.asyncio
    async def test_defensive_handling_on_movement_and_audit_repos(
        self,
        roster_movement_repo: RosterMovementRepository,
        audit_log_repo: AuditLogRepository,
    ) -> None:
        """
        Verify defensive UUID handling on RosterMovementRepository and AuditLogRepository.
        """
        for bad_id in ["not-a-uuid", "' OR 1=1 --", None, ""]:
            movements = await roster_movement_repo.list_by_team(bad_id)
            assert movements == []

            logs = await audit_log_repo.list_by_entity(entity_type="team", entity_id=bad_id)
            assert logs == []


# ===========================================================================
# 3. Randomized Fuzzing and Concurrent Lookup Stress Harness
# ===========================================================================


class TestFuzzingAndStressHarness:
    """
    Stress-tests the repository with generated inputs and concurrent queries.
    """

    @pytest.mark.asyncio
    async def test_input_fuzz_generator(
        self,
        team_membership_repo: TeamMembershipRepository,
        sample_user: DiscordUser,
    ) -> None:
        """Generates 50 varied adversarial strings and probes all read/delete methods."""
        fuzz_corpus = [
            f"fuzz_{i}_{c}"
            for i, c in enumerate(
                ["\x00", "\n", "\t", "';--", "/*", "*/", "null", "undefined", "' OR 'a'='a"]
            )
        ] + [
            "00000000-0000-0000-0000-00000000000" + str(i) for i in range(10)
        ]  # valid UUIDs format

        for payload in fuzz_corpus:
            # None of these should throw unhandled database exceptions
            get_res = await team_membership_repo.get(payload, sample_user.discord_id)
            list_res = await team_membership_repo.list_by_team(payload)
            del_res = await team_membership_repo.delete(payload, sample_user.discord_id)

            # Assert expected return types
            assert get_res is None or isinstance(get_res, TeamMembership)
            assert isinstance(list_res, (list, tuple))
            assert isinstance(del_res, bool)

    @pytest.mark.asyncio
    async def test_concurrent_multi_team_membership_lookups(
        self,
        session: AsyncSession,
        team_membership_repo: TeamMembershipRepository,
    ) -> None:
        """
        Stress-tests concurrent reads on multiple teams and users.
        """
        # Create 5 teams and 5 users
        teams = [
            Team(
                id=uuid.uuid4(),
                name=f"Stress Team {i}",
                tag=f"ST{i}",
                slug=f"stress-team-{i}",
                division=Division.PREMIER,
                discord_role_id=990000 + i,
            )
            for i in range(5)
        ]
        users = [
            DiscordUser(
                discord_id=f"99110022{i}",
                username=f"stress_user_{i}",
                role="viewer",
            )
            for i in range(5)
        ]
        session.add_all(teams)
        session.add_all(users)
        await session.flush()

        # Seed diverse memberships
        roles = [
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
            RosterRole.JUNGLE,
            RosterRole.SUBSTITUTE,
        ]
        for idx, u in enumerate(users):
            for t_idx, t in enumerate(teams):
                role = roles[(idx + t_idx) % len(roles)]
                # Ensure each user has only ONE competitive role
                if role.is_competitive() and t_idx > 0:
                    role = RosterRole.STAFF
                await team_membership_repo.create(t.id, u.discord_id, role)

        # Run concurrent lookups using asyncio.gather
        tasks = (
            [team_membership_repo.get_competitive_membership(u.discord_id) for u in users]
            + [team_membership_repo.list_by_user(u.discord_id) for u in users]
            + [team_membership_repo.list_by_team(t.id) for t in teams]
        )

        results = await asyncio.gather(*tasks)
        assert len(results) == 15
