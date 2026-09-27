"""
Pruebas exhaustivas para la capa de repositorios asíncronos en src/liga_bot/repositories/.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.models.enums import Division, MatchStatus
from liga_bot.models.team import Team
from liga_bot.repositories.base import BaseRepository
from liga_bot.repositories.match_repo import MatchRepository
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.repositories.ticket_repo import TicketNoticeRepository


@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provee una sesión limpia con truncado total de tablas tras cada test."""
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()
        await session.execute(text("TRUNCATE TABLE ticket_notices, matches, teams CASCADE;"))
        await session.commit()


@pytest.fixture
def team_repo(db_session: AsyncSession) -> TeamRepository:
    return TeamRepository(db_session)


@pytest.fixture
def match_repo(db_session: AsyncSession) -> MatchRepository:
    return MatchRepository(db_session)


@pytest.fixture
def ticket_repo(db_session: AsyncSession) -> TicketNoticeRepository:
    return TicketNoticeRepository(db_session)


# ---------------------------------------------------------------------------
# 1. BaseRepository CRUD General
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_repo_get_by_id_found_and_none(db_session: AsyncSession):
    """Verifica get_by_id con entidad existente y con ID inexistente."""
    repo = BaseRepository[Team](db_session, Team)
    team = await repo.create(
        name="Team SoloMid",
        tag="TSM",
        slug="tsm",
        division=Division.PREMIER,
        discord_role_id=100000000000000001,
    )
    found = await repo.get_by_id(team.id)
    assert found is not None
    assert found.name == "Team SoloMid"

    found_by_str = await repo.get_by_id(str(team.id))
    assert found_by_str is not None
    assert found_by_str.id == team.id

    not_found = await repo.get_by_id(uuid.uuid4())
    assert not_found is None


@pytest.mark.asyncio
async def test_repository_get_by_id_invalid_uuid_returns_none_without_aborting_transaction(
    team_repo: TeamRepository,
    match_repo: MatchRepository,
    ticket_repo: TicketNoticeRepository,
):
    """
    Verifica que consultar get_by_id con identificadores que no son UUIDs válidos
    retorne None de forma segura sin disparar DataError en la base de datos
    ni abortar el bloque de transacción SQL activo (InFailedSqlTransaction).
    """
    # 1. Crear una entidad previa para verificar la salud continua de la sesión
    created_team = await team_repo.create(
        name="Healthy Session Team",
        tag="HLT",
        slug="healthy-session-team",
        division=Division.PREMIER,
        discord_role_id=700000000000000001,
    )

    # 2. Probar múltiples formatos inválidos que no cumplen especificación UUID
    invalid_identifiers = [
        "not-a-valid-uuid",
        "12345-invalid-uuid",
        "xyz",
        "",
        "   ",
        "00000000-0000-0000-0000-00000000000Z",
        123456789,
    ]

    for invalid_id in invalid_identifiers:
        # BaseRepository / TeamRepository
        res_team = await team_repo.get_by_id(invalid_id)
        assert res_team is None, (
            f"Se esperaba None para invalid_id='{invalid_id}', recibido {res_team}"
        )

        # MatchRepository
        res_match = await match_repo.get_by_id(invalid_id)
        assert res_match is None, (
            f"Se esperaba None para match con invalid_id='{invalid_id}', recibido {res_match}"
        )

        res_match_eager = await match_repo.get_by_id(invalid_id, with_teams=True)
        assert res_match_eager is None

        # TicketNoticeRepository
        res_ticket = await ticket_repo.get_by_id(invalid_id)
        assert res_ticket is None

    # 3. Comprobar que la transacción SQL sigue viva y operativa tras consultas con errores
    still_healthy = await team_repo.get_by_id(created_team.id)
    assert still_healthy is not None
    assert still_healthy.name == "Healthy Session Team"

    all_teams = await team_repo.list_all()
    assert len(all_teams) >= 1


@pytest.mark.asyncio
async def test_base_repo_list_all_and_count(db_session: AsyncSession):
    """Verifica list_all y count en BaseRepository."""
    repo = BaseRepository[Team](db_session, Team)
    assert await repo.count() == 0
    assert len(await repo.list_all()) == 0

    await repo.create(
        name="Cloud9",
        tag="C9",
        slug="c9",
        division=Division.PREMIER,
        discord_role_id=100000000000000002,
    )
    await repo.create(
        name="Team Liquid",
        tag="TL",
        slug="tl",
        division=Division.PREMIER,
        discord_role_id=100000000000000003,
    )

    assert await repo.count() == 2
    all_teams = await repo.list_all()
    assert len(all_teams) == 2


@pytest.mark.asyncio
async def test_base_repo_create_with_instance_and_kwargs(db_session: AsyncSession):
    """Verifica creación mediante instancia preexistente o mediante kwargs."""
    repo = BaseRepository[Team](db_session, Team)

    # Vía kwargs
    t1 = await repo.create(
        name="100 Thieves",
        tag="100T",
        slug="100-thieves",
        division=Division.PREMIER,
        discord_role_id=100000000000000004,
    )
    assert isinstance(t1.id, uuid.UUID)

    # Vía instancia
    t2_instance = Team(
        name="FlyQuest",
        tag="FLY",
        slug="flyquest",
        division=Division.PREMIER,
        discord_role_id=100000000000000005,
    )
    t2 = await repo.create(t2_instance)
    assert t2.name == "FlyQuest"
    assert t2.id == t2_instance.id


@pytest.mark.asyncio
async def test_base_repo_update_attributes(db_session: AsyncSession):
    """Verifica actualización parcial de campos con persistencia sincronizada."""
    repo = BaseRepository[Team](db_session, Team)
    team = await repo.create(
        name="Dignitas",
        tag="DIG",
        slug="dignitas",
        division=Division.PREMIER,
        discord_role_id=100000000000000006,
    )
    updated = await repo.update(team, tag="DIGN")
    assert updated.tag == "DIGN"
    assert updated.division == Division.PREMIER

    reloaded = await repo.get_by_id(team.id)
    assert reloaded is not None
    assert reloaded.tag == "DIGN"


@pytest.mark.asyncio
async def test_base_repo_delete_instance(db_session: AsyncSession):
    """Verifica eliminación de una entidad pasando la instancia."""
    repo = BaseRepository[Team](db_session, Team)
    team = await repo.create(
        name="Immortals",
        tag="IMT",
        slug="immortals",
        division=Division.ASCEND,
        discord_role_id=100000000000000007,
    )
    await repo.delete(team)
    assert await repo.get_by_id(team.id) is None


@pytest.mark.asyncio
async def test_base_repo_delete_by_id(db_session: AsyncSession):
    """Verifica delete_by_id retornando True si existía y False si no existía."""
    repo = BaseRepository[Team](db_session, Team)
    team = await repo.create(
        name="Evil Geniuses",
        tag="EG",
        slug="evil-geniuses",
        division=Division.PREMIER,
        discord_role_id=100000000000000008,
    )
    deleted = await repo.delete_by_id(team.id)
    assert deleted is True
    assert await repo.get_by_id(team.id) is None

    deleted_again = await repo.delete_by_id(team.id)
    assert deleted_again is False


# ---------------------------------------------------------------------------
# 2. TeamRepository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_repo_create_generates_uuid_and_defaults(team_repo: TeamRepository):
    """Verifica que el repositorio genere UUID y timestamps automáticos."""
    team = await team_repo.create(
        name="KOI",
        tag="KOI",
        slug="koi",
        division=Division.PREMIER,
        discord_role_id=200000000000000001,
    )
    assert isinstance(team.id, uuid.UUID)
    assert team.created_at is not None
    assert team.name == "KOI"


@pytest.mark.asyncio
async def test_team_repo_get_by_name_exact(team_repo: TeamRepository):
    """Verifica búsqueda por nombre exacto."""
    await team_repo.create(
        name="G2 Esports",
        tag="G2",
        slug="g2-esports",
        division=Division.PREMIER,
        discord_role_id=200000000000000002,
    )
    found = await team_repo.get_by_name("G2 Esports", case_sensitive=True)
    assert found is not None
    assert found.tag == "G2"

    not_found = await team_repo.get_by_name("g2 esports", case_sensitive=True)
    assert not_found is None


@pytest.mark.asyncio
async def test_team_repo_get_by_name_case_insensitive(team_repo: TeamRepository):
    """Verifica búsqueda por nombre de forma insensible a mayúsculas."""
    await team_repo.create(
        name="Fnatic",
        tag="FNC",
        slug="fnatic",
        division=Division.PREMIER,
        discord_role_id=200000000000000003,
    )
    found = await team_repo.get_by_name("  fnatic  ")
    assert found is not None
    assert found.name == "Fnatic"


@pytest.mark.asyncio
async def test_team_repo_get_by_tag_case_insensitive(team_repo: TeamRepository):
    """Verifica búsqueda por tag insensible a mayúsculas."""
    await team_repo.create(
        name="MAD Lions KOI",
        tag="MDK",
        slug="mad-lions-koi",
        division=Division.PREMIER,
        discord_role_id=200000000000000004,
    )
    found = await team_repo.get_by_tag("mdk")
    assert found is not None
    assert found.tag == "MDK"

    assert await team_repo.get_by_tag("NONE") is None


@pytest.mark.asyncio
async def test_team_repo_get_by_role_id_snowflake(team_repo: TeamRepository):
    """Verifica búsqueda por discord_role_id de 64 bits."""
    role_id = 1548795786110967919
    await team_repo.create(
        name="Heretics",
        tag="TH",
        slug="heretics",
        division=Division.PREMIER,
        discord_role_id=role_id,
    )
    found = await team_repo.get_by_role_id(role_id)
    assert found is not None
    assert found.name == "Heretics"

    assert await team_repo.get_by_role_id(999999) is None


@pytest.mark.asyncio
async def test_team_repo_get_by_slug(team_repo: TeamRepository):
    """Verifica que get_by_slug fue eliminado conforme a R3."""
    assert not hasattr(team_repo, "get_by_slug")


@pytest.mark.asyncio
async def test_team_repo_list_by_division_filtering_and_order(team_repo: TeamRepository):
    """Verifica filtrado por división y orden alfabético ascendente."""
    await team_repo.create(
        name="Zeta Gaming",
        tag="ZETA",
        slug="zeta",
        division=Division.PREMIER,
        discord_role_id=200000000000000006,
    )
    await team_repo.create(
        name="Barça eSports",
        tag="BAR",
        slug="barca",
        division=Division.PREMIER,
        discord_role_id=200000000000000007,
    )
    await team_repo.create(
        name="UCAM Tokiers",
        tag="UCAM",
        slug="ucam",
        division=Division.ASCEND,
        discord_role_id=200000000000000008,
    )

    premier_teams = await team_repo.list_by_division(Division.PREMIER)
    assert len(premier_teams) == 2
    assert premier_teams[0].name == "Barça eSports"
    assert premier_teams[1].name == "Zeta Gaming"

    ascend_teams = await team_repo.list_by_division("ASCEND")
    assert len(ascend_teams) == 1
    assert ascend_teams[0].name == "UCAM Tokiers"


@pytest.mark.asyncio
async def test_team_repo_list_all_ordering(team_repo: TeamRepository):
    """Verifica list_all ordenado alfabéticamente por nombre conforme a R3."""
    await team_repo.create(
        name="Zeta",
        tag="ZET",
        slug="zeta",
        division=Division.PREMIER,
        discord_role_id=200000000000000009,
    )
    await team_repo.create(
        name="Alpha",
        tag="ALP",
        slug="alpha",
        division=Division.ASCEND,
        discord_role_id=200000000000000010,
    )

    all_teams = await team_repo.list_all()
    assert len(all_teams) == 2
    # R3: list_all ordena por Team.name.asc()
    assert all_teams[0].division == Division.ASCEND
    assert all_teams[0].name == "Alpha"
    assert all_teams[1].division == Division.PREMIER
    assert all_teams[1].name == "Zeta"


@pytest.mark.asyncio
async def test_team_repo_unique_name_constraint_violation(
    team_repo: TeamRepository, db_session: AsyncSession
):
    """Verifica que el repositorio dispare IntegrityError ante nombres duplicados."""
    await team_repo.create(
        name="Giants",
        tag="GIA",
        slug="giants",
        division=Division.PREMIER,
        discord_role_id=200000000000000011,
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await team_repo.create(
                name="Giants",
                tag="GIA2",
                slug="giants-2",
                division=Division.PREMIER,
                discord_role_id=200000000000000012,
            )


@pytest.mark.asyncio
async def test_team_repo_unique_discord_role_id_violation(
    team_repo: TeamRepository, db_session: AsyncSession
):
    """Verifica que el repositorio dispare IntegrityError ante discord_role_id duplicado."""
    shared_role = 200000000000000013
    await team_repo.create(
        name="Rebels",
        tag="RBL",
        slug="rebels",
        division=Division.PREMIER,
        discord_role_id=shared_role,
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await team_repo.create(
                name="Rebels Academy",
                tag="RBLA",
                slug="rebels-academy",
                division=Division.ASCEND,
                discord_role_id=shared_role,
            )


@pytest.mark.asyncio
async def test_team_repo_update_and_delete(team_repo: TeamRepository):
    """Verifica actualización y borrado de equipo a través del repositorio."""
    team = await team_repo.create(
        name="Team Queso",
        tag="TQ",
        slug="team-queso",
        division=Division.PREMIER,
        discord_role_id=200000000000000014,
    )
    updated = await team_repo.update(team, tag="TQ2")
    assert updated.tag == "TQ2"
    assert updated.division == Division.PREMIER

    await team_repo.delete(team)
    assert await team_repo.get_by_name("Team Queso") is None


@pytest.mark.asyncio
async def test_team_repo_delete_surviving_entity_attributes_accessible_no_missing_greenlet(
    team_repo: TeamRepository,
):
    """
    Verifica que eliminar un equipo no invalide ni expire las demás entidades
    en la misma sesión asíncrona, evitando excepciones sqlalchemy.exc.MissingGreenlet
    al acceder a sus atributos.
    """
    # 1. Crear dos entidades independientes en la misma sesión
    t1 = await team_repo.create(
        name="Team To Delete",
        tag="DEL",
        slug="team-to-delete",
        division=Division.PREMIER,
        discord_role_id=600000000000000001,
    )
    t2 = await team_repo.create(
        name="Team Surviving",
        tag="SURV",
        slug="team-surviving",
        division=Division.PREMIER,
        discord_role_id=600000000000000002,
    )

    # 2. Eliminar la primera entidad
    await team_repo.delete(t1)

    # 3. Acceder a todos los atributos de la entidad superviviente t2
    # Si expire_all() estuviera activo, esto lanzaría MissingGreenlet
    assert t2.name == "Team Surviving"
    assert t2.tag == "SURV"
    assert t2.slug == "team-surviving"
    assert t2.division == Division.PREMIER
    assert t2.discord_role_id == 600000000000000002

    # 4. Verificar consistencia en la base de datos
    assert await team_repo.get_by_id(t1.id) is None
    reloaded_t2 = await team_repo.get_by_id(t2.id)
    assert reloaded_t2 is not None
    assert reloaded_t2.id == t2.id
    assert reloaded_t2.name == "Team Surviving"


# ---------------------------------------------------------------------------
# 3. MatchRepository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_repo_create_pending_match(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica la creación de un partido en estado PENDIENTE."""
    t1 = await team_repo.create(
        name="T1",
        tag="T1",
        slug="t1",
        division=Division.PREMIER,
        discord_role_id=300000000000000001,
    )
    t2 = await team_repo.create(
        name="Gen.G",
        tag="GEN",
        slug="geng",
        division=Division.PREMIER,
        discord_role_id=300000000000000002,
    )

    match = await match_repo.create(
        jornada=1,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
    )
    assert match.status == MatchStatus.PENDIENTE
    assert match.jornada == 1
    assert match.discord_channel_id is None


@pytest.mark.asyncio
async def test_match_repo_get_by_jornada_and_teams_exact_order(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica búsqueda con exact_order=True."""
    t1 = await team_repo.create(
        name="Bilibili",
        tag="BLG",
        slug="blg",
        division=Division.PREMIER,
        discord_role_id=300000000000000003,
    )
    t2 = await team_repo.create(
        name="Weibo",
        tag="WBG",
        slug="wbg",
        division=Division.PREMIER,
        discord_role_id=300000000000000004,
    )

    await match_repo.create(
        jornada=3,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
    )

    found_exact = await match_repo.get_by_jornada_and_teams(3, t1.id, t2.id, exact_order=True)
    assert found_exact is not None

    found_inverted_strict = await match_repo.get_by_jornada_and_teams(
        3, t2.id, t1.id, exact_order=True
    )
    assert found_inverted_strict is None


@pytest.mark.asyncio
async def test_match_repo_get_by_jornada_and_teams_inverted_order_symmetric(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica búsqueda simétrica (exact_order=False por defecto)."""
    t1 = await team_repo.create(
        name="Top Esports",
        tag="TES",
        slug="tes",
        division=Division.PREMIER,
        discord_role_id=300000000000000005,
    )
    t2 = await team_repo.create(
        name="JD Gaming",
        tag="JDG",
        slug="jdg",
        division=Division.PREMIER,
        discord_role_id=300000000000000006,
    )

    created = await match_repo.create(
        jornada=5,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
    )

    found_symmetric = await match_repo.get_by_jornada_and_teams(5, t2.id, t1.id)
    assert found_symmetric is not None
    assert found_symmetric.id == created.id


@pytest.mark.asyncio
async def test_match_repo_list_by_jornada_with_division_filter(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica list_by_jornada con y sin filtro de división."""
    tp1 = await team_repo.create(
        name="TP1",
        tag="TP1",
        slug="tp1",
        division=Division.PREMIER,
        discord_role_id=300000000000000007,
    )
    tp2 = await team_repo.create(
        name="TP2",
        tag="TP2",
        slug="tp2",
        division=Division.PREMIER,
        discord_role_id=300000000000000008,
    )
    ta1 = await team_repo.create(
        name="TA1",
        tag="TA1",
        slug="ta1",
        division=Division.ASCEND,
        discord_role_id=300000000000000009,
    )
    ta2 = await team_repo.create(
        name="TA2",
        tag="TA2",
        slug="ta2",
        division=Division.ASCEND,
        discord_role_id=300000000000000010,
    )

    await match_repo.create(jornada=1, division=Division.PREMIER, team1_id=tp1.id, team2_id=tp2.id)
    await match_repo.create(jornada=1, division=Division.ASCEND, team1_id=ta1.id, team2_id=ta2.id)

    all_j1 = await match_repo.list_by_jornada(1)
    assert len(all_j1) == 2

    premier_j1 = await match_repo.list_by_jornada(1, division=Division.PREMIER)
    assert len(premier_j1) == 1
    assert premier_j1[0].division == Division.PREMIER


@pytest.mark.asyncio
async def test_match_repo_list_by_status(match_repo: MatchRepository, team_repo: TeamRepository):
    """Verifica list_by_status."""
    t1 = await team_repo.create(
        name="KT",
        tag="KT",
        slug="kt",
        division=Division.PREMIER,
        discord_role_id=300000000000000011,
    )
    t2 = await team_repo.create(
        name="DK",
        tag="DK",
        slug="dk",
        division=Division.PREMIER,
        discord_role_id=300000000000000012,
    )

    m = await match_repo.create(
        jornada=2, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )
    pending = await match_repo.list_by_status(MatchStatus.PENDIENTE)
    assert len(pending) == 1

    await match_repo.update_status(m, MatchStatus.LIVE)
    pending_after = await match_repo.list_by_status(MatchStatus.PENDIENTE)
    assert len(pending_after) == 0

    live = await match_repo.list_by_status(MatchStatus.LIVE)
    assert len(live) == 1


@pytest.mark.asyncio
async def test_match_repo_get_by_channel_id(match_repo: MatchRepository, team_repo: TeamRepository):
    """Verifica búsqueda por snowflake de canal de Discord."""
    t1 = await team_repo.create(
        name="Fredit",
        tag="BRO",
        slug="bro",
        division=Division.PREMIER,
        discord_role_id=300000000000000013,
    )
    t2 = await team_repo.create(
        name="Nongshim",
        tag="NS",
        slug="ns",
        division=Division.PREMIER,
        discord_role_id=300000000000000014,
    )

    channel_id = 998877665544332211
    match = await match_repo.create(
        jornada=4,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
        discord_channel_id=channel_id,
    )

    found = await match_repo.get_by_channel_id(channel_id)
    assert found is not None
    assert found.id == match.id


@pytest.mark.asyncio
async def test_match_repo_update_status_lifecycle(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica transición de estado de PENDIENTE a JUGADO."""
    t1 = await team_repo.create(
        name="DRX",
        tag="DRX",
        slug="drx",
        division=Division.PREMIER,
        discord_role_id=300000000000000015,
    )
    t2 = await team_repo.create(
        name="Kwangdong",
        tag="KDF",
        slug="kdf",
        division=Division.PREMIER,
        discord_role_id=300000000000000016,
    )

    match = await match_repo.create(
        jornada=6, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )
    updated = await match_repo.update_status(match.id, MatchStatus.JUGADO)
    assert updated is not None
    assert updated.status == MatchStatus.JUGADO

    none_res = await match_repo.update_status(uuid.uuid4(), MatchStatus.CANCELADO)
    assert none_res is None


@pytest.mark.asyncio
async def test_match_repo_update_channel_id(match_repo: MatchRepository, team_repo: TeamRepository):
    """Verifica vinculación de channel_id a un partido existente."""
    t1 = await team_repo.create(
        name="BNK",
        tag="BNK",
        slug="bnk",
        division=Division.PREMIER,
        discord_role_id=300000000000000017,
    )
    t2 = await team_repo.create(
        name="OKBRO",
        tag="OKB",
        slug="okb",
        division=Division.PREMIER,
        discord_role_id=300000000000000018,
    )

    match = await match_repo.create(
        jornada=7, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )
    assert match.discord_channel_id is None

    updated = await match_repo.update_channel_id(match, 123456789098765432)
    assert updated is not None
    assert updated.discord_channel_id == 123456789098765432


@pytest.mark.asyncio
async def test_match_repo_eager_loading_with_teams_no_missing_greenlet(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica que with_teams=True cargue team1 y team2 sin MissingGreenlet."""
    t1 = await team_repo.create(
        name="Weibo Gaming",
        tag="WBG2",
        slug="wbg2",
        division=Division.PREMIER,
        discord_role_id=300000000000000019,
    )
    t2 = await team_repo.create(
        name="Anyone's Legend",
        tag="AL",
        slug="al",
        division=Division.PREMIER,
        discord_role_id=300000000000000020,
    )

    match = await match_repo.create(
        jornada=8, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )

    loaded = await match_repo.get_by_id(match.id, with_teams=True)
    assert loaded is not None
    assert loaded.team1.name == "Weibo Gaming"
    assert loaded.team2.name == "Anyone's Legend"


@pytest.mark.asyncio
async def test_match_repo_unique_jornada_teams_constraint_violation(
    match_repo: MatchRepository, team_repo: TeamRepository, db_session: AsyncSession
):
    """Verifica que no se puedan duplicar partidos idénticos en la misma jornada."""
    t1 = await team_repo.create(
        name="LGD",
        tag="LGD",
        slug="lgd",
        division=Division.PREMIER,
        discord_role_id=300000000000000021,
    )
    t2 = await team_repo.create(
        name="RA",
        tag="RA",
        slug="ra",
        division=Division.PREMIER,
        discord_role_id=300000000000000022,
    )

    await match_repo.create(jornada=9, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await match_repo.create(
                jornada=9, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
            )


@pytest.mark.asyncio
async def test_match_repo_distinct_teams_check_constraint_violation(
    match_repo: MatchRepository, team_repo: TeamRepository, db_session: AsyncSession
):
    """Verifica que el CheckConstraint impida que un equipo juegue contra sí mismo."""
    t1 = await team_repo.create(
        name="OMG",
        tag="OMG",
        slug="omg",
        division=Division.PREMIER,
        discord_role_id=300000000000000023,
    )

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await match_repo.create(
                jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t1.id
            )


@pytest.mark.asyncio
async def test_match_repo_cascade_delete_on_team_removal(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica que borrar un equipo elimine en cascada sus partidos."""
    t1 = await team_repo.create(
        name="Ninjas in Pyjamas",
        tag="NIP",
        slug="nip",
        division=Division.PREMIER,
        discord_role_id=300000000000000024,
    )
    t2 = await team_repo.create(
        name="Invictus Gaming",
        tag="IG",
        slug="ig",
        division=Division.PREMIER,
        discord_role_id=300000000000000025,
    )

    match = await match_repo.create(
        jornada=10, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )
    match_id = match.id
    await team_repo.delete(t1)

    assert await match_repo.get_by_id(match_id) is None


@pytest.mark.asyncio
async def test_get_by_teams_bidirectional(match_repo: MatchRepository, team_repo: TeamRepository):
    """Verifica get_by_teams en orden normal y simétricamente invertido."""
    t1 = await team_repo.create(
        name="Team Liquid",
        tag="TL",
        slug="team-liquid",
        division=Division.PREMIER,
        discord_role_id=300000000000000026,
    )
    t2 = await team_repo.create(
        name="Cloud9",
        tag="C9",
        slug="cloud9",
        division=Division.PREMIER,
        discord_role_id=300000000000000027,
    )
    match = await match_repo.create(
        jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )

    # Orden normal (t1 vs t2)
    m1 = await match_repo.get_by_teams(t1.id, t2.id)
    assert m1 is not None
    assert m1.id == match.id
    assert m1.team1.name == "Team Liquid"
    assert m1.team2.name == "Cloud9"

    # Orden invertido (t2 vs t1)
    m2 = await match_repo.get_by_teams(t2.id, t1.id)
    assert m2 is not None
    assert m2.id == match.id

    # Pasando UUIDs como string
    m3 = await match_repo.get_by_teams(str(t1.id), str(t2.id))
    assert m3 is not None
    assert m3.id == match.id


@pytest.mark.asyncio
async def test_get_by_teams_with_jornada_filter(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica get_by_teams filtrando por jornada y ordenando por más reciente."""
    t1 = await team_repo.create(
        name="Fnatic",
        tag="FNC",
        slug="fnatic",
        division=Division.PREMIER,
        discord_role_id=300000000000000028,
    )
    t2 = await team_repo.create(
        name="G2 Esports",
        tag="G2",
        slug="g2-esports",
        division=Division.PREMIER,
        discord_role_id=300000000000000029,
    )

    dt1 = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)

    match_j1 = await match_repo.create(
        jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id, scheduled_at=dt1
    )
    match_j2 = await match_repo.create(
        jornada=2, division=Division.PREMIER, team1_id=t2.id, team2_id=t1.id, scheduled_at=dt2
    )

    # Filtrar por jornada específica
    res_j1 = await match_repo.get_by_teams(t1.id, t2.id, jornada=1)
    assert res_j1 is not None
    assert res_j1.id == match_j1.id

    res_j2 = await match_repo.get_by_teams(t1.id, t2.id, jornada=2)
    assert res_j2 is not None
    assert res_j2.id == match_j2.id

    res_j3 = await match_repo.get_by_teams(t1.id, t2.id, jornada=3)
    assert res_j3 is None

    # Sin jornada: debe devolver el más reciente (jornada 2 por scheduled_at)
    latest = await match_repo.get_by_teams(t1.id, t2.id)
    assert latest is not None
    assert latest.id == match_j2.id


@pytest.mark.asyncio
async def test_get_by_teams_invalid_uuid_and_not_found(
    match_repo: MatchRepository, team_repo: TeamRepository
):
    """Verifica manejo seguro de UUIDs inválidos y equipos no enfrentados."""
    t1 = await team_repo.create(
        name="Astralis",
        tag="AST",
        slug="astralis",
        division=Division.PREMIER,
        discord_role_id=300000000000000030,
    )
    # Formato inválido retorna None sin fallar la sesión
    assert await match_repo.get_by_teams("not-a-valid-uuid", str(t1.id)) is None
    assert await match_repo.get_by_teams(str(t1.id), "not-a-valid-uuid") is None

    # UUIDs válidos pero sin partido registrado
    assert await match_repo.get_by_teams(uuid.uuid4(), t1.id) is None


@pytest.mark.asyncio
async def test_update_stream_url(match_repo: MatchRepository, team_repo: TeamRepository):
    """Verifica actualización de stream_url (VOD) y stream_url_live."""
    t1 = await team_repo.create(
        name="Team Vitality",
        tag="VIT",
        slug="team-vitality",
        division=Division.PREMIER,
        discord_role_id=300000000000000031,
    )
    t2 = await team_repo.create(
        name="Karmine Corp",
        tag="KC",
        slug="karmine-corp",
        division=Division.PREMIER,
        discord_role_id=300000000000000032,
    )
    match = await match_repo.create(
        jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )

    # Actualizar stream_url (VOD / is_live=False) pasando UUID
    vod_url = "https://youtube.com/watch?v=vod123"
    updated_vod = await match_repo.update_stream_url(match.id, vod_url, is_live=False)
    assert updated_vod is not None
    assert updated_vod.stream_url == vod_url
    assert updated_vod.stream_url_live is None

    # Actualizar stream_url_live (is_live=True) pasando instancia Match
    live_url = "https://twitch.tv/rcl_live"
    updated_live = await match_repo.update_stream_url(updated_vod, live_url, is_live=True)
    assert updated_live is not None
    assert updated_live.stream_url == vod_url
    assert updated_live.stream_url_live == live_url

    # Actualizar pasando str ID
    live_url_updated = "https://twitch.tv/rcl_live_2"
    updated_str = await match_repo.update_stream_url(str(match.id), live_url_updated, is_live=True)
    assert updated_str is not None
    assert updated_str.stream_url_live == live_url_updated

    # ID inexistente
    non_existent = await match_repo.update_stream_url(uuid.uuid4(), "https://twitch.tv/foo")
    assert non_existent is None


# ---------------------------------------------------------------------------
# 4. TicketNoticeRepository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_repo_record_alert_initial_insert(ticket_repo: TicketNoticeRepository):
    """Verifica que record_alert inserte una nueva entidad si el canal no existe."""
    channel_id = 400000000000000001
    alert_dt = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)

    notice = await ticket_repo.record_alert(
        channel_id=channel_id,
        alert_time=alert_dt,
        category_name="TICKETS-GENERAL-PREMIER",
        is_pending_staff=True,
    )
    assert isinstance(notice.id, uuid.UUID)
    assert notice.discord_channel_id == channel_id
    assert notice.category_name == "TICKETS-GENERAL-PREMIER"
    assert notice.is_pending_staff is True
    assert notice.last_alert_sent_at == alert_dt


@pytest.mark.asyncio
async def test_ticket_repo_record_alert_idempotent_update(
    ticket_repo: TicketNoticeRepository,
):
    """Verifica que record_alert sea un upsert idempotente y no duplique registros."""
    channel_id = 400000000000000002
    t1_alert = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)
    t2_alert = datetime(2026, 9, 18, 14, 0, 0, tzinfo=timezone.utc)

    n1 = await ticket_repo.record_alert(
        channel_id=channel_id,
        alert_time=t1_alert,
        category_name="TICKETS-GENERAL-PREMIER",
    )
    orig_id = n1.id

    n2 = await ticket_repo.record_alert(
        channel_id=channel_id,
        alert_time=t2_alert,
        category_name="TICKETS-FICHAJES-PREMIER",
    )
    assert n2.id == orig_id
    assert n2.last_alert_sent_at == t2_alert
    assert n2.category_name == "TICKETS-FICHAJES-PREMIER"

    # Verificar que sólo existe una fila
    all_notices = await ticket_repo.list_all()
    assert len(all_notices) == 1


@pytest.mark.asyncio
async def test_ticket_repo_get_by_channel_id(ticket_repo: TicketNoticeRepository):
    """Verifica get_by_channel_id."""
    channel_id = 400000000000000003
    await ticket_repo.record_alert(channel_id=channel_id)

    found = await ticket_repo.get_by_channel_id(channel_id)
    assert found is not None
    assert found.discord_channel_id == channel_id

    assert await ticket_repo.get_by_channel_id(999999999) is None


@pytest.mark.asyncio
async def test_ticket_repo_record_staff_response_clears_pending(
    ticket_repo: TicketNoticeRepository,
):
    """Verifica que record_staff_response marque is_pending_staff=False y actualice timestamp."""
    channel_id = 400000000000000004
    await ticket_repo.record_alert(channel_id=channel_id, is_pending_staff=True)

    resp_time = datetime(2026, 9, 18, 16, 0, 0, tzinfo=timezone.utc)
    updated = await ticket_repo.record_staff_response(channel_id, response_time=resp_time)
    assert updated is not None
    assert updated.is_pending_staff is False
    assert updated.last_staff_message_at == resp_time

    # Para un canal inexistente debe retornar None
    assert await ticket_repo.record_staff_response(999999999) is None


@pytest.mark.asyncio
async def test_ticket_repo_list_pending_staff(ticket_repo: TicketNoticeRepository):
    """Verifica list_pending_staff filtrando sólo tickets con is_pending_staff=True."""
    c1 = 400000000000000005
    c2 = 400000000000000006

    await ticket_repo.record_alert(channel_id=c1, is_pending_staff=True)
    await ticket_repo.record_alert(channel_id=c2, is_pending_staff=False)

    pending = await ticket_repo.list_pending_staff()
    assert len(pending) == 1
    assert pending[0].discord_channel_id == c1


@pytest.mark.asyncio
async def test_ticket_repo_delete_by_channel_id(ticket_repo: TicketNoticeRepository):
    """Verifica delete_by_channel_id."""
    channel_id = 400000000000000007
    await ticket_repo.record_alert(channel_id=channel_id)

    deleted = await ticket_repo.delete_by_channel_id(channel_id)
    assert deleted is True
    assert await ticket_repo.get_by_channel_id(channel_id) is None

    deleted_again = await ticket_repo.delete_by_channel_id(channel_id)
    assert deleted_again is False


# ---------------------------------------------------------------------------
# 5. Transacciones e Idempotencia
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_rollback_cleans_uncommitted_state(
    team_repo: TeamRepository, db_session: AsyncSession
):
    """Verifica que un rollback limpie las operaciones de repositorio no confirmadas."""
    await team_repo.create(
        name="Temporary Team",
        tag="TEMP",
        slug="temporary-team",
        division=Division.PREMIER,
        discord_role_id=500000000000000001,
    )
    # Rollback explícito sin commit
    await db_session.rollback()

    reloaded = await team_repo.get_by_name("Temporary Team")
    assert reloaded is None


@pytest.mark.asyncio
async def test_repository_atomic_multi_operation_commit(
    team_repo: TeamRepository, match_repo: MatchRepository, db_session: AsyncSession
):
    """Verifica que múltiples operaciones entre distintos repositorios se confirmen atómicamente."""
    t1 = await team_repo.create(
        name="Team X",
        tag="TX",
        slug="tx",
        division=Division.PREMIER,
        discord_role_id=500000000000000002,
    )
    t2 = await team_repo.create(
        name="Team Y",
        tag="TY",
        slug="ty",
        division=Division.PREMIER,
        discord_role_id=500000000000000003,
    )
    match = await match_repo.create(
        jornada=1, division=Division.PREMIER, team1_id=t1.id, team2_id=t2.id
    )
    await db_session.commit()

    # Nueva sesión para confirmar persistencia real
    assert await team_repo.get_by_id(t1.id) is not None
    assert await match_repo.get_by_id(match.id) is not None
