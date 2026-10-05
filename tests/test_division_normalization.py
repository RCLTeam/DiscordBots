"""
Normalización del nombre de división guardado por la web en seasons_divisions.

La web guarda ``division_name`` con su propio formato (``Premier``, ``Ascend``...), mientras
que el enum ``Division`` usa mayúsculas. Todos los lectores deben normalizarlo y el bot no
debe modificar nunca la tabla compartida ``seasons_divisions``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division
from liga_bot.models.match import Match
from liga_bot.models.roster import DivisionModel, Season, SeasonDivision
from liga_bot.models.team import Team
from liga_bot.repositories.match_repo import MatchRepository
from liga_bot.repositories.team_repo import TeamRepository

JORNADA = 41


@dataclass
class WebSeason:
    """Temporada creada como la crea la web, con nombres de división en su formato."""

    premier: SeasonDivision
    ascend: SeasonDivision


async def _create_web_season(
    session: AsyncSession, premier_name: str, ascend_name: str
) -> WebSeason:
    suffix = uuid.uuid4().hex[:8]
    season = Season(name=f"Temporada web {suffix}")
    session.add(season)
    for name in (premier_name, ascend_name):
        if await session.get(DivisionModel, name) is None:
            session.add(DivisionModel(name=name))
    await session.flush()
    premier = SeasonDivision(season_name=season.name, division_name=premier_name)
    ascend = SeasonDivision(season_name=season.name, division_name=ascend_name)
    session.add_all([premier, ascend])
    await session.flush()
    return WebSeason(premier=premier, ascend=ascend)


def _role_id() -> int:
    return 400_000_000_000_000_000 + uuid.uuid4().int % 10**12


async def _create_team(session: AsyncSession, sd: SeasonDivision, name: str) -> Team:
    team = Team(
        name=f"{name} {uuid.uuid4().hex[:6]}",
        tag=name[:4].upper(),
        season_division_id=sd.id,
        discord_role_id=_role_id(),
    )
    session.add(team)
    await session.flush()
    return team


async def _reload(session: AsyncSession, model: type, entity_id: uuid.UUID):
    session.expunge_all()
    stmt = select(model).where(model.id == entity_id)
    return (await session.execute(stmt)).unique().scalar_one()


async def _division_names(session: AsyncSession, web: WebSeason) -> list[str]:
    stmt = select(SeasonDivision.division_name).where(
        SeasonDivision.id.in_([web.premier.id, web.ascend.id])
    )
    return sorted((await session.execute(stmt)).scalars().all())


@pytest_asyncio.fixture(params=["Ascend", " ascend "], ids=["Ascend", "espacios-minusculas"])
async def web_ascend(request, session: AsyncSession) -> WebSeason:
    premier_name = "Premier" if request.param == "Ascend" else " premier "
    return await _create_web_season(session, premier_name, request.param)


# ---------------------------------------------------------------------------
# Division.from_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ascend", Division.ASCEND),
        (" ascend ", Division.ASCEND),
        ("ASCEND", Division.ASCEND),
        ("Premier", Division.PREMIER),
        (Division.PREMIER, Division.PREMIER),
        ("Challengers", None),
        ("", None),
        (None, None),
    ],
)
def test_division_from_name(raw, expected):
    assert Division.from_name(raw) is expected


# ---------------------------------------------------------------------------
# Lectores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_division_reads_web_name(session: AsyncSession, web_ascend: WebSeason):
    t1 = await _create_team(session, web_ascend.ascend, "Local")
    t2 = await _create_team(session, web_ascend.ascend, "Visitante")
    match = Match(
        jornada=JORNADA,
        id_season_division=web_ascend.ascend.id,
        team1_id=t1.id,
        team2_id=t2.id,
    )
    session.add(match)
    await session.flush()

    reloaded = await _reload(session, Match, match.id)

    assert reloaded.division == Division.ASCEND


@pytest.mark.asyncio
async def test_team_division_reads_web_name(session: AsyncSession, web_ascend: WebSeason):
    team = await _create_team(session, web_ascend.ascend, "Equipo")

    reloaded = await _reload(session, Team, team.id)

    assert reloaded.division == Division.ASCEND


@pytest.mark.asyncio
async def test_list_by_division_matches_web_name(session: AsyncSession, web_ascend: WebSeason):
    ascend_team = await _create_team(session, web_ascend.ascend, "Ascenso")
    premier_team = await _create_team(session, web_ascend.premier, "Premier")
    repo = TeamRepository(session)

    ascend_ids = {t.id for t in await repo.list_by_division(Division.ASCEND)}
    premier_ids = {t.id for t in await repo.list_by_division("premier")}

    assert ascend_team.id in ascend_ids
    assert premier_team.id not in ascend_ids
    assert premier_team.id in premier_ids
    assert ascend_team.id not in premier_ids


@pytest.mark.asyncio
async def test_list_by_jornada_filters_web_name(session: AsyncSession, web_ascend: WebSeason):
    a1 = await _create_team(session, web_ascend.ascend, "AscA")
    a2 = await _create_team(session, web_ascend.ascend, "AscB")
    p1 = await _create_team(session, web_ascend.premier, "PreA")
    p2 = await _create_team(session, web_ascend.premier, "PreB")
    repo = MatchRepository(session)
    ascend_match = await repo.create(
        jornada=JORNADA, id_season_division=web_ascend.ascend.id, team1_id=a1.id, team2_id=a2.id
    )
    premier_match = await repo.create(
        jornada=JORNADA, id_season_division=web_ascend.premier.id, team1_id=p1.id, team2_id=p2.id
    )

    ascend_ids = {m.id for m in await repo.list_by_jornada(JORNADA, division=Division.ASCEND)}
    premier_ids = {m.id for m in await repo.list_by_jornada(JORNADA, division=Division.PREMIER)}

    assert ascend_match.id in ascend_ids
    assert premier_match.id not in ascend_ids
    assert premier_match.id in premier_ids
    assert ascend_match.id not in premier_ids


@pytest.mark.asyncio
async def test_repositories_reject_unknown_division_filter(session: AsyncSession):
    with pytest.raises(ValueError):
        await TeamRepository(session).list_by_division("Challengers")
    with pytest.raises(ValueError):
        await MatchRepository(session).list_by_jornada(JORNADA, division="Challengers")


@pytest.mark.asyncio
async def test_unknown_division_name_logs_warning(session: AsyncSession, caplog, monkeypatch):
    # La configuración de logging de Alembic (fileConfig) desactiva los loggers ya creados.
    monkeypatch.setattr(logging.getLogger("liga_bot.models.enums"), "disabled", False)
    web = await _create_web_season(session, "Premier", f"Challengers {uuid.uuid4().hex[:6]}")
    t1 = await _create_team(session, web.ascend, "Desconocido")
    t2 = await _create_team(session, web.ascend, "Otro")
    match = Match(jornada=JORNADA, id_season_division=web.ascend.id, team1_id=t1.id, team2_id=t2.id)
    session.add(match)
    await session.flush()

    team = await _reload(session, Team, t1.id)
    with caplog.at_level(logging.WARNING, logger="liga_bot.models.enums"):
        assert team.division == Division.PREMIER
    assert any(
        "División desconocida" in r.getMessage() and "Challengers" in r.getMessage()
        for r in caplog.records
    )

    caplog.clear()
    reloaded_match = await _reload(session, Match, match.id)
    with caplog.at_level(logging.WARNING, logger="liga_bot.models.enums"):
        assert reloaded_match.division == Division.PREMIER
    assert any("Match" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Escrituras: el bot no modifica seasons_divisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_same_division_keeps_web_name(session: AsyncSession):
    web = await _create_web_season(session, "Premier", "Ascend")
    team = await _create_team(session, web.ascend, "Igual")
    team = await _reload(session, Team, team.id)

    await TeamRepository(session).update(team, division=Division.ASCEND, tag="IGU")

    assert team.season_division_id == web.ascend.id
    assert await _division_names(session, web) == ["Ascend", "Premier"]


@pytest.mark.asyncio
async def test_update_other_division_moves_team_without_renaming(session: AsyncSession):
    web = await _create_web_season(session, "Premier", "Ascend")
    team = await _create_team(session, web.premier, "Cambio")
    other = await _create_team(session, web.premier, "Resto")
    team = await _reload(session, Team, team.id)

    updated = await TeamRepository(session).update(team, division=Division.ASCEND)

    assert updated.season_division_id == web.ascend.id
    assert updated.division == Division.ASCEND
    assert await _division_names(session, web) == ["Ascend", "Premier"]
    assert (await _reload(session, Team, other.id)).division == Division.PREMIER


@pytest.mark.asyncio
async def test_update_to_division_missing_in_season_fails(session: AsyncSession):
    suffix = uuid.uuid4().hex[:8]
    season = Season(name=f"Solo premier {suffix}")
    session.add(season)
    if await session.get(DivisionModel, "Premier") is None:
        session.add(DivisionModel(name="Premier"))
    await session.flush()
    premier = SeasonDivision(season_name=season.name, division_name="Premier")
    session.add(premier)
    await session.flush()
    team = await _create_team(session, premier, "Sola")
    team = await _reload(session, Team, team.id)

    with pytest.raises(ValueError, match="ASCEND"):
        await TeamRepository(session).update(team, division=Division.ASCEND)

    assert (await session.get(SeasonDivision, premier.id)).division_name == "Premier"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Team, Match])
async def test_setter_never_writes_season_division(session: AsyncSession, model):
    web = await _create_web_season(session, "Premier", "Ascend")
    t1 = await _create_team(session, web.ascend, "SetA")
    t2 = await _create_team(session, web.ascend, "SetB")
    if model is Team:
        entity_id = t1.id
    else:
        match = Match(
            jornada=JORNADA, id_season_division=web.ascend.id, team1_id=t1.id, team2_id=t2.id
        )
        session.add(match)
        await session.flush()
        entity_id = match.id
    entity = await _reload(session, model, entity_id)

    entity.division = Division.ASCEND
    entity.division = " ascend "
    with pytest.raises(ValueError):
        entity.division = Division.PREMIER
    await session.flush()

    assert entity.season_division.division_name == "Ascend"
    assert entity.division == Division.ASCEND
    assert await _division_names(session, web) == ["Ascend", "Premier"]
