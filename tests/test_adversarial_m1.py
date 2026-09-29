"""Empirical adversarial test suite for Milestone 1.

Testing concurrency, race conditions, boundary values, timezones, and cascade
delete under transaction rollback vs commit.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models import (
    CasterRole,
    Division,
    Match,
    MatchCaster,
    MatchCasterCard,
    Team,
)


@pytest.fixture
async def sample_teams(session: AsyncSession) -> tuple[Team, Team]:
    """Crea dos equipos de prueba con slugs únicos aislados por test."""
    u = uuid.uuid4().hex[:8]
    t1 = Team(
        name=f"Team KOI {u}",
        tag=f"K{u[:3].upper()}",
        slug=f"team-koi-{u}",
        division=Division.PREMIER,
        discord_role_id=int(f"111111{int(u[:6], 16) % 1000000000:09d}"),
    )
    t2 = Team(
        name=f"Team Heretics {u}",
        tag=f"H{u[3:6].upper()}",
        slug=f"team-heretics-{u}",
        division=Division.PREMIER,
        discord_role_id=int(f"222222{int(u[2:8], 16) % 1000000000:09d}"),
    )
    session.add_all([t1, t2])
    await session.flush()
    return t1, t2


@pytest.fixture
async def sample_match(session: AsyncSession, sample_teams: tuple[Team, Team]) -> Match:
    """Crea un partido de prueba dentro de la transacción de prueba."""
    t1, t2 = sample_teams
    match = Match(
        jornada=1,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
    )
    session.add(match)
    await session.flush()
    return match


class TestConcurrencyAndRaceConditions:
    """Challenge 1: Concurrency and Race Conditions at DB Level."""

    @pytest.mark.asyncio
    async def test_race_streamer_vs_streamer(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Two transactions attempting to assign STREAMER for the same match.
        Tx1 commits savepoint; Tx2 MUST fail with IntegrityError on flush.
        """
        match_id = sample_match.id

        sp1 = await session.begin_nested()
        c1 = MatchCaster(
            match_id=match_id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.STREAMER,
        )
        session.add(c1)
        await session.flush()

        sp2 = await session.begin_nested()
        c2 = MatchCaster(
            match_id=match_id,
            discord_user_id=100000000000000002,
            caster_role=CasterRole.STREAMER,
        )
        session.add(c2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_single_streamer" in str(exc_info.value).lower()
        await sp2.rollback()

        await sp1.commit()

        # Verify only c1 exists
        res = await session.execute(select(MatchCaster).where(MatchCaster.match_id == match_id))
        rows = res.scalars().all()
        assert len(rows) == 1
        assert rows[0].discord_user_id == 100000000000000001

    @pytest.mark.asyncio
    async def test_race_streamer_vs_both(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Tx1 assigns STREAMER; concurrent Tx2 attempts to assign BOTH.
        Tx2 MUST fail with IntegrityError.
        """
        match_id = sample_match.id

        sp1 = await session.begin_nested()
        c1 = MatchCaster(
            match_id=match_id,
            discord_user_id=200000000000000001,
            caster_role=CasterRole.STREAMER,
        )
        session.add(c1)
        await session.flush()

        sp2 = await session.begin_nested()
        c2 = MatchCaster(
            match_id=match_id,
            discord_user_id=200000000000000002,
            caster_role=CasterRole.BOTH,
        )
        session.add(c2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_single_streamer" in str(exc_info.value).lower()
        await sp2.rollback()
        await sp1.commit()

    @pytest.mark.asyncio
    async def test_race_both_vs_both(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Tx1 assigns BOTH; concurrent Tx2 attempts to assign BOTH.
        Tx2 MUST fail with IntegrityError.
        """
        match_id = sample_match.id

        sp1 = await session.begin_nested()
        c1 = MatchCaster(
            match_id=match_id,
            discord_user_id=300000000000000001,
            caster_role=CasterRole.BOTH,
        )
        session.add(c1)
        await session.flush()

        sp2 = await session.begin_nested()
        c2 = MatchCaster(
            match_id=match_id,
            discord_user_id=300000000000000002,
            caster_role=CasterRole.BOTH,
        )
        session.add(c2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_single_streamer" in str(exc_info.value).lower()
        await sp2.rollback()
        await sp1.commit()

    @pytest.mark.asyncio
    async def test_race_rollback_of_streamer_allows_subsequent_streamer(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Tx1 attempts STREAMER but rolls back; Tx2 attempts STREAMER and commits.
        Tx2 MUST succeed cleanly.
        """
        match_id = sample_match.id

        sp1 = await session.begin_nested()
        c1 = MatchCaster(
            match_id=match_id,
            discord_user_id=400000000000000001,
            caster_role=CasterRole.STREAMER,
        )
        session.add(c1)
        await session.flush()
        # Tx1 rolls back
        await sp1.rollback()

        # Tx2 now attempts
        sp2 = await session.begin_nested()
        c2 = MatchCaster(
            match_id=match_id,
            discord_user_id=400000000000000002,
            caster_role=CasterRole.STREAMER,
        )
        session.add(c2)
        await session.flush()
        await sp2.commit()

        res = await session.execute(select(MatchCaster).where(MatchCaster.match_id == match_id))
        rows = res.scalars().all()
        assert len(rows) == 1
        assert rows[0].discord_user_id == 400000000000000002

    @pytest.mark.asyncio
    async def test_concurrent_multiple_casters_all_succeed(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Multiple concurrent transactions inserting CASTER roles for different users.
        None must be blocked or fail.
        """
        match_id = sample_match.id

        uids = [
            500000000000000001,
            500000000000000002,
            500000000000000003,
            500000000000000004,
        ]
        for uid in uids:
            sp = await session.begin_nested()
            caster = MatchCaster(
                match_id=match_id,
                discord_user_id=uid,
                caster_role=CasterRole.CASTER,
            )
            session.add(caster)
            await session.flush()
            await sp.commit()

        res = await session.execute(select(MatchCaster).where(MatchCaster.match_id == match_id))
        rows = res.scalars().all()
        assert len(rows) == 4

    @pytest.mark.asyncio
    async def test_race_same_user_same_match(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Two transactions attempting to add the same user to the same match.
        Tx2 MUST fail with uq_match_casters_match_user.
        """
        match_id = sample_match.id
        same_user = 600000000000000001

        sp1 = await session.begin_nested()
        c1 = MatchCaster(
            match_id=match_id,
            discord_user_id=same_user,
            caster_role=CasterRole.CASTER,
        )
        session.add(c1)
        await session.flush()

        sp2 = await session.begin_nested()
        c2 = MatchCaster(
            match_id=match_id,
            discord_user_id=same_user,
            caster_role=CasterRole.CASTER,
        )
        session.add(c2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_match_user" in str(exc_info.value).lower()
        await sp2.rollback()
        await sp1.commit()

    @pytest.mark.asyncio
    async def test_race_card_duplicate_channel(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Two transactions creating a card for the same match and channel.
        Tx2 MUST fail with uq_match_caster_cards_match_channel.
        """
        match_id = sample_match.id
        channel_id = 1550210628361392278

        sp1 = await session.begin_nested()
        card1 = MatchCasterCard(
            match_id=match_id,
            channel_id=channel_id,
            message_id=700000000000000001,
        )
        session.add(card1)
        await session.flush()

        sp2 = await session.begin_nested()
        card2 = MatchCasterCard(
            match_id=match_id,
            channel_id=channel_id,
            message_id=700000000000000002,
        )
        session.add(card2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_caster_cards_match_channel" in str(exc_info.value).lower()
        await sp2.rollback()
        await sp1.commit()


class TestBoundaryCases:
    """Challenge 2: Boundary cases - UUID formats, BigInteger, Timezones, Nullability."""

    @pytest.mark.asyncio
    async def test_uuid_variants(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Verify support for nil UUID and UUID1."""
        nil_uuid = uuid.UUID("00000000-0000-0000-0000-000000000000")
        c_nil = MatchCaster(
            id=nil_uuid,
            match_id=sample_match.id,
            discord_user_id=800000000000000001,
            caster_role=CasterRole.CASTER,
        )
        uuid1 = uuid.uuid1()
        c_uuid1 = MatchCaster(
            id=uuid1,
            match_id=sample_match.id,
            discord_user_id=800000000000000002,
            caster_role=CasterRole.CASTER,
        )
        session.add_all([c_nil, c_uuid1])
        await session.flush()

        assert c_nil.id == nil_uuid
        assert c_uuid1.id == uuid1

    @pytest.mark.asyncio
    async def test_invalid_foreign_key_match_id(
        self,
        session: AsyncSession,
    ):
        """Non-existent match_id must fail foreign key constraint."""
        ghost_match_id = uuid.uuid4()
        c = MatchCaster(
            match_id=ghost_match_id,
            discord_user_id=800000000000000003,
            caster_role=CasterRole.CASTER,
        )
        session.add(c)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "foreign key" in str(exc_info.value).lower() or "fk_" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_nullability_constraints(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Fields marked nullable=False must be rejected if None."""
        # 1. match_id is None
        sp = await session.begin_nested()
        c_no_match = MatchCaster(
            match_id=None,  # type: ignore
            discord_user_id=800000000000000004,
            caster_role=CasterRole.CASTER,
        )
        session.add(c_no_match)
        with pytest.raises(IntegrityError):
            await session.flush()
        await sp.rollback()

        # 2. discord_user_id is None
        sp = await session.begin_nested()
        c_no_user = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=None,  # type: ignore
            caster_role=CasterRole.CASTER,
        )
        session.add(c_no_user)
        with pytest.raises(IntegrityError):
            await session.flush()
        await sp.rollback()

        # 3. caster_role is None
        sp = await session.begin_nested()
        c_no_role = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=800000000000000005,
            caster_role=None,  # type: ignore
        )
        session.add(c_no_role)
        with pytest.raises(IntegrityError):
            await session.flush()
        await sp.rollback()

    @pytest.mark.asyncio
    async def test_biginteger_boundary_values(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Verify BigInteger handles values > 2^53 (JS safe int) up to 2^63-1."""
        js_safe_max = 2**53 - 1  # 9007199254740991
        js_safe_plus_1 = 2**53  # 9007199254740992
        real_discord_snowflake = 1550210628361392278
        max_int64 = 2**63 - 1  # 9223372036854775807

        c1 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=js_safe_max,
            caster_role=CasterRole.CASTER,
        )
        c2 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=js_safe_plus_1,
            caster_role=CasterRole.CASTER,
        )
        c3 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=real_discord_snowflake,
            caster_role=CasterRole.CASTER,
        )
        c4 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=max_int64,
            caster_role=CasterRole.CASTER,
        )
        session.add_all([c1, c2, c3, c4])
        await session.flush()

        res = await session.execute(
            select(MatchCaster).where(MatchCaster.match_id == sample_match.id)
        )
        casters = {c.discord_user_id: c for c in res.scalars().all()}
        assert casters[js_safe_max].discord_user_id == 9007199254740991
        assert casters[js_safe_plus_1].discord_user_id == 9007199254740992
        assert casters[real_discord_snowflake].discord_user_id == 1550210628361392278
        assert casters[max_int64].discord_user_id == 9223372036854775807

    @pytest.mark.asyncio
    async def test_biginteger_overflow_beyond_signed_64bit(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Values exceeding signed 64-bit integer (> 2^63-1) must be rejected by PostgreSQL."""
        overflow_val = 2**63  # 9223372036854775808
        c = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=overflow_val,
            caster_role=CasterRole.CASTER,
        )
        session.add(c)
        with pytest.raises((DataError, DBAPIError, OverflowError)) as exc_info:
            await session.flush()
        err_str = str(exc_info.value).lower()
        assert "out of range" in err_str or "overflow" in err_str

    @pytest.mark.asyncio
    async def test_timezone_awareness_and_accuracy(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Verify that created_at and updated_at are tz-aware (UTC) and accurate."""
        caster = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=850000000000000001,
            caster_role=CasterRole.CASTER,
        )
        session.add(caster)
        await session.flush()

        res = await session.execute(select(MatchCaster).where(MatchCaster.id == caster.id))
        refetched = res.scalar_one()

        assert refetched.created_at is not None
        assert refetched.created_at.tzinfo is not None
        assert refetched.updated_at is not None
        assert refetched.updated_at.tzinfo is not None

        now_utc = datetime.now(timezone.utc)
        diff_created = abs((now_utc - refetched.created_at).total_seconds())
        assert diff_created < 10.0, f"created_at {refetched.created_at} too far from {now_utc}"


class TestCascadeDeleteRollbackVsCommit:
    """Challenge 3: Cascade Delete Behavior Under Rollback vs Commit."""

    @pytest.mark.asyncio
    async def test_cascade_delete_rollback_preserves_records(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """If a match deletion is rolled back, all casters and cards must be preserved."""
        match_id = sample_match.id

        caster = MatchCaster(
            match_id=match_id,
            discord_user_id=900000000000000001,
            caster_role=CasterRole.BOTH,
        )
        card = MatchCasterCard(
            match_id=match_id,
            channel_id=1550210628361392278,
            message_id=900000000000000002,
        )
        session.add_all([caster, card])
        await session.flush()
        caster_id = caster.id
        card_id = card.id

        # Enter savepoint, delete match, then roll back
        sp = await session.begin_nested()
        await session.delete(sample_match)
        await session.flush()
        # Verify in savepoint they are deleted or pending delete
        await sp.rollback()

        # Verify outside savepoint that match, caster, and card are intact
        res_m = await session.execute(select(Match).where(Match.id == match_id))
        assert res_m.scalar_one_or_none() is not None

        res_c = await session.execute(select(MatchCaster).where(MatchCaster.id == caster_id))
        assert res_c.scalar_one_or_none() is not None

        res_card = await session.execute(
            select(MatchCasterCard).where(MatchCasterCard.id == card_id)
        )
        assert res_card.scalar_one_or_none() is not None

    @pytest.mark.asyncio
    async def test_cascade_delete_commit_deletes_all_associated_records(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """When a match deletion is executed and flushed, all casters and cards are purged."""
        match_id = sample_match.id

        caster = MatchCaster(
            match_id=match_id,
            discord_user_id=910000000000000001,
            caster_role=CasterRole.STREAMER,
        )
        card = MatchCasterCard(
            match_id=match_id,
            channel_id=1550210628361392278,
            message_id=910000000000000002,
        )
        session.add_all([caster, card])
        await session.flush()
        caster_id = caster.id
        card_id = card.id

        await session.delete(sample_match)
        await session.flush()

        res_m = await session.execute(select(Match).where(Match.id == match_id))
        assert res_m.scalar_one_or_none() is None

        res_c = await session.execute(select(MatchCaster).where(MatchCaster.id == caster_id))
        assert res_c.scalar_one_or_none() is None

        res_card = await session.execute(
            select(MatchCasterCard).where(MatchCasterCard.id == card_id)
        )
        assert res_card.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_delete_caster_leaves_match_intact(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Deleting a MatchCaster directly MUST NOT delete the Match."""
        match_id = sample_match.id

        caster = MatchCaster(
            match_id=match_id,
            discord_user_id=920000000000000001,
            caster_role=CasterRole.CASTER,
        )
        session.add(caster)
        await session.flush()
        caster_id = caster.id

        await session.delete(caster)
        await session.flush()

        res_m = await session.execute(select(Match).where(Match.id == match_id))
        assert res_m.scalar_one_or_none() is not None

        res_c = await session.execute(select(MatchCaster).where(MatchCaster.id == caster_id))
        assert res_c.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_delete_card_leaves_match_intact(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Deleting a MatchCasterCard directly MUST NOT delete the Match."""
        match_id = sample_match.id

        card = MatchCasterCard(
            match_id=match_id,
            channel_id=1550210628361392278,
            message_id=930000000000000001,
        )
        session.add(card)
        await session.flush()
        card_id = card.id

        await session.delete(card)
        await session.flush()

        res_m = await session.execute(select(Match).where(Match.id == match_id))
        assert res_m.scalar_one_or_none() is not None

        res_card = await session.execute(
            select(MatchCasterCard).where(MatchCasterCard.id == card_id)
        )
        assert res_card.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_orm_orphan_removal_on_casters_clear(
        self,
        session: AsyncSession,
        sample_match: Match,
    ):
        """Test cascade='all, delete-orphan' when casters list is cleared via relationship."""
        match_id = sample_match.id

        c1 = MatchCaster(
            match_id=match_id,
            discord_user_id=940000000000000001,
            caster_role=CasterRole.CASTER,
        )
        c2 = MatchCaster(
            match_id=match_id,
            discord_user_id=940000000000000002,
            caster_role=CasterRole.STREAMER,
        )
        session.add_all([c1, c2])
        await session.flush()
        c1_id = c1.id
        c2_id = c2.id

        res = await session.execute(select(Match).where(Match.id == match_id))
        match_obj = res.scalar_one()
        assert len(match_obj.casters) == 2

        # Clear casters collection
        match_obj.casters.clear()
        await session.flush()

        # Check orphans were deleted
        res1 = await session.execute(select(MatchCaster).where(MatchCaster.id == c1_id))
        res2 = await session.execute(select(MatchCaster).where(MatchCaster.id == c2_id))
        assert res1.scalar_one_or_none() is None
        assert res2.scalar_one_or_none() is None
