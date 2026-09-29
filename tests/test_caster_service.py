"""Unit and integration tests for CasterRepository and CasterService."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.models import (
    CasterRole,
    Division,
    Match,
    Team,
)
from liga_bot.repositories.caster_repo import CasterRepository, _clean_user_id
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.services.caster_service import (
    CasterAssignmentResult,
    CasterService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provee una sesión limpia truncando tablas de casters y partidos antes y después."""
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(
            text("TRUNCATE TABLE match_casters, match_caster_cards, matches, teams CASCADE;")
        )
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(
            text("TRUNCATE TABLE match_casters, match_caster_cards, matches, teams CASCADE;")
        )
        await session.commit()


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factoría de sesiones asíncronas para pruebas de servicios."""
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest.fixture
def caster_service(session_factory: async_sessionmaker[AsyncSession]) -> CasterService:
    """Instancia del servicio de casters inyectada con la factoría de pruebas."""
    return CasterService(session_factory=session_factory)


@pytest.fixture
async def sample_teams(db_session: AsyncSession) -> tuple[Team, Team]:
    """Crea y persiste dos equipos para los partidos de prueba."""
    team_repo = TeamRepository(db_session)
    t1 = await team_repo.create(
        name="Team Liquid",
        tag="TL",
        slug="team-liquid",
        division=Division.PREMIER,
        discord_role_id=111111111111111111,
    )
    t2 = await team_repo.create(
        name="Fnatic",
        tag="FNC",
        slug="fnatic",
        division=Division.PREMIER,
        discord_role_id=222222222222222222,
    )
    await db_session.commit()
    return t1, t2


@pytest.fixture
async def sample_match(db_session: AsyncSession, sample_teams: tuple[Team, Team]) -> Match:
    """Crea y persiste un partido en jornada 1."""
    t1, t2 = sample_teams
    match = Match(
        jornada=1,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
        scheduled_at=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
    )
    db_session.add(match)
    await db_session.commit()
    return match


# ---------------------------------------------------------------------------
# Tests de CasterService - Asignaciones y Exclusividad
# ---------------------------------------------------------------------------


class TestCasterServiceAssignments:
    """Pruebas para asignaciones de casters, streamers y exclusividad en CasterService."""

    @pytest.mark.asyncio
    async def test_assign_caster_role_success(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Asignar rol CASTER no ocupa el streamer y se añade a la lista de casters."""
        user_id = 100000000000000001
        res: CasterAssignmentResult = await caster_service.assign_caster(
            sample_match.id, user_id, CasterRole.CASTER
        )

        assert res.success is True
        assert res.action == "assign"
        assert res.error is None
        assert res.data is not None
        assert res.data.has_streamer is False
        assert res.data.streamer is None
        assert len(res.data.casters) == 1
        assert res.data.casters[0].discord_user_id == user_id
        assert res.data.casters[0].caster_role == CasterRole.CASTER

    @pytest.mark.asyncio
    async def test_multiple_casters_allowed_on_same_match(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Múltiples usuarios pueden asignarse libremente como casters."""
        u1, u2, u3 = 100000000000000001, 100000000000000002, 100000000000000003
        await caster_service.assign_caster(sample_match.id, u1, CasterRole.CASTER)
        await caster_service.assign_caster(sample_match.id, u2, CasterRole.CASTER)
        res = await caster_service.assign_caster(sample_match.id, u3, CasterRole.CASTER)

        assert res.success is True
        assert res.data is not None
        assert res.data.has_streamer is False
        assert len(res.data.casters) == 3
        caster_ids = [c.discord_user_id for c in res.data.casters]
        assert caster_ids == [u1, u2, u3]

    @pytest.mark.asyncio
    async def test_assign_streamer_role_populates_streamer(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Asignar rol STREAMER activa has_streamer y rellena la entidad streamer."""
        user_id = 200000000000000001
        res = await caster_service.assign_caster(
            sample_match.id, user_id, CasterRole.STREAMER
        )

        assert res.success is True
        assert res.data is not None
        assert res.data.has_streamer is True
        assert res.data.streamer is not None
        assert res.data.streamer.discord_user_id == user_id
        assert res.data.streamer.caster_role == CasterRole.STREAMER
        # STREAMER puro no está en casters
        assert len(res.data.casters) == 0

    @pytest.mark.asyncio
    async def test_assign_both_role_populates_streamer_and_casters(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Asignar rol BOTH cuenta como streamer y como caster."""
        user_id = 300000000000000001
        res = await caster_service.assign_caster(
            sample_match.id, user_id, CasterRole.BOTH
        )

        assert res.success is True
        assert res.data is not None
        assert res.data.has_streamer is True
        assert res.data.streamer is not None
        assert res.data.streamer.discord_user_id == user_id
        assert len(res.data.casters) == 1
        assert res.data.casters[0].discord_user_id == user_id
        assert res.data.casters[0].caster_role == CasterRole.BOTH

    @pytest.mark.asyncio
    async def test_rejection_of_second_streamer_with_error_message(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Si ya existe un streamer, otro usuario no puede asignarse como STREAMER."""
        u1, u2 = 400000000000000001, 400000000000000002
        res1 = await caster_service.assign_caster(
            sample_match.id, u1, CasterRole.STREAMER
        )
        assert res1.success is True

        res2 = await caster_service.assign_caster(
            sample_match.id, u2, CasterRole.STREAMER
        )
        assert res2.success is False
        assert res2.action == "assign"
        assert res2.error == "Ya hay una persona asignada a la retransmisión de este partido."
        assert res2.data is not None
        assert res2.data.has_streamer is True
        assert res2.data.streamer.discord_user_id == u1

    @pytest.mark.asyncio
    async def test_rejection_of_both_role_when_streamer_exists(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Si ya existe un streamer, otro usuario no puede asignarse como BOTH."""
        u1, u2 = 500000000000000001, 500000000000000002
        await caster_service.assign_caster(sample_match.id, u1, CasterRole.STREAMER)

        res2 = await caster_service.assign_caster(
            sample_match.id, u2, CasterRole.BOTH
        )
        assert res2.success is False
        assert res2.error == "Ya hay una persona asignada a la retransmisión de este partido."
        assert res2.data.streamer.discord_user_id == u1

    @pytest.mark.asyncio
    async def test_rejection_of_streamer_when_both_role_exists(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Si ya existe un usuario con rol BOTH, otro usuario no puede asignarse como STREAMER."""
        u1, u2 = 600000000000000001, 600000000000000002
        await caster_service.assign_caster(sample_match.id, u1, CasterRole.BOTH)

        res2 = await caster_service.assign_caster(
            sample_match.id, u2, CasterRole.STREAMER
        )
        assert res2.success is False
        assert res2.error == "Ya hay una persona asignada a la retransmisión de este partido."

    @pytest.mark.asyncio
    async def test_same_user_can_update_role_from_caster_to_streamer(
        self, caster_service: CasterService, sample_match: Match
    ):
        """El mismo usuario puede promocionarse de CASTER a STREAMER si el puesto está libre."""
        user_id = 700000000000000001
        await caster_service.assign_caster(sample_match.id, user_id, CasterRole.CASTER)

        res = await caster_service.assign_caster(
            sample_match.id, user_id, CasterRole.STREAMER
        )
        assert res.success is True
        assert res.data.has_streamer is True
        assert res.data.streamer.discord_user_id == user_id
        assert len(res.data.casters) == 0

    @pytest.mark.asyncio
    async def test_same_user_can_update_role_from_streamer_to_both(
        self, caster_service: CasterService, sample_match: Match
    ):
        """El mismo streamer puede cambiar su rol a BOTH sin colisionar consigo mismo."""
        user_id = 800000000000000001
        await caster_service.assign_caster(sample_match.id, user_id, CasterRole.STREAMER)

        res = await caster_service.assign_caster(sample_match.id, user_id, CasterRole.BOTH)
        assert res.success is True
        assert res.data.has_streamer is True
        assert res.data.streamer.discord_user_id == user_id
        assert len(res.data.casters) == 1

    @pytest.mark.asyncio
    async def test_same_user_switch_from_streamer_to_caster_frees_slot(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Si el streamer cambia a CASTER, libera el slot de streamer para otro usuario."""
        u1, u2 = 900000000000000001, 900000000000000002
        await caster_service.assign_caster(sample_match.id, u1, CasterRole.STREAMER)

        # u1 cambia a CASTER
        res1 = await caster_service.assign_caster(sample_match.id, u1, CasterRole.CASTER)
        assert res1.success is True
        assert res1.data.has_streamer is False

        # u2 ahora puede ocupar STREAMER
        res2 = await caster_service.assign_caster(sample_match.id, u2, CasterRole.STREAMER)
        assert res2.success is True
        assert res2.data.has_streamer is True
        assert res2.data.streamer.discord_user_id == u2

    @pytest.mark.asyncio
    async def test_assign_caster_with_string_role(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Permite pasar roles como strings válidos."""
        res = await caster_service.assign_caster(
            sample_match.id, 111111, "STREAMER"
        )
        assert res.success is True
        assert res.data.has_streamer is True

    @pytest.mark.asyncio
    async def test_assign_caster_with_invalid_role_string(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Retorna error legible si se proporciona un rol inválido."""
        res = await caster_service.assign_caster(
            sample_match.id, 111111, "INVALID_ROLE"
        )
        assert res.success is False
        assert "Rol de casteo inválido" in res.error

    @pytest.mark.asyncio
    async def test_assign_caster_invalid_match_id(
        self, caster_service: CasterService
    ):
        """Retorna error de identificador inválido si el match_id no es un UUID válido."""
        res = await caster_service.assign_caster(
            "not-a-valid-uuid", 111111, CasterRole.CASTER
        )
        assert res.success is False
        assert res.error == "Identificador de partido inválido."


# ---------------------------------------------------------------------------
# Tests de CasterService - Desasignación (remove_caster)
# ---------------------------------------------------------------------------


class TestCasterServiceRemoval:
    """Pruebas para desasignaciones de casters y streamers."""

    @pytest.mark.asyncio
    async def test_remove_caster_success(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Desasignar a un caster lo retira de la lista de casters."""
        u1, u2 = 100001, 100002
        await caster_service.assign_caster(sample_match.id, u1, CasterRole.CASTER)
        await caster_service.assign_caster(sample_match.id, u2, CasterRole.CASTER)

        res = await caster_service.remove_caster(sample_match.id, u1)
        assert res.success is True
        assert res.action == "remove"
        assert len(res.data.casters) == 1
        assert res.data.casters[0].discord_user_id == u2

    @pytest.mark.asyncio
    async def test_remove_streamer_frees_streamer_slot(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Desasignar al streamer reactiva el estado vacante y permite un nuevo streamer."""
        u1, u2 = 200001, 200002
        await caster_service.assign_caster(sample_match.id, u1, CasterRole.STREAMER)

        res_rem = await caster_service.remove_caster(sample_match.id, u1)
        assert res_rem.success is True
        assert res_rem.data.has_streamer is False
        assert res_rem.data.streamer is None

        # u2 se asigna como nuevo streamer
        res_new = await caster_service.assign_caster(sample_match.id, u2, CasterRole.STREAMER)
        assert res_new.success is True
        assert res_new.data.has_streamer is True
        assert res_new.data.streamer.discord_user_id == u2

    @pytest.mark.asyncio
    async def test_remove_non_assigned_user_is_noop_safe(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Desasignar a un usuario no registrado no causa errores."""
        res = await caster_service.remove_caster(sample_match.id, 999999999)
        assert res.success is True
        assert res.action == "remove"
        assert res.data.has_streamer is False
        assert len(res.data.casters) == 0

    @pytest.mark.asyncio
    async def test_remove_caster_invalid_match_id(
        self, caster_service: CasterService
    ):
        """Retorna error si el match_id no es un UUID válido."""
        res = await caster_service.remove_caster("bad-uuid", 12345)
        assert res.success is False
        assert res.error == "Identificador de partido inválido."


# ---------------------------------------------------------------------------
# Tests de Independencia entre Partidos
# ---------------------------------------------------------------------------


class TestCrossMatchIndependence:
    """Pruebas de que la exclusividad opera por match_id y no globalmente."""

    @pytest.mark.asyncio
    async def test_same_user_can_be_streamer_in_multiple_matches(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """Un mismo usuario puede ser streamer en Match A y Match B de la misma jornada."""
        t1, t2 = sample_teams
        m1 = Match(jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        m2 = Match(jornada=1, division=Division.PREMIER, team1_id=t2.id, team2_id=t1.id)
        db_session.add_all([m1, m2])
        await db_session.commit()

        user_id = 9990001
        res1 = await caster_service.assign_caster(m1.id, user_id, CasterRole.STREAMER)
        res2 = await caster_service.assign_caster(m2.id, user_id, CasterRole.STREAMER)

        assert res1.success is True
        assert res2.success is True

        d1 = await caster_service.get_match_casters_data(m1.id)
        d2 = await caster_service.get_match_casters_data(m2.id)
        assert d1.has_streamer is True
        assert d2.has_streamer is True
        assert d1.streamer.discord_user_id == user_id
        assert d2.streamer.discord_user_id == user_id

    @pytest.mark.asyncio
    async def test_streamer_in_match_a_does_not_block_streamer_in_match_b(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """Distintos streamers pueden operar en distintos partidos simultáneamente."""
        t1, t2 = sample_teams
        m1 = Match(jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        m2 = Match(jornada=1, division=Division.PREMIER, team1_id=t2.id, team2_id=t1.id)
        db_session.add_all([m1, m2])
        await db_session.commit()

        u1, u2 = 888001, 888002
        res1 = await caster_service.assign_caster(m1.id, u1, CasterRole.STREAMER)
        res2 = await caster_service.assign_caster(m2.id, u2, CasterRole.STREAMER)

        assert res1.success is True
        assert res2.success is True


# ---------------------------------------------------------------------------
# Tests de Jornadas y Partidos (get_active_jornada y get_matches_for_jornada)
# ---------------------------------------------------------------------------


class TestJornadaAndMatchesResolution:
    """Pruebas para resolución de jornadas activas y consulta de partidos con relaciones."""

    @pytest.mark.asyncio
    async def test_get_active_jornada_empty_returns_none(
        self, caster_service: CasterService
    ):
        """Si no hay partidos registrados, get_active_jornada retorna None."""
        active = await caster_service.get_active_jornada()
        assert active is None

    @pytest.mark.asyncio
    async def test_get_active_jornada_returns_max(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """get_active_jornada retorna el valor MAX de jornada entre todos los partidos."""
        t1, t2 = sample_teams
        m1 = Match(jornada=2, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        m2 = Match(jornada=5, division=Division.PREMIER, team1_id=t2.id, team2_id=t1.id)
        m3 = Match(jornada=3, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        db_session.add_all([m1, m2, m3])
        await db_session.commit()

        active = await caster_service.get_active_jornada()
        assert active == 5

    @pytest.mark.asyncio
    async def test_get_matches_for_jornada_explicit(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """Consulta partidos filtrados explícitamente por número de jornada."""
        t1, t2 = sample_teams
        m_j1_1 = Match(
            jornada=1,
            division=Division.PREMIER,
            team1_id=t1.id,
            team2_id=t2.id,
            scheduled_at=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
        )
        m_j1_2 = Match(
            jornada=1,
            division=Division.PREMIER,
            team1_id=t2.id,
            team2_id=t1.id,
            scheduled_at=datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc),
        )
        m_j2 = Match(
            jornada=2,
            division=Division.PREMIER,
            team1_id=t1.id,
            team2_id=t2.id,
        )
        db_session.add_all([m_j1_1, m_j1_2, m_j2])
        await db_session.commit()

        matches_j1 = await caster_service.get_matches_for_jornada(jornada=1)
        assert len(matches_j1) == 2
        # Comprobar carga eager de relaciones
        assert matches_j1[0].team1 is not None
        assert matches_j1[0].team2 is not None
        assert hasattr(matches_j1[0], "casters")

    @pytest.mark.asyncio
    async def test_get_matches_for_jornada_resolves_active_when_none(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """Cuando jornada=None, resuelve la última jornada activa y carga sus partidos."""
        t1, t2 = sample_teams
        m1 = Match(jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        m2 = Match(jornada=4, division=Division.PREMIER, team1_id=t2.id, team2_id=t1.id)
        db_session.add_all([m1, m2])
        await db_session.commit()

        matches = await caster_service.get_matches_for_jornada(jornada=None)
        assert len(matches) == 1
        assert matches[0].jornada == 4

    @pytest.mark.asyncio
    async def test_get_matches_for_jornada_empty_db_returns_empty(
        self, caster_service: CasterService
    ):
        """Si la base de datos no tiene partidos, retorna una lista vacía."""
        matches = await caster_service.get_matches_for_jornada(jornada=None)
        assert matches == []


# ---------------------------------------------------------------------------
# Tests de Tarjetas Publicadas e Idempotencia (MatchCasterCard)
# ---------------------------------------------------------------------------


class TestCardIdempotencyAndPublication:
    """Pruebas para el registro de tarjetas y publicación idempotente sin duplicados."""

    @pytest.mark.asyncio
    async def test_record_card_and_get_card(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Registrar una tarjeta la persiste y permite recuperarla por match_id y channel_id."""
        channel_id = 1550210628361392278
        message_id = 987654321098765432

        card = await caster_service.record_card(sample_match.id, channel_id, message_id)
        assert card.match_id == sample_match.id
        assert card.channel_id == channel_id
        assert card.message_id == message_id

        fetched = await caster_service.get_card(sample_match.id, channel_id)
        assert fetched is not None
        assert fetched.id == card.id
        assert fetched.message_id == message_id

    @pytest.mark.asyncio
    async def test_record_card_upsert_updates_existing(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Volver a registrar la tarjeta para el mismo match y canal actualiza message_id."""
        channel_id = 1550210628361392278
        card1 = await caster_service.record_card(sample_match.id, channel_id, 111111)
        assert card1.message_id == 111111
        card2 = await caster_service.record_card(sample_match.id, channel_id, 222222)

        assert card2.channel_id == channel_id
        assert card2.message_id == 222222

        fetched = await caster_service.get_card(sample_match.id, channel_id)
        assert fetched.message_id == 222222

    @pytest.mark.asyncio
    async def test_delete_card(
        self, caster_service: CasterService, sample_match: Match
    ):
        """delete_card elimina el registro de tarjeta y retorna True si existía."""
        channel_id = 1550210628361392278
        await caster_service.record_card(sample_match.id, channel_id, 123456)

        deleted = await caster_service.delete_card(sample_match.id, channel_id)
        assert deleted is True

        fetched = await caster_service.get_card(sample_match.id, channel_id)
        assert fetched is None

        # Borrado redundante retorna False
        deleted_again = await caster_service.delete_card(sample_match.id, channel_id)
        assert deleted_again is False

    @pytest.mark.asyncio
    async def test_list_cards_for_channel(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """list_cards_for_channel devuelve únicamente las tarjetas de ese canal."""
        t1, t2 = sample_teams
        m1 = Match(jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        m2 = Match(jornada=1, division=Division.PREMIER, team1_id=t2.id, team2_id=t1.id)
        db_session.add_all([m1, m2])
        await db_session.commit()

        c100 = 100
        c200 = 200
        await caster_service.record_card(m1.id, c100, 11)
        await caster_service.record_card(m2.id, c100, 12)
        await caster_service.record_card(m1.id, c200, 21)

        cards_100 = await caster_service.list_cards_for_channel(c100)
        assert len(cards_100) == 2

        cards_200 = await caster_service.list_cards_for_channel(c200)
        assert len(cards_200) == 1

    @pytest.mark.asyncio
    async def test_get_unposted_matches_filters_already_posted(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """get_unposted_matches omite partidos con tarjeta publicada y retorna los pendientes."""
        t1, t2 = sample_teams
        team_repo = TeamRepository(db_session)
        t3 = await team_repo.create(
            name="G2 Esports",
            tag="G2",
            slug="g2-esports",
            division=Division.PREMIER,
            discord_role_id=333333333333333333,
        )
        t4 = await team_repo.create(
            name="Mad Lions",
            tag="MAD",
            slug="mad-lions",
            division=Division.PREMIER,
            discord_role_id=444444444444444444,
        )

        m1 = Match(
            jornada=1,
            division=Division.PREMIER,
            team1_id=t1.id,
            team2_id=t2.id,
            scheduled_at=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
        )
        m2 = Match(
            jornada=1,
            division=Division.PREMIER,
            team1_id=t2.id,
            team2_id=t1.id,
            scheduled_at=datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc),
        )
        m3 = Match(
            jornada=1,
            division=Division.PREMIER,
            team1_id=t3.id,
            team2_id=t4.id,
            scheduled_at=datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc),
        )
        db_session.add_all([m1, m2, m3])
        await db_session.commit()

        channel_id = 55555

        # Inicialmente, los 3 partidos están pendientes de publicar en channel_id
        pending = await caster_service.get_unposted_matches(1, channel_id)
        assert len(pending) == 3

        # Publicamos m1
        await caster_service.record_card(m1.id, channel_id, 101)

        # Ahora solo m2 y m3 están pendientes
        pending_after_m1 = await caster_service.get_unposted_matches(1, channel_id)
        assert len(pending_after_m1) == 2
        pending_ids = [m.id for m in pending_after_m1]
        assert m1.id not in pending_ids
        assert m2.id in pending_ids
        assert m3.id in pending_ids

        # Publicamos m2 y m3
        await caster_service.record_card(m2.id, channel_id, 102)
        await caster_service.record_card(m3.id, channel_id, 103)

        # Todos publicados: retorno vacío
        pending_all = await caster_service.get_unposted_matches(1, channel_id)
        assert len(pending_all) == 0

        # Si se borra la tarjeta de m2 (por ejemplo, mensaje eliminado en Discord):
        await caster_service.delete_card(m2.id, channel_id)
        pending_reposted = await caster_service.get_unposted_matches(1, channel_id)
        assert len(pending_reposted) == 1
        assert pending_reposted[0].id == m2.id

    @pytest.mark.asyncio
    async def test_unposted_matches_independent_per_channel(
        self,
        caster_service: CasterService,
        db_session: AsyncSession,
        sample_teams: tuple[Team, Team],
    ):
        """Haber publicado una tarjeta en el canal A no afecta los pendientes del canal B."""
        t1, t2 = sample_teams
        m = Match(jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
        db_session.add(m)
        await db_session.commit()

        chan_a = 1001
        chan_b = 1002

        await caster_service.record_card(m.id, chan_a, 777)

        assert len(await caster_service.get_unposted_matches(1, chan_a)) == 0
        assert len(await caster_service.get_unposted_matches(1, chan_b)) == 1


# ---------------------------------------------------------------------------
# Tests directos de CasterRepository
# ---------------------------------------------------------------------------


class TestCasterRepositoryDirect:
    """Pruebas unitarias directas sobre métodos de CasterRepository."""

    @pytest.mark.asyncio
    async def test_repo_get_match_assignments_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """get_match_assignments retorna lista vacía ante UUID inválido sin error de BD."""
        repo = CasterRepository(db_session)
        res = await repo.get_match_assignments("invalid-uuid")
        assert res == []

    @pytest.mark.asyncio
    async def test_repo_get_user_assignment_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """get_user_assignment retorna None ante UUID inválido."""
        repo = CasterRepository(db_session)
        res = await repo.get_user_assignment("invalid-uuid", 123)
        assert res is None

    @pytest.mark.asyncio
    async def test_repo_get_streamer_assignment_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """get_streamer_assignment retorna None ante UUID inválido."""
        repo = CasterRepository(db_session)
        res = await repo.get_streamer_assignment("invalid-uuid")
        assert res is None

    @pytest.mark.asyncio
    async def test_repo_assign_raises_on_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """assign lanza ValueError si el match_id no es convertible a UUID."""
        repo = CasterRepository(db_session)
        with pytest.raises(ValueError) as exc:
            await repo.assign("bad-uuid", 123, CasterRole.CASTER)
        assert "Identificador de partido inválido" in str(exc.value)

    @pytest.mark.asyncio
    async def test_repo_remove_returns_false_on_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """remove retorna False ante UUID inválido."""
        repo = CasterRepository(db_session)
        res = await repo.remove("bad-uuid", 123)
        assert res is False

    @pytest.mark.asyncio
    async def test_repo_record_card_raises_on_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """record_card lanza ValueError si match_id es inválido."""
        repo = CasterRepository(db_session)
        with pytest.raises(ValueError):
            await repo.record_card("bad-uuid", 123, 456)

    @pytest.mark.asyncio
    async def test_repo_get_card_returns_none_on_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """get_card retorna None ante UUID inválido."""
        repo = CasterRepository(db_session)
        assert await repo.get_card("bad-uuid", 123) is None

    @pytest.mark.asyncio
    async def test_repo_delete_card_returns_false_on_invalid_uuid(
        self, db_session: AsyncSession
    ):
        """delete_card retorna False ante UUID inválido."""
        repo = CasterRepository(db_session)
        assert await repo.delete_card("bad-uuid", 123) is False


class TestCasterServiceEdgeCasesAndAdversarial:
    """Pruebas adversariales de condiciones de carrera y valores límite."""

    @pytest.mark.asyncio
    async def test_assign_caster_race_condition_handled(
        self,
        caster_service: CasterService,
        sample_match: Match,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Simula una condición de carrera donde repo.assign lanza IntegrityError."""
        from unittest.mock import AsyncMock

        from sqlalchemy.exc import IntegrityError

        mock_assign = AsyncMock(
            side_effect=IntegrityError("stmt", {}, Exception("uq_match_casters_single_streamer"))
        )
        monkeypatch.setattr(CasterRepository, "assign", mock_assign)

        res = await caster_service.assign_caster(
            sample_match.id, 999999, CasterRole.STREAMER
        )
        assert res.success is False
        assert res.error == "Ya hay una persona asignada a la retransmisión de este partido."

    @pytest.mark.asyncio
    async def test_get_match_casters_data_non_existent_match(
        self, caster_service: CasterService
    ):
        """Consultar casters de un partido inexistente retorna estructura vacía segura."""
        import uuid

        random_id = uuid.uuid4()
        data = await caster_service.get_match_casters_data(random_id)
        assert data.has_streamer is False
        assert data.streamer is None
        assert data.casters == []

    @pytest.mark.asyncio
    async def test_get_match_casters_data_invalid_uuid(
        self, caster_service: CasterService
    ):
        """Consultar casters con UUID corrupto retorna estructura vacía sin error de BD."""
        data = await caster_service.get_match_casters_data("malformed-uuid-12345")
        assert data.has_streamer is False
        assert data.streamer is None
        assert data.casters == []

    @pytest.mark.asyncio
    async def test_get_unposted_matches_empty_jornada(
        self, caster_service: CasterService
    ):
        """get_unposted_matches retorna lista vacía para jornadas sin partidos."""
        unposted = await caster_service.get_unposted_matches(999, channel_id=12345)
        assert unposted == []

    @pytest.mark.asyncio
    async def test_service_list_cards_for_channel(
        self, caster_service: CasterService, sample_match: Match
    ):
        """list_cards_for_channel en el servicio devuelve las tarjetas del canal."""
        channel_id = 987654321
        await caster_service.record_card(sample_match.id, channel_id, 123001)

        cards = await caster_service.list_cards_for_channel(channel_id)
        assert len(cards) == 1
        assert cards[0].message_id == 123001

    @pytest.mark.asyncio
    async def test_assign_caster_with_valid_string_user_id(
        self, caster_service: CasterService, sample_match: Match
    ):
        """String numérico válido de user_id es parseado y asignado como entero."""
        res = await caster_service.assign_caster(
            sample_match.id, "123456789", CasterRole.CASTER
        )
        assert res.success is True
        assert len(res.data.casters) == 1
        assert res.data.casters[0].discord_user_id == 123456789

        # Con espacios en blanco circundantes
        res2 = await caster_service.assign_caster(
            sample_match.id, "  987654321  ", CasterRole.STREAMER
        )
        assert res2.success is True
        assert res2.data.has_streamer is True
        assert res2.data.streamer.discord_user_id == 987654321

        # Desasignación con string
        rem = await caster_service.remove_caster(sample_match.id, "123456789")
        assert rem.success is True
        assert len(rem.data.casters) == 0

    @pytest.mark.asyncio
    async def test_assign_and_remove_empty_or_whitespace_user_id_fails_gracefully(
        self, caster_service: CasterService, sample_match: Match
    ):
        """user_id vacío, con solo espacios o no numérico falla limpiamente sin ProgrammingError."""
        for invalid_uid in ("", "   ", "not-a-number", None, 0, -10):
            res_assign = await caster_service.assign_caster(
                sample_match.id, invalid_uid, CasterRole.CASTER
            )
            assert res_assign.success is False
            assert res_assign.error == "ID de usuario de Discord inválido."

            res_remove = await caster_service.remove_caster(
                sample_match.id, invalid_uid
            )
            assert res_remove.success is False
            assert res_remove.error == "ID de usuario de Discord inválido."

    @pytest.mark.asyncio
    async def test_assign_caster_non_existent_match_returns_foreign_key_error(
        self, caster_service: CasterService
    ):
        """Asignar a un match_id inexistente retorna error de clave foránea diferenciado."""
        import uuid

        non_existent_id = uuid.uuid4()
        res = await caster_service.assign_caster(
            non_existent_id, 123456, CasterRole.CASTER
        )
        assert res.success is False
        assert res.error == "El partido especificado no existe."
        assert res.data is not None
        assert res.data.has_streamer is False
        assert res.data.streamer is None
        assert res.data.casters == []

    @pytest.mark.asyncio
    async def test_assign_caster_unhandled_integrity_error_fallback(
        self,
        caster_service: CasterService,
        sample_match: Match,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Error de integridad no asociado a streamer único ni FK devuelve mensaje genérico."""
        from unittest.mock import AsyncMock

        from sqlalchemy.exc import IntegrityError

        mock_assign = AsyncMock(
            side_effect=IntegrityError("stmt", {}, Exception("some_generic_check_violation"))
        )
        monkeypatch.setattr(CasterRepository, "assign", mock_assign)

        res = await caster_service.assign_caster(
            sample_match.id, 123456, CasterRole.CASTER
        )
        assert res.success is False
        assert res.error == "Error de integridad en la asignación."

    def test_clean_user_id_helper_pure(self):
        """Validación pura del helper _clean_user_id."""
        assert _clean_user_id(123) == 123
        assert _clean_user_id("123") == 123
        assert _clean_user_id("  123  ") == 123
        assert _clean_user_id("+123") == 123
        assert _clean_user_id("") is None
        assert _clean_user_id("   ") is None
        assert _clean_user_id("abc") is None
        assert _clean_user_id(0) is None
        assert _clean_user_id("0") is None
        assert _clean_user_id(-10) is None
        assert _clean_user_id("-10") is None
        assert _clean_user_id(None) is None
        assert _clean_user_id(True) is None
        assert _clean_user_id(False) is None

    @pytest.mark.asyncio
    async def test_repo_user_id_sanitization_direct(
        self, db_session: AsyncSession, sample_match: Match
    ):
        """CasterRepository descarta user_id inválidos sin ejecutar queries corruptas."""
        repo = CasterRepository(db_session)
        assert await repo.get_user_assignment(sample_match.id, "") is None
        assert await repo.get_user_assignment(sample_match.id, "   ") is None
        assert await repo.get_user_assignment(sample_match.id, -100) is None
        assert await repo.assign(sample_match.id, "", CasterRole.CASTER) is None
        assert await repo.remove(sample_match.id, "") is False
