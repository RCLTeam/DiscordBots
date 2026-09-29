"""Unit and integration tests for CasterRole, MatchCaster, and MatchCasterCard models."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alembic import command
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
    """Crea dos equipos de prueba para asociar a los partidos."""
    t1 = Team(
        name="Team KOI",
        tag="KOI",
        slug="team-koi",
        division=Division.PREMIER,
        discord_role_id=111111111111111111,
    )
    t2 = Team(
        name="Team Heretics",
        tag="HRT",
        slug="team-heretics",
        division=Division.PREMIER,
        discord_role_id=222222222222222222,
    )
    session.add_all([t1, t2])
    await session.flush()
    return t1, t2


@pytest.fixture
async def sample_match(session: AsyncSession, sample_teams: tuple[Team, Team]) -> Match:
    """Crea un partido de prueba persistido."""
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


class TestCasterRoleEnum:
    """Pruebas para el enum CasterRole."""

    def test_caster_role_values(self):
        """Verifica que los valores de CasterRole coincidan con los requisitos del dominio."""
        assert CasterRole.CASTER.value == "CASTER"
        assert CasterRole.STREAMER.value == "STREAMER"
        assert CasterRole.BOTH.value == "BOTH"
        assert len(CasterRole) == 3


class TestMatchCasterModel:
    """Pruebas para el modelo declarativo MatchCaster y sus restricciones de integridad."""

    @pytest.mark.asyncio
    async def test_create_match_caster_success(self, session: AsyncSession, sample_match: Match):
        """Verifica la persistencia de un MatchCaster con UUID, timestamps y clave foránea."""
        caster = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=123456789012345678,
            caster_role=CasterRole.CASTER,
        )
        session.add(caster)
        await session.flush()

        assert isinstance(caster.id, uuid.UUID)
        assert caster.match_id == sample_match.id
        assert caster.discord_user_id == 123456789012345678
        assert caster.caster_role == CasterRole.CASTER
        assert isinstance(caster.created_at, datetime)
        assert isinstance(caster.updated_at, datetime)
        assert "MatchCaster" in repr(caster)

    @pytest.mark.asyncio
    async def test_multiple_casters_allowed_on_same_match(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que múltiples usuarios puedan asignarse como CASTER en el mismo partido."""
        c1 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.CASTER,
        )
        c2 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000002,
            caster_role=CasterRole.CASTER,
        )
        c3 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000003,
            caster_role=CasterRole.CASTER,
        )
        session.add_all([c1, c2, c3])
        await session.flush()

        result = await session.execute(
            select(MatchCaster).where(MatchCaster.match_id == sample_match.id)
        )
        casters = result.scalars().all()
        assert len(casters) == 3

    @pytest.mark.asyncio
    async def test_single_streamer_enforced_by_database_streamer_role(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que el índice único parcial prohíba un segundo streamer con rol STREAMER."""
        s1 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.STREAMER,
        )
        session.add(s1)
        await session.flush()

        s2 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000002,
            caster_role=CasterRole.STREAMER,
        )
        session.add(s2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_single_streamer" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_single_streamer_enforced_by_database_both_role(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que el índice único parcial prohíba combinar STREAMER y BOTH en un partido."""
        s1 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.STREAMER,
        )
        session.add(s1)
        await session.flush()

        s2 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000002,
            caster_role=CasterRole.BOTH,
        )
        session.add(s2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_single_streamer" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_single_streamer_enforced_two_both_roles(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que no se puedan asignar dos usuarios con rol BOTH al mismo partido."""
        s1 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.BOTH,
        )
        session.add(s1)
        await session.flush()

        s2 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000002,
            caster_role=CasterRole.BOTH,
        )
        session.add(s2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_single_streamer" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_streamer_and_caster_coexist_on_same_match(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que un STREAMER o BOTH pueda coexistir con uno o más CASTERS."""
        streamer = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.STREAMER,
        )
        caster = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000002,
            caster_role=CasterRole.CASTER,
        )
        session.add_all([streamer, caster])
        await session.flush()

        result = await session.execute(
            select(MatchCaster).where(MatchCaster.match_id == sample_match.id)
        )
        rows = result.scalars().all()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_unique_match_user_constraint(self, session: AsyncSession, sample_match: Match):
        """Verifica que el mismo usuario no pueda tener dos registros en el mismo partido."""
        c1 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.CASTER,
        )
        session.add(c1)
        await session.flush()

        c2 = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.CASTER,
        )
        session.add(c2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_casters_match_user" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_same_user_can_stream_different_matches(
        self,
        session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """Verifica que la exclusividad de streamer sea por match_id, no global."""
        t1, t2 = sample_teams
        m1 = Match(jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        session.add(m1)
        await session.flush()

        m2 = Match(jornada=2, division=Division.PREMIER, team1_id=t2.id, team2_id=t1.id)
        session.add(m2)
        await session.flush()

        user_id = 999999999999999999
        s1 = MatchCaster(match_id=m1.id, discord_user_id=user_id, caster_role=CasterRole.STREAMER)
        s2 = MatchCaster(match_id=m2.id, discord_user_id=user_id, caster_role=CasterRole.STREAMER)
        session.add_all([s1, s2])
        await session.flush()

        res1 = await session.execute(select(MatchCaster).where(MatchCaster.match_id == m1.id))
        res2 = await session.execute(select(MatchCaster).where(MatchCaster.match_id == m2.id))
        assert len(res1.scalars().all()) == 1
        assert len(res2.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_cascade_delete_on_match_removal(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que al eliminar un partido, sus casters se borren automáticamente en cascada."""
        caster = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.CASTER,
        )
        session.add(caster)
        await session.flush()
        caster_id = caster.id

        await session.delete(sample_match)
        await session.flush()

        result = await session.execute(
            select(MatchCaster).where(MatchCaster.id == caster_id)
        )
        assert result.scalar_one_or_none() is None


class TestMatchCasterCardModel:
    """Pruebas para el modelo declarativo MatchCasterCard."""

    @pytest.mark.asyncio
    async def test_create_match_caster_card_success(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica la persistencia de una tarjeta publicada con match_id,
        channel_id y message_id.
        """
        card = MatchCasterCard(
            match_id=sample_match.id,
            channel_id=1550210628361392278,
            message_id=987654321098765432,
        )
        session.add(card)
        await session.flush()

        assert isinstance(card.id, uuid.UUID)
        assert card.match_id == sample_match.id
        assert card.channel_id == 1550210628361392278
        assert card.message_id == 987654321098765432
        assert isinstance(card.created_at, datetime)
        assert isinstance(card.updated_at, datetime)
        assert "MatchCasterCard" in repr(card)

    @pytest.mark.asyncio
    async def test_unique_match_channel_constraint(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que no se puedan crear dos tarjetas para el mismo partido en el mismo canal."""
        card1 = MatchCasterCard(
            match_id=sample_match.id,
            channel_id=1550210628361392278,
            message_id=111111111111111111,
        )
        session.add(card1)
        await session.flush()

        card2 = MatchCasterCard(
            match_id=sample_match.id,
            channel_id=1550210628361392278,
            message_id=222222222222222222,
        )
        session.add(card2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "uq_match_caster_cards_match_channel" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_same_match_different_channels_allowed(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que un partido pueda tener tarjetas en canales diferentes."""
        card1 = MatchCasterCard(
            match_id=sample_match.id,
            channel_id=111111111111111111,
            message_id=333333333333333333,
        )
        card2 = MatchCasterCard(
            match_id=sample_match.id,
            channel_id=222222222222222222,
            message_id=444444444444444444,
        )
        session.add_all([card1, card2])
        await session.flush()

        result = await session.execute(
            select(MatchCasterCard).where(MatchCasterCard.match_id == sample_match.id)
        )
        cards = result.scalars().all()
        assert len(cards) == 2

    @pytest.mark.asyncio
    async def test_cascade_delete_on_match_removal(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que al eliminar un partido, sus tarjetas se borren
        automáticamente en cascada.
        """
        card = MatchCasterCard(
            match_id=sample_match.id,
            channel_id=1550210628361392278,
            message_id=987654321098765432,
        )
        session.add(card)
        await session.flush()
        card_id = card.id

        await session.delete(sample_match)
        await session.flush()

        result = await session.execute(
            select(MatchCasterCard).where(MatchCasterCard.id == card_id)
        )
        assert result.scalar_one_or_none() is None


class TestMatchRelationshipsWithCasters:
    """Pruebas de relaciones bidireccionales entre Match, MatchCaster y MatchCasterCard."""

    @pytest.mark.asyncio
    async def test_match_casters_relationship_eager_loaded(
        self, session: AsyncSession, sample_match: Match
    ):
        """Verifica que match.casters y match.caster_cards se carguen con selectin."""
        caster = MatchCaster(
            match_id=sample_match.id,
            discord_user_id=100000000000000001,
            caster_role=CasterRole.BOTH,
        )
        card = MatchCasterCard(
            match_id=sample_match.id,
            channel_id=1550210628361392278,
            message_id=987654321098765432,
        )
        session.add_all([caster, card])
        await session.flush()

        # Reconsultar match con sus relaciones
        result = await session.execute(
            select(Match).where(Match.id == sample_match.id)
        )
        m = result.scalar_one()

        assert len(m.casters) == 1
        assert m.casters[0].discord_user_id == 100000000000000001
        assert m.casters[0].caster_role == CasterRole.BOTH
        assert m.casters[0].match.id == sample_match.id

        assert len(m.caster_cards) == 1
        assert m.caster_cards[0].channel_id == 1550210628361392278
        assert m.caster_cards[0].match.id == sample_match.id

    @pytest.mark.asyncio
    async def test_session_factory_fixture(
        self, session_factory: async_sessionmaker[AsyncSession]
    ):
        """Verifica que la fixture compartida session_factory instancie sesiones operativas."""
        async with session_factory() as sess:
            result = await sess.execute(select(MatchCaster).limit(1))
            assert result is not None

    @pytest.mark.asyncio
    async def test_alembic_migration_0003_reversibility(
        self, migrated_db: AsyncEngine
    ):
        """Verifica la reversibilidad limpia de la migración 0003_match_casters
        (downgrade a 0002 y re-upgrade a head).
        """
        cfg = Config("alembic.ini")
        async with migrated_db.connect() as conn:

            def do_migration_cycle(sync_conn):
                cfg.attributes["connection"] = sync_conn
                # Revertir migración 0003 a 0002
                command.downgrade(cfg, "0002")
                tables_after_downgrade = inspect(sync_conn).get_table_names()
                assert "match_casters" not in tables_after_downgrade
                assert "match_caster_cards" not in tables_after_downgrade

                # Re-aplicar migración 0003 hasta head
                command.upgrade(cfg, "head")
                tables_after_upgrade = inspect(sync_conn).get_table_names()
                assert "match_casters" in tables_after_upgrade
                assert "match_caster_cards" in tables_after_upgrade

            await conn.run_sync(do_migration_cycle)
            await conn.commit()
