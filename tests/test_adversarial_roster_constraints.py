"""
Empirical Adversarial Challenge Suite: Constraints & Enums on PGlite.

Directly tests and stress-challenges:
1. Captain role check constraint:
   - is_captain = True with non-starter roles ('substitute', 'coach', 'staff', 'partners')
     raises IntegrityError (CheckViolation) at the database level (INSERT, UPDATE, Raw SQL).
   - is_captain = True with starter roles ('top', 'jungle', 'mid', 'adc', 'support') succeeds.
2. Partial unique index on captain:
   - Attempting to insert two captain records for same team_id raises IntegrityError.
   - Updating existing member to captain when one already exists raises IntegrityError.
   - Multiple non-captains (is_captain = False) for same team_id succeed.
   - Captain handoff (demote current, promote successor) succeeds cleanly.
3. Composite primary key:
   - Duplicate (team_id, discord_user_id) raises IntegrityError (UniqueViolation).
   - Same user in multiple teams succeeds (composite uniqueness is scoped per team).
4. High-volume empirical stress harness:
   - Batch insertion across multiple teams and concurrent constraint attack probes.
"""

from __future__ import annotations

import uuid

import psycopg.errors
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division, RosterRole
from liga_bot.models.roster import DiscordUser, Team, TeamMembership


@pytest.fixture
async def sample_team(session: AsyncSession) -> Team:
    """Fixture to provide a persisted team."""
    team = Team(
        id=uuid.uuid4(),
        name="Adversarial FC",
        tag="ADV",
        slug="adversarial-fc",
        division=Division.PREMIER,
        discord_role_id=888111222333,
    )
    session.add(team)
    await session.flush()
    return team


class TestAdversarialCaptainCheckConstraint:
    """Empirically challenge team_memberships_captain_role_check constraint."""

    @pytest.mark.parametrize(
        "non_starter_role",
        [
            RosterRole.SUBSTITUTE,
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
        ],
    )
    @pytest.mark.asyncio
    async def test_insert_non_starter_as_captain_raises_integrity_error(
        self, session: AsyncSession, sample_team: Team, non_starter_role: RosterRole
    ):
        """Verify is_captain=True with non-starters raises CheckViolation on INSERT."""
        user = DiscordUser(
            discord_id=f"user_non_starter_{non_starter_role.value}",
            username=f"user_{non_starter_role.value}",
        )
        session.add(user)
        await session.flush()

        membership = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=user.discord_id,
            role=non_starter_role,
            is_captain=True,
        )

        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                session.add(membership)
                await session.flush()

        assert isinstance(exc_info.value.orig, psycopg.errors.CheckViolation)
        assert "team_memberships_captain_role_check" in str(exc_info.value)

    @pytest.mark.parametrize(
        "starter_role",
        [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
        ],
    )
    @pytest.mark.asyncio
    async def test_insert_starter_as_captain_succeeds(
        self, session: AsyncSession, starter_role: RosterRole
    ):
        """Verify is_captain=True with starter roles succeeds on INSERT."""
        team = Team(
            id=uuid.uuid4(),
            name=f"Team-{starter_role.value}",
            tag="STR",
            slug=f"team-{starter_role.value}",
            division=Division.ASCEND,
            discord_role_id=int(f"777{ord(starter_role.value[0])}"),
        )
        user = DiscordUser(
            discord_id=f"user_starter_{starter_role.value}",
            username=f"starter_{starter_role.value}",
        )
        session.add_all([team, user])
        await session.flush()

        membership = TeamMembership(
            team_id=team.id,
            discord_user_id=user.discord_id,
            role=starter_role,
            is_captain=True,
        )
        session.add(membership)
        await session.flush()

        assert membership.is_captain is True
        assert membership.role == starter_role

    @pytest.mark.parametrize(
        "non_starter_role",
        [
            RosterRole.SUBSTITUTE,
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
        ],
    )
    @pytest.mark.asyncio
    async def test_update_starter_captain_to_non_starter_raises_integrity_error(
        self, session: AsyncSession, sample_team: Team, non_starter_role: RosterRole
    ):
        """Verify updating existing captain's role to non-starter raises CheckViolation."""
        user = DiscordUser(
            discord_id=f"captain_update_{non_starter_role.value}",
            username=f"capt_{non_starter_role.value}",
        )
        session.add(user)
        await session.flush()

        membership = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.TOP,
            is_captain=True,
        )
        session.add(membership)
        await session.flush()

        membership.role = non_starter_role
        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                await session.flush()

        assert isinstance(exc_info.value.orig, psycopg.errors.CheckViolation)
        assert "team_memberships_captain_role_check" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_non_starter_to_captain_raises_integrity_error(
        self, session: AsyncSession, sample_team: Team
    ):
        """Verify promoting a coach to is_captain=True raises CheckViolation on UPDATE."""
        user = DiscordUser(discord_id="coach_to_captain", username="coach_tom")
        session.add(user)
        await session.flush()

        membership = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.COACH,
            is_captain=False,
        )
        session.add(membership)
        await session.flush()

        membership.is_captain = True
        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                await session.flush()

        assert isinstance(exc_info.value.orig, psycopg.errors.CheckViolation)
        assert "team_memberships_captain_role_check" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raw_sql_violating_captain_check_constraint(
        self, session: AsyncSession, sample_team: Team
    ):
        """Verify PGlite database engine enforces check constraint via direct Raw SQL."""
        user = DiscordUser(discord_id="raw_sql_violator", username="violator")
        session.add(user)
        await session.flush()

        raw_insert = sa.text(
            "INSERT INTO team_memberships "
            "(team_id, discord_user_id, role, is_captain, created_at, updated_at) "
            "VALUES (:t_id, :u_id, 'substitute'::roster_role, true, NOW(), NOW())"
        )

        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                await session.execute(raw_insert, {"t_id": sample_team.id, "u_id": user.discord_id})

        assert isinstance(exc_info.value.orig, psycopg.errors.CheckViolation)
        assert "team_memberships_captain_role_check" in str(exc_info.value)


class TestAdversarialPartialUniqueCaptainIndex:
    """Empirically challenge team_memberships_unique_captain partial unique index."""

    @pytest.mark.asyncio
    async def test_insert_two_captains_same_team_raises_integrity_error(
        self, session: AsyncSession, sample_team: Team
    ):
        """Verify inserting two captain records for the same team raises UniqueViolation."""
        u1 = DiscordUser(discord_id="c1", username="cap1")
        u2 = DiscordUser(discord_id="c2", username="cap2")
        session.add_all([u1, u2])
        await session.flush()

        m1 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=u1.discord_id,
            role=RosterRole.MID,
            is_captain=True,
        )
        session.add(m1)
        await session.flush()

        m2 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=u2.discord_id,
            role=RosterRole.ADC,
            is_captain=True,
        )

        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                session.add(m2)
                await session.flush()

        assert isinstance(exc_info.value.orig, psycopg.errors.UniqueViolation)
        assert "team_memberships_unique_captain" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_member_to_captain_when_captain_exists_raises_integrity_error(
        self, session: AsyncSession, sample_team: Team
    ):
        """Verify updating existing member to is_captain=True collides with existing captain."""
        u1 = DiscordUser(discord_id="active_cap", username="active_cap")
        u2 = DiscordUser(discord_id="aspiring_cap", username="aspiring_cap")
        session.add_all([u1, u2])
        await session.flush()

        m1 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=u1.discord_id,
            role=RosterRole.TOP,
            is_captain=True,
        )
        m2 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=u2.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )
        session.add_all([m1, m2])
        await session.flush()

        m2.is_captain = True
        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                await session.flush()

        assert isinstance(exc_info.value.orig, psycopg.errors.UniqueViolation)
        assert "team_memberships_unique_captain" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_multiple_non_captains_in_same_team_succeed(
        self, session: AsyncSession, sample_team: Team
    ):
        """Verify inserting 20 non-captain members in the same team succeeds cleanly."""
        users = [
            DiscordUser(discord_id=f"multi_nc_{i}", username=f"non_cap_{i}") for i in range(20)
        ]
        session.add_all(users)
        await session.flush()

        memberships = [
            TeamMembership(
                team_id=sample_team.id,
                discord_user_id=u.discord_id,
                role=RosterRole.TOP if i % 2 == 0 else RosterRole.SUBSTITUTE,
                is_captain=False,
            )
            for i, u in enumerate(users)
        ]
        session.add_all(memberships)
        await session.flush()

        assert len(memberships) == 20
        for m in memberships:
            assert m.is_captain is False

    @pytest.mark.asyncio
    async def test_captain_handoff_within_transaction(
        self, session: AsyncSession, sample_team: Team
    ):
        """Verify captain handoff (demote current, promote successor) within transaction."""
        u1 = DiscordUser(discord_id="retiring_cap", username="retiring_cap")
        u2 = DiscordUser(discord_id="successor_cap", username="successor_cap")
        session.add_all([u1, u2])
        await session.flush()

        m1 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=u1.discord_id,
            role=RosterRole.MID,
            is_captain=True,
        )
        m2 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=u2.discord_id,
            role=RosterRole.ADC,
            is_captain=False,
        )
        session.add_all([m1, m2])
        await session.flush()

        m1.is_captain = False
        await session.flush()
        m2.is_captain = True
        await session.flush()

        assert m1.is_captain is False
        assert m2.is_captain is True


class TestAdversarialCompositePrimaryKey:
    """Empirically challenge composite primary key (team_id, discord_user_id)."""

    @pytest.mark.asyncio
    async def test_duplicate_team_and_user_raises_integrity_error(
        self, session: AsyncSession, sample_team: Team
    ):
        """Verify duplicate (team_id, discord_user_id) raises UniqueViolation on PK."""
        user = DiscordUser(discord_id="duplicate_pk_user", username="pk_user")
        session.add(user)
        await session.flush()

        m1 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.TOP,
            is_captain=False,
        )
        session.add(m1)
        await session.flush()

        m2 = TeamMembership(
            team_id=sample_team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.COACH,
            is_captain=False,
        )

        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                session.add(m2)
                await session.flush()

        assert isinstance(exc_info.value.orig, psycopg.errors.UniqueViolation)
        err_msg = str(exc_info.value).lower()
        assert "team_memberships_pkey" in err_msg or "duplicate key" in err_msg

    @pytest.mark.asyncio
    async def test_same_user_in_multiple_teams_allowed_by_pk(self, session: AsyncSession):
        """Verify composite PK allows the same discord_user_id in different teams."""
        t1 = Team(
            id=uuid.uuid4(),
            name="Club One",
            tag="C1",
            slug="club-one",
            division=Division.PREMIER,
            discord_role_id=1010101,
        )
        t2 = Team(
            id=uuid.uuid4(),
            name="Club Two",
            tag="C2",
            slug="club-two",
            division=Division.ASCEND,
            discord_role_id=2020202,
        )
        user = DiscordUser(discord_id="multiclub_staff", username="staff_user")
        session.add_all([t1, t2, user])
        await session.flush()

        m1 = TeamMembership(
            team_id=t1.id,
            discord_user_id=user.discord_id,
            role=RosterRole.COACH,
            is_captain=False,
        )
        m2 = TeamMembership(
            team_id=t2.id,
            discord_user_id=user.discord_id,
            role=RosterRole.STAFF,
            is_captain=False,
        )
        session.add_all([m1, m2])
        await session.flush()

        assert m1.team_id == t1.id
        assert m2.team_id == t2.id
        assert m1.discord_user_id == user.discord_id
        assert m2.discord_user_id == user.discord_id


class TestAdversarialStressHarness:
    """High-volume empirical stress harness on PGlite."""

    @pytest.mark.asyncio
    async def test_mass_team_creation_and_adversarial_injection(self, session: AsyncSession):
        """
        Stress test creating 10 teams with complete rosters (6 members each),
        then launching simultaneous adversarial collisions across all 10 teams:
        - 10 attempts to insert a second captain (partial unique index check).
        - 10 attempts to assign captaincy to a coach (check constraint).
        - 10 attempts to duplicate membership (composite PK).
        """
        num_teams = 10
        teams = []
        users = []

        for i in range(num_teams):
            t = Team(
                id=uuid.uuid4(),
                name=f"Stress Team {i}",
                tag=f"ST{i:02d}",
                slug=f"stress-team-{i}",
                division=Division.PREMIER if i % 2 == 0 else Division.ASCEND,
                discord_role_id=900000 + i,
            )
            teams.append(t)
            session.add(t)

            team_users = []
            for j in range(6):
                u = DiscordUser(
                    discord_id=f"stress_u_{i}_{j}",
                    username=f"st_user_{i}_{j}",
                )
                users.append(u)
                team_users.append(u)
            session.add_all(team_users)
            await session.flush()

            team_memberships = [
                TeamMembership(
                    team_id=t.id,
                    discord_user_id=team_users[0].discord_id,
                    role=RosterRole.TOP,
                    is_captain=True,
                ),
                TeamMembership(
                    team_id=t.id,
                    discord_user_id=team_users[1].discord_id,
                    role=RosterRole.JUNGLE,
                    is_captain=False,
                ),
                TeamMembership(
                    team_id=t.id,
                    discord_user_id=team_users[2].discord_id,
                    role=RosterRole.MID,
                    is_captain=False,
                ),
                TeamMembership(
                    team_id=t.id,
                    discord_user_id=team_users[3].discord_id,
                    role=RosterRole.ADC,
                    is_captain=False,
                ),
                TeamMembership(
                    team_id=t.id,
                    discord_user_id=team_users[4].discord_id,
                    role=RosterRole.SUPPORT,
                    is_captain=False,
                ),
                TeamMembership(
                    team_id=t.id,
                    discord_user_id=team_users[5].discord_id,
                    role=RosterRole.COACH,
                    is_captain=False,
                ),
            ]
            session.add_all(team_memberships)
            await session.flush()

        extra_users = [
            DiscordUser(discord_id=f"extra_u_{i}", username=f"extra_{i}") for i in range(num_teams)
        ]
        session.add_all(extra_users)
        await session.flush()

        # Attack 1: Attempt to add a 2nd captain to all teams
        for i, t in enumerate(teams):
            bad_captain = TeamMembership(
                team_id=t.id,
                discord_user_id=extra_users[i].discord_id,
                role=RosterRole.MID,
                is_captain=True,
            )
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin_nested():
                    session.add(bad_captain)
                    await session.flush()
            assert isinstance(exc_info.value.orig, psycopg.errors.UniqueViolation)
            assert "team_memberships_unique_captain" in str(exc_info.value)

        # Attack 2: Attempt to assign is_captain=True to a coach in all teams
        for i, t in enumerate(teams):
            bad_coach = TeamMembership(
                team_id=t.id,
                discord_user_id=extra_users[i].discord_id,
                role=RosterRole.COACH,
                is_captain=True,
            )
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin_nested():
                    session.add(bad_coach)
                    await session.flush()
            assert isinstance(exc_info.value.orig, psycopg.errors.CheckViolation)
            assert "team_memberships_captain_role_check" in str(exc_info.value)

        # Attack 3: Attempt to insert duplicate composite PK for existing member
        for i, t in enumerate(teams):
            duplicate_pk = TeamMembership(
                team_id=t.id,
                discord_user_id=f"stress_u_{i}_0",
                role=RosterRole.SUPPORT,
                is_captain=False,
            )
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin_nested():
                    session.add(duplicate_pk)
                    await session.flush()
            assert isinstance(exc_info.value.orig, psycopg.errors.UniqueViolation)
            err_msg = str(exc_info.value).lower()
            assert "team_memberships_pkey" in err_msg or "duplicate key" in err_msg
