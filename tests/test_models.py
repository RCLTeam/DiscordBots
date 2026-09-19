"""
Pruebas unitarias para los modelos declarativos de SQLAlchemy 2.0 en src/liga_bot/models/.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from liga_bot.models import Base, Division, Match, MatchStatus, Team, TicketNotice


@pytest_asyncio.fixture
async def session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Proporciona una AsyncSession aislada por test con rollback automático."""
    async with migrated_db.connect() as conn:
        trans = await conn.begin()
        async_session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            await trans.rollback()


class TestMetadataAndParity:
    """Verifica el registro de metadatos y la paridad exacta con las migraciones de Alembic."""

    def test_metadata_table_registry(self):
        """Verifica que Base.metadata contenga exactamente las tablas esperadas."""
        expected_tables = {"teams", "matches", "ticket_notices", "role_requests"}
        assert expected_tables.issubset(set(Base.metadata.tables.keys()))

    @pytest.mark.asyncio
    async def test_alembic_schema_diff_is_empty(self, migrated_db: AsyncEngine):
        """Valida que no exista divergencia (diff vacío) entre los modelos y la base migrada."""
        async with migrated_db.connect() as conn:

            def do_compare(sync_conn):
                mc = MigrationContext.configure(
                    sync_conn,
                    opts={"compare_type": True, "compare_server_default": True},
                )
                return compare_metadata(mc, Base.metadata)

            diff = await conn.run_sync(do_compare)
            assert diff == []


class TestTeamModel:
    """Pruebas unitarias para el modelo Team."""

    @pytest.mark.asyncio
    async def test_create_team_defaults_and_fields(self, session: AsyncSession):
        """Verifica la creación de un equipo, generación de UUID y campos."""
        team = Team(
            name="KOI Squad",
            tag="KOI",
            slug="koi-squad",
            division=Division.PREMIER,
            discord_role_id=123456789012345678,
        )
        session.add(team)
        await session.flush()

        assert isinstance(team.id, uuid.UUID)
        assert team.name == "KOI Squad"
        assert team.tag == "KOI"
        assert team.slug == "koi-squad"
        assert team.division == Division.PREMIER
        assert team.discord_role_id == 123456789012345678
        assert isinstance(team.created_at, datetime)
        assert "KOI Squad" in repr(team)

    @pytest.mark.asyncio
    async def test_unique_team_name_constraint(self, session: AsyncSession):
        """Verifica que no se permitan dos equipos con el mismo nombre."""
        team1 = Team(
            name="Fnatic",
            tag="FNC",
            slug="fnatic",
            division=Division.PREMIER,
            discord_role_id=111111111111111111,
        )
        session.add(team1)
        await session.flush()

        team2 = Team(
            name="Fnatic",
            tag="FNC2",
            slug="fnatic-2",
            division=Division.PREMIER,
            discord_role_id=222222222222222222,
        )
        session.add(team2)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.flush()

    @pytest.mark.asyncio
    async def test_unique_team_role_id_constraint(self, session: AsyncSession):
        """Verifica que no se permitan dos equipos con el mismo discord_role_id."""
        team1 = Team(
            name="G2 Esports",
            tag="G2",
            slug="g2-esports",
            division=Division.PREMIER,
            discord_role_id=333333333333333333,
        )
        session.add(team1)
        await session.flush()

        team2 = Team(
            name="G2 Academy",
            tag="G2A",
            slug="g2-academy",
            division=Division.ASCEND,
            discord_role_id=333333333333333333,
        )
        session.add(team2)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.flush()

    @pytest.mark.asyncio
    async def test_tag_length_constraint_rejection(self, session: AsyncSession):
        """Verifica que un tag con más de 4 caracteres sea rechazado por el CheckConstraint/DB."""
        team = Team(
            name="Giantx",
            tag="GIANTX",  # 6 caracteres, supera el límite de 4
            slug="giantx",
            division=Division.PREMIER,
            discord_role_id=444444444444444444,
        )
        session.add(team)
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin_nested():
                await session.flush()

    @pytest.mark.asyncio
    async def test_invalid_division_enum_rejection(self, session: AsyncSession):
        """Verifica que un valor no válido para la división sea rechazado."""
        with pytest.raises((ValueError, LookupError)):
            Division("INVALID_DIVISION")


class TestMatchModel:
    """Pruebas unitarias para el modelo Match."""

    @pytest.mark.asyncio
    async def test_create_match_defaults(self, session: AsyncSession):
        """Verifica la creación de un partido con valores por defecto."""
        t1 = Team(
            name="Heretics",
            tag="TH",
            slug="heretics",
            division=Division.PREMIER,
            discord_role_id=555555555555555555,
        )
        t2 = Team(
            name="Movistar Riders",
            tag="MRS",
            slug="movistar-riders",
            division=Division.PREMIER,
            discord_role_id=666666666666666666,
        )
        session.add_all([t1, t2])
        await session.flush()

        match = Match(
            jornada=1,
            division=Division.PREMIER,
            team1_id=t1.id,
            team2_id=t2.id,
        )
        session.add(match)
        await session.flush()

        assert isinstance(match.id, uuid.UUID)
        assert match.status == MatchStatus.PENDIENTE
        assert match.discord_channel_id is None
        assert match.scheduled_at is None
        assert isinstance(match.created_at, datetime)
        assert "Match" in repr(match)

    @pytest.mark.asyncio
    async def test_relationship_bidirectionality_and_eager_load(self, session: AsyncSession):
        """Valida que las relaciones bidireccionales con Team funcionen de forma eager."""
        t1 = Team(
            name="MAD Lions",
            tag="MAD",
            slug="mad-lions",
            division=Division.PREMIER,
            discord_role_id=777777777777777777,
        )
        t2 = Team(
            name="Rogue",
            tag="RGE",
            slug="rogue",
            division=Division.PREMIER,
            discord_role_id=888888888888888888,
        )
        session.add_all([t1, t2])
        await session.flush()

        match = Match(
            jornada=2,
            division=Division.PREMIER,
            team1_id=t1.id,
            team2_id=t2.id,
        )
        session.add(match)
        await session.flush()

        # Consultar el partido y acceder a team1 y team2 sin MissingGreenlet
        result = await session.execute(select(Match).where(Match.id == match.id))
        loaded_match = result.scalar_one()

        assert loaded_match.team1.name == "MAD Lions"
        assert loaded_match.team2.name == "Rogue"
        await session.refresh(t1, ["home_matches"])
        await session.refresh(t2, ["away_matches"])
        assert loaded_match in t1.home_matches
        assert loaded_match in t2.away_matches

    @pytest.mark.asyncio
    async def test_unique_match_per_jornada_constraint(self, session: AsyncSession):
        """Verifica que no se permitan partidos duplicados en una jornada."""
        t1 = Team(
            name="Team Queso",
            tag="TQ",
            slug="team-queso",
            division=Division.ASCEND,
            discord_role_id=999999999999999991,
        )
        t2 = Team(
            name="UCAM",
            tag="UCAM",
            slug="ucam",
            division=Division.ASCEND,
            discord_role_id=999999999999999992,
        )
        session.add_all([t1, t2])
        await session.flush()

        m1 = Match(jornada=1, division=Division.ASCEND, team1_id=t1.id, team2_id=t2.id)
        session.add(m1)
        await session.flush()

        m2 = Match(jornada=1, division=Division.ASCEND, team1_id=t1.id, team2_id=t2.id)
        session.add(m2)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.flush()

    @pytest.mark.asyncio
    async def test_distinct_teams_check_constraint(self, session: AsyncSession):
        """Verifica que el CheckConstraint impida que un equipo juegue contra sí mismo."""
        t1 = Team(
            name="Rebels Gaming",
            tag="RBLS",
            slug="rebels-gaming",
            division=Division.PREMIER,
            discord_role_id=999999999999999993,
        )
        session.add(t1)
        await session.flush()

        match = Match(jornada=3, division=Division.PREMIER, team1_id=t1.id, team2_id=t1.id)
        session.add(match)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.flush()

    @pytest.mark.asyncio
    async def test_foreign_key_cascade_delete(self, session: AsyncSession):
        """Verifica que al eliminar un equipo se eliminen en cascada sus partidos."""
        t1 = Team(
            name="Barça eSports",
            tag="BAR",
            slug="barca-esports",
            division=Division.PREMIER,
            discord_role_id=999999999999999994,
        )
        t2 = Team(
            name="Zeta Gaming",
            tag="ZETA",
            slug="zeta-gaming",
            division=Division.PREMIER,
            discord_role_id=999999999999999995,
        )
        session.add_all([t1, t2])
        await session.flush()

        match = Match(jornada=4, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        session.add(match)
        await session.flush()
        match_id = match.id

        await session.delete(t1)
        await session.flush()

        res = await session.execute(select(Match).where(Match.id == match_id))
        assert res.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_nullable_channel_id_uniqueness(self, session: AsyncSession):
        """Verifica que discord_channel_id sea único si no es null, y permita múltiples nulls."""
        t1 = Team(
            name="Team Alpha",
            tag="TALP",
            slug="team-alpha",
            division=Division.PREMIER,
            discord_role_id=999999999999999996,
        )
        t2 = Team(
            name="Team Beta",
            tag="TBET",
            slug="team-beta",
            division=Division.PREMIER,
            discord_role_id=999999999999999997,
        )
        t3 = Team(
            name="Team Gamma",
            tag="TGAM",
            slug="team-gamma",
            division=Division.PREMIER,
            discord_role_id=999999999999999998,
        )
        session.add_all([t1, t2, t3])
        await session.flush()

        # Dos partidos con channel_id=None deben permitirse
        m1 = Match(
            jornada=1,
            division=Division.PREMIER,
            team1_id=t1.id,
            team2_id=t2.id,
            discord_channel_id=None,
        )
        m2 = Match(
            jornada=2,
            division=Division.PREMIER,
            team1_id=t1.id,
            team2_id=t3.id,
            discord_channel_id=None,
        )
        session.add_all([m1, m2])
        await session.flush()

        # Asignar un channel_id al primer partido
        m1.discord_channel_id = 112233445566778899
        await session.flush()

        # Intentar asignar el mismo channel_id al segundo debe fallar
        m2.discord_channel_id = 112233445566778899
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.flush()


class TestTicketNoticeModel:
    """Pruebas unitarias para el modelo TicketNotice."""

    @pytest.mark.asyncio
    async def test_create_ticket_notice_and_defaults(self, session: AsyncSession):
        """Verifica creación de TicketNotice, campos y defaults."""
        now = datetime.now(timezone.utc)
        notice = TicketNotice(
            discord_channel_id=123123123123123123,
            category_name="TICKETS-GENERAL-PREMIER",
            last_staff_message_at=now,
            last_alert_sent_at=now,
        )
        session.add(notice)
        await session.flush()

        assert isinstance(notice.id, uuid.UUID)
        assert notice.discord_channel_id == 123123123123123123
        assert notice.category_name == "TICKETS-GENERAL-PREMIER"
        assert notice.is_pending_staff is False
        assert isinstance(notice.created_at, datetime)
        assert "TicketNotice" in repr(notice)

    @pytest.mark.asyncio
    async def test_unique_ticket_channel_id_constraint(self, session: AsyncSession):
        """Verifica unicidad de discord_channel_id en TicketNotice."""
        n1 = TicketNotice(discord_channel_id=987654321987654321)
        session.add(n1)
        await session.flush()

        n2 = TicketNotice(discord_channel_id=987654321987654321)
        session.add(n2)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.flush()
