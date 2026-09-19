"""
Empirical Adversarial Challenge Suite: Cascades, Lifecycle & JSONB Serialization on PGlite.

Directly tests and stress-challenges:
1. Foreign key cascading:
   - Deleting a Team must cascade-delete all corresponding TeamMembership and RosterMovement rows
     (ORM deletion and raw SQL DDL level bypass).
   - Deleting a DiscordUser must cascade-delete their TeamMembership and RosterMovement rows
     (ORM and raw SQL, multi-team membership, subject vs actor lifecycle in RosterMovement).
   - Deleting a DiscordUser must set player.discord_user_id to NULL and
     audit_log.actor_discord_user_id to NULL (ON DELETE SET NULL).
2. AuditLog JSONB robustness:
   - Complex nested structures: deep dicts, heterogeneous arrays, unicode strings,
     emojis, escape characters, booleans, explicit null values, empty structures,
     and large payloads.
   - Querying AuditLog by entity_type and entity_id using the composite index:
     exact point lookup, entity_type prefix lookup, null entity_id, EXPLAIN index eligibility,
     and PostgreSQL JSONB path/containment operators.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import (
    AuditLog,
    DiscordUser,
    Player,
    RosterMovement,
    Team,
    TeamMembership,
)


@pytest.fixture
async def sample_teams(session: AsyncSession) -> tuple[Team, Team]:
    """Crea dos equipos independientes para pruebas de aislamiento de cascada."""
    t1 = Team(
        id=uuid.uuid4(),
        name="Team Zenith",
        tag="ZEN",
        slug="team-zenith",
        division=Division.PREMIER,
        discord_role_id=777001001,
    )
    t2 = Team(
        id=uuid.uuid4(),
        name="Team Eclipse",
        tag="ECL",
        slug="team-eclipse",
        division=Division.PREMIER,
        discord_role_id=777002002,
    )
    session.add_all([t1, t2])
    await session.flush()
    return t1, t2


@pytest.fixture
async def sample_users(session: AsyncSession) -> tuple[DiscordUser, DiscordUser, DiscordUser]:
    """Crea tres usuarios de Discord para pruebas de cascada y relaciones duales."""
    u1 = DiscordUser(discord_id="usr_001", username="faker_mid")
    u2 = DiscordUser(discord_id="usr_002", username="gumayusi_adc")
    u3 = DiscordUser(discord_id="usr_003", username="kkoma_coach")
    session.add_all([u1, u2, u3])
    await session.flush()
    return u1, u2, u3


# ============================================================================
# 1. Foreign Key Cascading: Team Deletion
# ============================================================================


class TestTeamCascadeDeletions:
    """Empirically verify that deleting a Team cascade-deletes memberships & movements."""

    @pytest.mark.asyncio
    async def test_orm_delete_team_cascades_memberships_and_movements(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """ORM session.delete(team) must delete all TeamMembership and RosterMovement rows."""
        t1, _ = sample_teams
        u1, u2, u3 = sample_users

        t1_id = t1.id
        u1_id = u1.discord_id
        u2_id = u2.discord_id
        u3_id = u3.discord_id

        # Create multiple memberships
        m1 = TeamMembership(
            team_id=t1_id, discord_user_id=u1_id, role=RosterRole.MID, is_captain=True
        )
        m2 = TeamMembership(
            team_id=t1_id, discord_user_id=u2_id, role=RosterRole.ADC, is_captain=False
        )
        m3 = TeamMembership(
            team_id=t1_id, discord_user_id=u3_id, role=RosterRole.COACH, is_captain=False
        )

        # Create multiple movements
        mov1 = RosterMovement(
            team_id=t1_id,
            discord_user_id=u1_id,
            action=RosterMovementAction.JOINED,
            role=RosterRole.MID,
            actor_id=u3_id,
        )
        mov2 = RosterMovement(
            team_id=t1_id,
            discord_user_id=u1_id,
            action=RosterMovementAction.PROMOTED_TO_CAPTAIN,
            role=RosterRole.MID,
            actor_id=u3_id,
        )
        mov3 = RosterMovement(
            team_id=t1_id,
            discord_user_id=u2_id,
            action=RosterMovementAction.JOINED,
            role=RosterRole.ADC,
        )
        session.add_all([m1, m2, m3, mov1, mov2, mov3])
        await session.flush()

        # Delete team via ORM
        await session.delete(t1)
        await session.flush()
        session.expire_all()

        # Verify memberships were deleted
        res_m = await session.execute(
            sa.select(TeamMembership).where(TeamMembership.team_id == t1_id)
        )
        assert res_m.scalars().all() == []

        # Verify movements were deleted
        res_mov = await session.execute(
            sa.select(RosterMovement).where(RosterMovement.team_id == t1_id)
        )
        assert res_mov.scalars().all() == []

        # Verify users were NOT deleted
        res_u = await session.execute(
            sa.select(DiscordUser).where(DiscordUser.discord_id.in_([u1_id, u2_id, u3_id]))
        )
        assert len(res_u.scalars().all()) == 3

    @pytest.mark.asyncio
    async def test_raw_sql_delete_team_cascades_at_ddl_level(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """Raw SQL 'DELETE FROM teams' must trigger ON DELETE CASCADE in PostgreSQL without ORM."""
        t1, _ = sample_teams
        u1, _, _ = sample_users

        t1_id = t1.id
        u1_id = u1.discord_id

        m = TeamMembership(
            team_id=t1_id, discord_user_id=u1_id, role=RosterRole.TOP, is_captain=False
        )
        mov = RosterMovement(
            team_id=t1_id,
            discord_user_id=u1_id,
            action=RosterMovementAction.JOINED,
            role=RosterRole.TOP,
        )
        session.add_all([m, mov])
        await session.flush()

        # Execute raw SQL delete bypassing SQLAlchemy ORM
        await session.execute(text("DELETE FROM teams WHERE id = :team_id"), {"team_id": t1_id})
        await session.flush()
        session.expire_all()

        # Check raw counts in database
        cnt_m = (
            await session.execute(
                text("SELECT COUNT(*) FROM team_memberships WHERE team_id = :team_id"),
                {"team_id": t1_id},
            )
        ).scalar()
        cnt_mov = (
            await session.execute(
                text("SELECT COUNT(*) FROM roster_movements WHERE team_id = :team_id"),
                {"team_id": t1_id},
            )
        ).scalar()

        assert cnt_m == 0
        assert cnt_mov == 0

    @pytest.mark.asyncio
    async def test_delete_team_multi_team_isolation(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """Deleting Team A must NOT cascade or affect Team B's memberships or movements."""
        t1, t2 = sample_teams
        u1, u2, _ = sample_users

        t1_id = t1.id
        t2_id = t2.id
        u1_id = u1.discord_id
        u2_id = u2.discord_id

        # Memberships in t1 and t2
        m_t1 = TeamMembership(
            team_id=t1_id, discord_user_id=u1_id, role=RosterRole.MID, is_captain=True
        )
        m_t2 = TeamMembership(
            team_id=t2_id, discord_user_id=u2_id, role=RosterRole.ADC, is_captain=True
        )
        # Movement in t1 and t2
        mov_t1 = RosterMovement(
            team_id=t1_id, discord_user_id=u1_id, action=RosterMovementAction.JOINED
        )
        mov_t2 = RosterMovement(
            team_id=t2_id, discord_user_id=u2_id, action=RosterMovementAction.JOINED
        )

        session.add_all([m_t1, m_t2, mov_t1, mov_t2])
        await session.flush()

        # Delete t1
        await session.delete(t1)
        await session.flush()
        session.expire_all()

        # t1 data gone
        res_t1_m = (
            (
                await session.execute(
                    sa.select(TeamMembership).where(TeamMembership.team_id == t1_id)
                )
            )
            .scalars()
            .all()
        )
        assert res_t1_m == []

        # t2 data intact
        res_t2_m = (
            (
                await session.execute(
                    sa.select(TeamMembership).where(TeamMembership.team_id == t2_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(res_t2_m) == 1
        assert res_t2_m[0].discord_user_id == u2_id

        res_t2_mov = (
            (
                await session.execute(
                    sa.select(RosterMovement).where(RosterMovement.team_id == t2_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(res_t2_mov) == 1


# ============================================================================
# 2. Foreign Key Cascading: DiscordUser Deletion
# ============================================================================


class TestDiscordUserCascadeDeletions:
    """Empirically verify that deleting a DiscordUser cascade-deletes memberships & movements."""

    @pytest.mark.asyncio
    async def test_orm_delete_user_cascades_memberships_and_movements(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """Deleting a DiscordUser must cascade-delete their memberships and movements."""
        t1, t2 = sample_teams
        u1, u2, _ = sample_users

        t1_id = t1.id
        t2_id = t2.id
        u1_id = u1.discord_id
        u2_id = u2.discord_id

        # u1 is in t1 and t2
        m1 = TeamMembership(
            team_id=t1_id, discord_user_id=u1_id, role=RosterRole.MID, is_captain=True
        )
        m2 = TeamMembership(
            team_id=t2_id, discord_user_id=u1_id, role=RosterRole.COACH, is_captain=False
        )
        # u2 is in t1
        m3 = TeamMembership(
            team_id=t1_id, discord_user_id=u2_id, role=RosterRole.SUPPORT, is_captain=False
        )

        # Movements for u1
        mov1 = RosterMovement(
            team_id=t1_id, discord_user_id=u1_id, action=RosterMovementAction.JOINED
        )
        mov2 = RosterMovement(
            team_id=t2_id, discord_user_id=u1_id, action=RosterMovementAction.JOINED
        )
        # Movement for u2
        mov3 = RosterMovement(
            team_id=t1_id, discord_user_id=u2_id, action=RosterMovementAction.JOINED
        )

        session.add_all([m1, m2, m3, mov1, mov2, mov3])
        await session.flush()

        # Delete u1
        await session.delete(u1)
        await session.flush()
        session.expire_all()

        # u1 memberships gone
        res_u1_m = (
            (
                await session.execute(
                    sa.select(TeamMembership).where(TeamMembership.discord_user_id == u1_id)
                )
            )
            .scalars()
            .all()
        )
        assert res_u1_m == []

        # u1 movements gone
        res_u1_mov = (
            (
                await session.execute(
                    sa.select(RosterMovement).where(RosterMovement.discord_user_id == u1_id)
                )
            )
            .scalars()
            .all()
        )
        assert res_u1_mov == []

        # u2 membership and movement untouched
        res_u2_m = (
            (
                await session.execute(
                    sa.select(TeamMembership).where(TeamMembership.discord_user_id == u2_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(res_u2_m) == 1

        res_u2_mov = (
            (
                await session.execute(
                    sa.select(RosterMovement).where(RosterMovement.discord_user_id == u2_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(res_u2_mov) == 1

        # Teams t1 and t2 still exist
        teams = (
            (await session.execute(sa.select(Team).where(Team.id.in_([t1_id, t2_id]))))
            .scalars()
            .all()
        )
        assert len(teams) == 2

    @pytest.mark.asyncio
    async def test_raw_sql_delete_user_cascades_at_ddl_level(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """Raw SQL 'DELETE FROM discord_users' triggers ON DELETE CASCADE in PostgreSQL."""
        t1, _ = sample_teams
        u1, _, _ = sample_users

        t1_id = t1.id
        u1_id = u1.discord_id

        m = TeamMembership(
            team_id=t1_id, discord_user_id=u1_id, role=RosterRole.JUNGLE, is_captain=False
        )
        mov = RosterMovement(
            team_id=t1_id, discord_user_id=u1_id, action=RosterMovementAction.JOINED
        )
        session.add_all([m, mov])
        await session.flush()

        # Raw SQL delete
        await session.execute(
            text("DELETE FROM discord_users WHERE discord_id = :uid"), {"uid": u1_id}
        )
        await session.flush()
        session.expire_all()

        cnt_m = (
            await session.execute(
                text("SELECT COUNT(*) FROM team_memberships WHERE discord_user_id = :uid"),
                {"uid": u1_id},
            )
        ).scalar()
        cnt_mov = (
            await session.execute(
                text("SELECT COUNT(*) FROM roster_movements WHERE discord_user_id = :uid"),
                {"uid": u1_id},
            )
        ).scalar()

        assert cnt_m == 0
        assert cnt_mov == 0

    @pytest.mark.asyncio
    async def test_roster_movement_actor_vs_subject_lifecycle(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """
        Adversarial edge case:
        - When ACTOR user is deleted: movement is NOT deleted, actor_id is SET NULL.
        - When SUBJECT user is deleted: movement IS deleted (CASCADE).
        """
        t1, _ = sample_teams
        subject_user, other_user, actor_user = sample_users

        t1_id = t1.id
        sub_id = subject_user.discord_id
        oth_id = other_user.discord_id
        act_id = actor_user.discord_id

        # Movement 1: actor_user acts on subject_user
        mov1 = RosterMovement(
            team_id=t1_id,
            discord_user_id=sub_id,
            action=RosterMovementAction.JOINED,
            role=RosterRole.ADC,
            actor_id=act_id,
        )
        # Movement 2: actor_user acts on other_user
        mov2 = RosterMovement(
            team_id=t1_id,
            discord_user_id=oth_id,
            action=RosterMovementAction.ROLE_CHANGED,
            role=RosterRole.TOP,
            actor_id=act_id,
        )
        session.add_all([mov1, mov2])
        await session.flush()

        mov1_id = mov1.id
        mov2_id = mov2.id

        # Sub-test A: Delete actor_user. Both movements should SURVIVE with actor_id = NULL.
        await session.delete(actor_user)
        await session.flush()
        session.expire_all()

        refreshed_mov1 = (
            await session.execute(sa.select(RosterMovement).where(RosterMovement.id == mov1_id))
        ).scalar_one()
        refreshed_mov2 = (
            await session.execute(sa.select(RosterMovement).where(RosterMovement.id == mov2_id))
        ).scalar_one()

        assert refreshed_mov1.actor_id is None
        assert refreshed_mov1.discord_user_id == sub_id
        assert refreshed_mov2.actor_id is None
        assert refreshed_mov2.discord_user_id == oth_id

        # Sub-test B: Now delete subject_user. mov1 MUST be deleted (CASCADE), while mov2 survives.
        subject_ref = (
            await session.execute(sa.select(DiscordUser).where(DiscordUser.discord_id == sub_id))
        ).scalar_one()
        await session.delete(subject_ref)
        await session.flush()
        session.expire_all()

        deleted_mov1 = (
            await session.execute(sa.select(RosterMovement).where(RosterMovement.id == mov1_id))
        ).scalar_one_or_none()
        surviving_mov2 = (
            await session.execute(sa.select(RosterMovement).where(RosterMovement.id == mov2_id))
        ).scalar_one_or_none()

        assert deleted_mov1 is None
        assert surviving_mov2 is not None

    @pytest.mark.asyncio
    async def test_roster_movement_self_action_dual_fk_deletion(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """
        Adversarial edge case:
        User is both discord_user_id (CASCADE) and actor_id (SET NULL) on the SAME row.
        Deleting the user must cleanly cascade without constraint conflict or deadlock.
        """
        t1, _ = sample_teams
        u1, _, _ = sample_users

        t1_id = t1.id
        u1_id = u1.discord_id

        mov_self = RosterMovement(
            team_id=t1_id,
            discord_user_id=u1_id,
            action=RosterMovementAction.JOINED,
            role=RosterRole.MID,
            actor_id=u1_id,  # Self-action
        )
        session.add(mov_self)
        await session.flush()

        mov_id = mov_self.id

        await session.delete(u1)
        await session.flush()
        session.expire_all()

        res = (
            await session.execute(sa.select(RosterMovement).where(RosterMovement.id == mov_id))
        ).scalar_one_or_none()
        assert res is None


# ============================================================================
# 3. ON DELETE SET NULL on Player and AuditLog
# ============================================================================


class TestDiscordUserSetNullCascades:
    """Empirically verify ON DELETE SET NULL for player and audit_log user references."""

    @pytest.mark.asyncio
    async def test_orm_delete_user_sets_null_on_multiple_players_and_audit_logs(
        self,
        session: AsyncSession,
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """Deleting a DiscordUser sets user FK to NULL on all their players and audit_logs."""
        u1, _, _ = sample_users
        u1_id = u1.discord_id

        # User owns 3 players (main, smurf1, smurf2)
        p1 = Player(discord_user_id=u1_id, game_name="Faker", riot_tag="KR1", is_main=True)
        p2 = Player(discord_user_id=u1_id, game_name="Hide on bush", riot_tag="KR1", is_main=False)
        p3 = Player(discord_user_id=u1_id, game_name="Barcode", riot_tag="KR2", is_main=False)

        # User generated 2 audit logs
        log1 = AuditLog(
            actor_discord_user_id=u1_id,
            action="roster.role_changed",
            entity_type="team_membership",
        )
        log2 = AuditLog(
            actor_discord_user_id=u1_id,
            action="roster.captain_assigned",
            entity_type="team_membership",
        )
        session.add_all([p1, p2, p3, log1, log2])
        await session.flush()

        p_ids = [p1.id, p2.id, p3.id]
        log_ids = [log1.id, log2.id]

        # Delete user via ORM
        await session.delete(u1)
        await session.flush()
        session.expire_all()

        # Verify all 3 players survive and have discord_user_id = None
        res_players = (
            (await session.execute(sa.select(Player).where(Player.id.in_(p_ids)))).scalars().all()
        )
        assert len(res_players) == 3
        for p in res_players:
            assert p.discord_user_id is None

        # Verify all 2 audit logs survive and have actor_discord_user_id = None
        res_logs = (
            (await session.execute(sa.select(AuditLog).where(AuditLog.id.in_(log_ids))))
            .scalars()
            .all()
        )
        assert len(res_logs) == 2
        for log in res_logs:
            assert log.actor_discord_user_id is None

    @pytest.mark.asyncio
    async def test_raw_sql_delete_user_sets_null_at_ddl_level(
        self,
        session: AsyncSession,
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """Raw SQL 'DELETE FROM discord_users' triggers ON DELETE SET NULL in PostgreSQL."""
        u1, _, _ = sample_users
        u1_id = u1.discord_id

        p = Player(discord_user_id=u1_id, game_name="Keria", riot_tag="KR1")
        log = AuditLog(actor_discord_user_id=u1_id, action="test.raw_delete", entity_type="test")
        session.add_all([p, log])
        await session.flush()

        p_id = p.id
        log_id = log.id

        # Raw SQL delete
        await session.execute(
            text("DELETE FROM discord_users WHERE discord_id = :uid"),
            {"uid": u1_id},
        )
        await session.flush()
        session.expire_all()

        # Inspect database values directly via raw SQL
        p_uid = (
            await session.execute(
                text("SELECT discord_user_id FROM players WHERE id = :pid"),
                {"pid": p_id},
            )
        ).scalar()
        log_uid = (
            await session.execute(
                text("SELECT actor_discord_user_id FROM audit_logs WHERE id = :lid"),
                {"lid": log_id},
            )
        ).scalar()

        assert p_uid is None
        assert log_uid is None

    @pytest.mark.asyncio
    async def test_unlinked_player_and_system_audit_log_unaffected(
        self,
        session: AsyncSession,
        sample_users: tuple[DiscordUser, DiscordUser, DiscordUser],
    ):
        """Entities with NULL user references remain unaffected when users are deleted."""
        u1, _, _ = sample_users

        unlinked_player = Player(discord_user_id=None, game_name="SoloQueueWarrior", riot_tag="EUW")
        system_log = AuditLog(
            actor_discord_user_id=None, action="system.cron_tick", entity_type="system"
        )
        session.add_all([unlinked_player, system_log])
        await session.flush()

        pid = unlinked_player.id
        lid = system_log.id

        # Delete u1
        await session.delete(u1)
        await session.flush()
        session.expire_all()

        p = (await session.execute(sa.select(Player).where(Player.id == pid))).scalar_one()
        audit_res = (
            await session.execute(sa.select(AuditLog).where(AuditLog.id == lid))
        ).scalar_one()

        assert p.discord_user_id is None
        assert audit_res.actor_discord_user_id is None
        assert p.game_name == "SoloQueueWarrior"


# ============================================================================
# 4. AuditLog JSONB Robustness & Composite Index
# ============================================================================


class TestAuditLogJSONBRobustness:
    """Empirically challenge AuditLog JSONB storage, retrieval, and composite index queries."""

    @pytest.mark.asyncio
    async def test_deeply_nested_dict_jsonb(self, session: AsyncSession):
        """Test storing and retrieving 6 levels of deeply nested dictionary structures."""
        nested_data = {
            "level1": {
                "level2": {
                    "level3": {
                        "level4": {
                            "level5": {
                                "level6": "deep_leaf_value",
                                "counter": 42,
                                "active": True,
                            }
                        }
                    }
                }
            }
        }
        log = AuditLog(
            action="test.deep_nesting",
            entity_type="nested_test",
            entity_id=uuid.uuid4(),
            before=None,
            after=nested_data,
        )
        session.add(log)
        await session.flush()

        log_id = log.id
        session.expire_all()

        fetched = (
            await session.execute(sa.select(AuditLog).where(AuditLog.id == log_id))
        ).scalar_one()

        assert fetched.after == nested_data
        assert (
            fetched.after["level1"]["level2"]["level3"]["level4"]["level5"]["level6"]
            == "deep_leaf_value"
        )
        assert fetched.after["level1"]["level2"]["level3"]["level4"]["level5"]["counter"] == 42
        assert fetched.after["level1"]["level2"]["level3"]["level4"]["level5"]["active"] is True

    @pytest.mark.asyncio
    async def test_heterogeneous_arrays_and_mixed_types(self, session: AsyncSession):
        """Test storing heterogeneous arrays: numbers, booleans, nulls, and sub-dicts."""
        complex_array = [
            100,
            3.14159265359,
            "string_item",
            True,
            False,
            None,
            {"sub_key": "sub_value", "sub_list": [1, 2, 3]},
            ["nested_array_item_1", "nested_array_item_2"],
        ]
        log = AuditLog(
            action="test.mixed_array",
            entity_type="array_test",
            entity_id=uuid.uuid4(),
            before={"items": complex_array},
            after={"items": complex_array[::-1]},
        )
        session.add(log)
        await session.flush()

        log_id = log.id
        session.expire_all()

        fetched = (
            await session.execute(sa.select(AuditLog).where(AuditLog.id == log_id))
        ).scalar_one()

        assert fetched.before["items"] == complex_array
        assert fetched.before["items"][0] == 100
        assert fetched.before["items"][1] == pytest.approx(3.14159265359)
        assert fetched.before["items"][3] is True
        assert fetched.before["items"][4] is False
        assert fetched.before["items"][5] is None
        assert fetched.before["items"][6]["sub_key"] == "sub_value"
        assert fetched.before["items"][7] == ["nested_array_item_1", "nested_array_item_2"]

    @pytest.mark.asyncio
    async def test_unicode_emojis_and_escape_characters(self, session: AsyncSession):
        """Test storing international strings, emojis, special characters, and escapes in JSONB."""
        unicode_payload = {
            "emojis": "🎮 🏆 🛡️ ⚔️ 🔥 ⚡ 🎯 👑 🌟 🚀",
            "korean": "이상혁 (Lee Sang-hyeok) - 대상혁",
            "chinese": "英雄联盟职业联赛",
            "japanese": "リーグ・オブ・レジェンド",
            "spanish_accents": "Canción de cuna para el pingüino en el año 2026",
            "symbols": "≠ ≤ ≥ ÷ × √ π ∞ § ¶ © ® ™",
            "escapes": 'line1\nline2\ttabbed "quoted" and \\backslashes\\',
            "html_unsafe": "<script>alert('xss')</script> & 'single_quote'",
        }
        log = AuditLog(
            action="test.unicode",
            entity_type="i18n_test",
            entity_id=uuid.uuid4(),
            before=unicode_payload,
            after=unicode_payload,
        )
        session.add(log)
        await session.flush()

        log_id = log.id
        session.expire_all()

        fetched = (
            await session.execute(sa.select(AuditLog).where(AuditLog.id == log_id))
        ).scalar_one()

        for key, val in unicode_payload.items():
            assert fetched.before[key] == val
            assert fetched.after[key] == val

    @pytest.mark.asyncio
    async def test_booleans_and_explicit_nulls(self, session: AsyncSession):
        """Test JSONB explicit null values vs missing keys, and strict boolean handling."""
        payload_with_nulls = {
            "is_captain": True,
            "is_starter": False,
            "replacement_role": None,  # Explicit JSON null
            "notes": None,
            "nested": {
                "verified": False,
                "reason": None,
            },
        }
        log = AuditLog(
            action="test.nulls_and_booleans",
            entity_type="membership",
            entity_id=uuid.uuid4(),
            before=payload_with_nulls,
            after={"replacement_role": "substitute", "is_captain": False},
        )
        session.add(log)
        await session.flush()

        log_id = log.id
        session.expire_all()

        fetched = (
            await session.execute(sa.select(AuditLog).where(AuditLog.id == log_id))
        ).scalar_one()

        assert fetched.before["is_captain"] is True
        assert fetched.before["is_starter"] is False
        assert fetched.before["replacement_role"] is None
        assert "replacement_role" in fetched.before
        assert fetched.before["nested"]["verified"] is False
        assert fetched.before["nested"]["reason"] is None

    @pytest.mark.asyncio
    async def test_empty_containers_and_boundary_structures(self, session: AsyncSession):
        """Test empty dict, empty list, and edge case JSON structures."""
        log1 = AuditLog(
            action="test.empty_dict",
            entity_type="empty_test",
            before={},
            after={},
        )
        log2 = AuditLog(
            action="test.empty_nested",
            entity_type="empty_test",
            before={"empty_list": [], "empty_dict": {}},
            after={"nested_empty": {"list": []}},
        )
        session.add_all([log1, log2])
        await session.flush()

        id1 = log1.id
        id2 = log2.id
        session.expire_all()

        f1 = (await session.execute(sa.select(AuditLog).where(AuditLog.id == id1))).scalar_one()
        f2 = (await session.execute(sa.select(AuditLog).where(AuditLog.id == id2))).scalar_one()

        assert f1.before == {}
        assert f1.after == {}
        assert f2.before == {"empty_list": [], "empty_dict": {}}
        assert f2.after == {"nested_empty": {"list": []}}

    @pytest.mark.asyncio
    async def test_large_payload_stress(self, session: AsyncSession):
        """Test high-volume JSONB payload with 200 distinct keys and 500-element array."""
        large_dict = {f"field_{i}": f"value_{i}_{'x' * 20}" for i in range(200)}
        large_dict["large_array"] = [f"item_{j}" for j in range(500)]

        log = AuditLog(
            action="test.large_payload",
            entity_type="stress_test",
            entity_id=uuid.uuid4(),
            before=large_dict,
            after=large_dict,
        )
        session.add(log)
        await session.flush()

        log_id = log.id
        session.expire_all()

        fetched = (
            await session.execute(sa.select(AuditLog).where(AuditLog.id == log_id))
        ).scalar_one()

        assert len(fetched.before) == 201
        assert len(fetched.before["large_array"]) == 500
        assert fetched.before["field_199"].startswith("value_199_")

    @pytest.mark.asyncio
    async def test_composite_index_exact_lookup(self, session: AsyncSession):
        """Test querying AuditLog by (entity_type, entity_id) matching the composite index."""
        target_entity_id = uuid.uuid4()
        other_entity_id = uuid.uuid4()

        log1 = AuditLog(
            action="action_1",
            entity_type="team_membership",
            entity_id=target_entity_id,
            before={"step": 1},
        )
        log2 = AuditLog(
            action="action_2",
            entity_type="team_membership",
            entity_id=target_entity_id,
            before={"step": 2},
        )
        log3 = AuditLog(
            action="action_3",
            entity_type="team_membership",
            entity_id=other_entity_id,
            before={"step": 3},
        )
        log4 = AuditLog(
            action="action_4",
            entity_type="player",
            entity_id=target_entity_id,
            before={"step": 4},
        )
        session.add_all([log1, log2, log3, log4])
        await session.flush()

        # Query exact match (entity_type, entity_id)
        stmt = (
            sa.select(AuditLog)
            .where(
                AuditLog.entity_type == "team_membership",
                AuditLog.entity_id == target_entity_id,
            )
            .order_by(AuditLog.created_at)
        )
        res = (await session.execute(stmt)).scalars().all()

        assert len(res) == 2
        actions = {r.action for r in res}
        assert actions == {"action_1", "action_2"}

    @pytest.mark.asyncio
    async def test_composite_index_prefix_lookup(self, session: AsyncSession):
        """Test querying AuditLog by entity_type only (utilizing composite index leading column)."""
        unique_type = f"type_{uuid.uuid4().hex[:8]}"

        for i in range(5):
            session.add(
                AuditLog(
                    action=f"batch_{i}",
                    entity_type=unique_type,
                    entity_id=uuid.uuid4(),
                )
            )
        await session.flush()

        stmt = sa.select(AuditLog).where(AuditLog.entity_type == unique_type)
        res = (await session.execute(stmt)).scalars().all()
        assert len(res) == 5

    @pytest.mark.asyncio
    async def test_composite_index_null_entity_id_lookup(self, session: AsyncSession):
        """Test querying records where entity_id IS NULL."""
        system_type = f"sys_{uuid.uuid4().hex[:8]}"
        log_null = AuditLog(action="tick", entity_type=system_type, entity_id=None)
        log_with_id = AuditLog(action="tick", entity_type=system_type, entity_id=uuid.uuid4())
        session.add_all([log_null, log_with_id])
        await session.flush()

        stmt = sa.select(AuditLog).where(
            AuditLog.entity_type == system_type,
            AuditLog.entity_id.is_(None),
        )
        res = (await session.execute(stmt)).scalars().all()
        assert len(res) == 1
        assert res[0].id == log_null.id

    @pytest.mark.asyncio
    async def test_explain_composite_index_eligibility(self, session: AsyncSession):
        """
        Verify that PostgreSQL optimizer considers audit_logs_entity_idx eligible.
        Force enable_seqscan = off in local transaction to verify index applicability.
        """
        target_uuid = uuid.uuid4()
        test_type = "index_probe"

        # Populate a few records
        for i in range(10):
            session.add(
                AuditLog(
                    action=f"probe_{i}",
                    entity_type=test_type,
                    entity_id=target_uuid if i < 3 else uuid.uuid4(),
                )
            )
        await session.flush()

        # Check EXPLAIN plan with enable_seqscan=off to verify index viability
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        explain_query = text(
            "EXPLAIN (FORMAT JSON) "
            "SELECT * FROM audit_logs WHERE entity_type = :etype AND entity_id = :eid"
        )
        result = await session.execute(explain_query, {"etype": test_type, "eid": target_uuid})
        raw_plan = result.scalar()

        # The JSON explain output contains the Plan node
        plan_str = str(raw_plan)
        assert (
            "audit_logs_entity_idx" in plan_str
            or "Index Scan" in plan_str
            or "Bitmap Index Scan" in plan_str
        )

    @pytest.mark.asyncio
    async def test_pglite_jsonb_path_and_containment_operators(self, session: AsyncSession):
        """
        Test native PostgreSQL JSONB operators in PGlite:
        - Path extraction (->>)
        - Containment (@>)
        """
        log1 = AuditLog(
            action="op_test",
            entity_type="operator_demo",
            entity_id=uuid.uuid4(),
            after={
                "role": "mid",
                "is_captain": True,
                "tags": ["competitive", "starter", "lck"],
                "metadata": {"source": "discord_bot", "version": 2},
            },
        )
        log2 = AuditLog(
            action="op_test",
            entity_type="operator_demo",
            entity_id=uuid.uuid4(),
            after={
                "role": "coach",
                "is_captain": False,
                "tags": ["staff", "lck"],
                "metadata": {"source": "web_admin", "version": 2},
            },
        )
        session.add_all([log1, log2])
        await session.flush()

        # 1. Path extraction query via SQLAlchemy
        stmt_path = sa.select(AuditLog).where(
            AuditLog.entity_type == "operator_demo",
            AuditLog.after["metadata"]["source"].astext == "discord_bot",
        )
        res_path = (await session.execute(stmt_path)).scalars().all()
        assert len(res_path) == 1
        assert res_path[0].id == log1.id

        # 2. Raw SQL JSONB containment operator (@>): after @> '{"role": "mid"}'
        raw_containment = text(
            "SELECT id FROM audit_logs "
            "WHERE entity_type = 'operator_demo' AND after @> '{\"role\": \"mid\"}'::jsonb"
        )
        res_cont = (await session.execute(raw_containment)).scalars().all()
        assert len(res_cont) == 1
        assert res_cont[0] == log1.id

        # 3. Raw SQL JSONB array containment: after @> '{"tags": ["competitive"]}'
        raw_arr_containment = text(
            "SELECT id FROM audit_logs "
            "WHERE entity_type = 'operator_demo' "
            'AND after @> \'{"tags": ["competitive"]}\'::jsonb'
        )
        res_arr = (await session.execute(raw_arr_containment)).scalars().all()
        assert len(res_arr) == 1
        assert res_arr[0] == log1.id


# ============================================================================
# 5. High-Volume Empirical Stress Harness
# ============================================================================


class TestHighVolumeEmpiricalStressHarness:
    """Stress-test cascade deletions, set-null integrity, and JSONB serialization under volume."""

    @pytest.mark.asyncio
    async def test_bulk_team_cascade_deletion_stress(self, session: AsyncSession):
        """
        Stress test:
        Create 20 Teams, each with 5 Memberships (1 captain, 4 starters) and 5 RosterMovements.
        Total: 20 teams, 100 memberships, 100 movements.
        Delete 10 teams in bulk via raw SQL.
        Verify:
        - 10 teams remaining.
        - Exactly 50 memberships remaining (all belonging to non-deleted teams).
        - Exactly 50 movements remaining (all belonging to non-deleted teams).
        """
        teams = []
        memberships = []
        movements = []
        users = []

        # Create 100 users
        for i in range(100):
            users.append(DiscordUser(discord_id=f"stress_u_{i:03d}", username=f"stress_user_{i}"))
        session.add_all(users)
        await session.flush()

        roles = [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
        ]

        # Create 20 teams
        for t_idx in range(20):
            t = Team(
                name=f"Stress Team {t_idx:02d}",
                tag=f"S{t_idx:02d}",
                slug=f"stress-team-{t_idx:02d}",
                division=Division.PREMIER if t_idx % 2 == 0 else Division.ASCEND,
                discord_role_id=990000 + t_idx,
            )
            teams.append(t)
            session.add(t)
            await session.flush()

            # 5 memberships per team
            for m_idx in range(5):
                u = users[t_idx * 5 + m_idx]
                m = TeamMembership(
                    team_id=t.id,
                    discord_user_id=u.discord_id,
                    role=roles[m_idx],
                    is_captain=(m_idx == 0),
                )
                mov = RosterMovement(
                    team_id=t.id,
                    discord_user_id=u.discord_id,
                    action=RosterMovementAction.JOINED,
                    role=roles[m_idx],
                )
                memberships.append(m)
                movements.append(mov)

        session.add_all(memberships + movements)
        await session.flush()

        # Delete first 10 teams via raw SQL
        deleted_team_ids = [t.id for t in teams[:10]]
        surviving_team_ids = [t.id for t in teams[10:]]

        await session.execute(
            text("DELETE FROM teams WHERE id = ANY(:tids)"),
            {"tids": deleted_team_ids},
        )
        await session.flush()
        session.expire_all()

        # Verify team count
        team_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM teams WHERE id = ANY(:tids)"), {"tids": deleted_team_ids}
            )
        ).scalar()
        assert team_count == 0

        surviving_team_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM teams WHERE id = ANY(:tids)"),
                {"tids": surviving_team_ids},
            )
        ).scalar()
        assert surviving_team_count == 10

        # Verify memberships
        deleted_m_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM team_memberships WHERE team_id = ANY(:tids)"),
                {"tids": deleted_team_ids},
            )
        ).scalar()
        assert deleted_m_count == 0

        surviving_m_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM team_memberships WHERE team_id = ANY(:tids)"),
                {"tids": surviving_team_ids},
            )
        ).scalar()
        assert surviving_m_count == 50

        # Verify movements
        deleted_mov_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM roster_movements WHERE team_id = ANY(:tids)"),
                {"tids": deleted_team_ids},
            )
        ).scalar()
        assert deleted_mov_count == 0

        surviving_mov_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM roster_movements WHERE team_id = ANY(:tids)"),
                {"tids": surviving_team_ids},
            )
        ).scalar()
        assert surviving_mov_count == 50

    @pytest.mark.asyncio
    async def test_bulk_user_set_null_and_cascade_stress(self, session: AsyncSession):
        """
        Stress test:
        Create 20 Users. Each has 2 linked Players and 3 AuditLogs.
        Delete 10 users in bulk via raw SQL.
        Verify:
        - Exactly 20 players have discord_user_id set to NULL.
        - Exactly 20 players still have their original discord_user_id intact.
        - Exactly 30 audit logs have actor_discord_user_id set to NULL.
        - Exactly 30 audit logs retain their actor_discord_user_id intact.
        """
        users = [
            DiscordUser(discord_id=f"bulk_usr_{i:02d}", username=f"bulk_user_{i}")
            for i in range(20)
        ]
        session.add_all(users)
        await session.flush()

        players = []
        logs = []
        for i, u in enumerate(users):
            p1 = Player(
                discord_user_id=u.discord_id, game_name=f"P_{i}_Main", riot_tag="EUW", is_main=True
            )
            p2 = Player(
                discord_user_id=u.discord_id,
                game_name=f"P_{i}_Smurf",
                riot_tag="EUW",
                is_main=False,
            )
            players.extend([p1, p2])

            for j in range(3):
                logs.append(
                    AuditLog(
                        actor_discord_user_id=u.discord_id,
                        action=f"action_{j}",
                        entity_type="stress_entity",
                    )
                )

        session.add_all(players + logs)
        await session.flush()

        del_user_ids = [u.discord_id for u in users[:10]]
        keep_user_ids = [u.discord_id for u in users[10:]]

        # Bulk delete 10 users via raw SQL
        await session.execute(
            text("DELETE FROM discord_users WHERE discord_id = ANY(:uids)"),
            {"uids": del_user_ids},
        )
        await session.flush()
        session.expire_all()

        # Count players with discord_user_id = NULL
        null_players = (
            await session.execute(
                text("SELECT COUNT(*) FROM players WHERE discord_user_id IS NULL")
            )
        ).scalar()
        assert null_players == 20

        # Count players with kept user ids
        kept_players = (
            await session.execute(
                text("SELECT COUNT(*) FROM players WHERE discord_user_id = ANY(:uids)"),
                {"uids": keep_user_ids},
            )
        ).scalar()
        assert kept_players == 20

        # Count audit logs with actor_discord_user_id = NULL
        null_logs = (
            await session.execute(
                text("SELECT COUNT(*) FROM audit_logs WHERE actor_discord_user_id IS NULL")
            )
        ).scalar()
        assert null_logs == 30

        # Count audit logs with kept user ids
        kept_logs = (
            await session.execute(
                text("SELECT COUNT(*) FROM audit_logs WHERE actor_discord_user_id = ANY(:uids)"),
                {"uids": keep_user_ids},
            )
        ).scalar()
        assert kept_logs == 30

    @pytest.mark.asyncio
    async def test_deep_jsonb_fuzzing_and_mutation_stress(self, session: AsyncSession):
        """Fuzz test JSONB column with diverse payloads and verify structural fidelity."""
        payloads = [
            {"simple_int": 1234567890123456789},
            {"float_precise": 0.00000000000123456},
            {"negative_float": -99999.88888},
            {"boolean_true": True, "boolean_false": False},
            {"null_val": None},
            {"empty_str": "", "whitespace_str": "   \t\n   "},
            {"special_quotes": "hello 'world' and \"universe\""},
            {"cjk_korean": "결승전 MVP 페이커"},
            {"cjk_japanese": "優勝チーム"},
            {"cjk_chinese": "全球总决赛冠军"},
            {"cyrillic": "Чемпионат мира по League of Legends"},
            {"arabic": "بطولة الدوري للأساطير"},
            {"hebrew": "אליפות ליגת האגדות"},
            {"accents_german": "Größenmaßstab für Käfer"},
            {"nested_lists": [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]},
            {"mixed_dict": {"a": [1, {"b": [2, {"c": 3}]}]}},
            {"array_of_nulls": [None, None, None]},
            {"array_of_bools": [True, False, True, False]},
            {"dict_with_numeric_keys_as_str": {"0": "zero", "1": "one", "100": "hundred"}},
            {
                "deep_branching": {
                    f"k_{i}": {f"sub_{j}": j * 2 for j in range(5)} for i in range(10)
                }
            },
        ]

        inserted_ids = []
        for idx, p in enumerate(payloads):
            log = AuditLog(
                action=f"fuzz_{idx}",
                entity_type="jsonb_fuzz",
                entity_id=uuid.uuid4(),
                before=p,
                after=p,
            )
            session.add(log)
            await session.flush()
            inserted_ids.append((log.id, p))

        session.expire_all()

        for log_id, expected_payload in inserted_ids:
            fetched = (
                await session.execute(sa.select(AuditLog).where(AuditLog.id == log_id))
            ).scalar_one()
            assert fetched.before == expected_payload
            assert fetched.after == expected_payload
