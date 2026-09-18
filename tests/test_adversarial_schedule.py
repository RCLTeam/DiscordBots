"""
Pruebas de estrés adversarial y casos límite para ScheduleService y formateo.

Casos de prueba de verificación para ScheduleService:
1. Ingesta de CSVs hostiles y de frontera.
2. Garantía de rollback anti-canales huérfanos ante fallos de Discord o Base de Datos.
3. Formateo extremo de nombres de canales y slugs con Unicode, emojis, RTL y longitudes extremas.
"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.models.enums import Division
from liga_bot.repositories.match_repo import MatchRepository
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.services.schedule_service import ScheduleService
from liga_bot.utils.formatting import (
    format_match_channel_name,
    normalize_name,
    normalize_slug,
    normalize_tag,
)

# ---------------------------------------------------------------------------
# Fixtures y Mocks
# ---------------------------------------------------------------------------


def make_mock_role(role_id: int, name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_mock_guild(settings: Settings, roles: list[MagicMock] | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = settings.guild_id

    default_role = make_mock_role(settings.guild_id, "@everyone")
    guild.default_role = default_role

    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999999999999
    bot_member.name = "LigaBot"
    guild.me = bot_member

    staff_role = make_mock_role(settings.staff_role_id, "Staff")
    admin_role = make_mock_role(settings.admin_role_id, "Admin")
    ceo_premier = make_mock_role(settings.ceo_premier_role_id, "CEO Premier")
    ceo_ascend = make_mock_role(settings.ceo_ascend_role_id, "CEO Ascend")

    all_roles = [default_role, staff_role, admin_role, ceo_premier, ceo_ascend]
    if roles:
        all_roles.extend(roles)
    role_map = {r.id: r for r in all_roles}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_map.get(rid)

    guild.categories = []
    channel_seq = [5000]

    async def mock_create_category(name: str):
        cat = MagicMock(spec=discord.CategoryChannel)
        cat.id = channel_seq[0]
        channel_seq[0] += 1
        cat.name = name
        cat.channels = []
        guild.categories.append(cat)
        return cat

    async def mock_create_text_channel(name: str, category=None, overwrites=None):
        chan = AsyncMock(spec=discord.TextChannel)
        chan.id = channel_seq[0]
        channel_seq[0] += 1
        chan.name = name
        chan.category = category
        chan.guild = guild
        chan.mention = f"<#{chan.id}>"
        chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        chan.delete = AsyncMock()
        if category is not None:
            category.channels.append(chan)
        return chan

    guild.create_category = AsyncMock(side_effect=mock_create_category)
    guild.create_text_channel = AsyncMock(side_effect=mock_create_text_channel)
    return guild


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        staff_role_id=1001,
        admin_role_id=1002,
        ceo_premier_role_id=1003,
        ceo_ascend_role_id=1004,
        guild_id=1000,
        database_url="pglite:///:memory:",
    )


# ---------------------------------------------------------------------------
# Vector 1: Hostile & Boundary CSV Ingestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csv_trailing_and_intermediate_empty_lines(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba que líneas en blanco iniciales, intermedias y finales se gestionen sin crash."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Alpha Team",
            tag="ALP",
            slug="alpha-team",
            division=Division.PREMIER,
            discord_role_id=3001,
        )
        await team_repo.create(
            name="Beta Team",
            tag="BET",
            slug="beta-team",
            division=Division.PREMIER,
            discord_role_id=3002,
        )
        await session.commit()

    role1 = make_mock_role(3001, "Alpha Team")
    role2 = make_mock_role(3002, "Beta Team")
    guild = make_mock_guild(test_settings, roles=[role1, role2])

    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    # CSV con líneas en blanco al inicio, intermedio y final, más una fila con celdas vacías (,,,)
    csv_with_blanks = (
        "\n\nequipo1,equipo2,fecha,hora\n\nAlpha Team,Beta Team,20/09/2026,21:00\n\n   \n,,,\n\n"
    )

    j_res = await service.create_jornada_from_csv(
        guild=guild, jornada=1, csv_content=csv_with_blanks
    )

    assert j_res.success_count == 1
    assert len(j_res.matches) == 1
    # La fila con sólo espacios y la fila con comas vacías (,,,) registran
    # error sin crashear el proceso
    assert j_res.error_count == 2
    assert "Fila 3: faltan nombres de equipos." in j_res.errors[0]
    assert "Fila 4: faltan nombres de equipos." in j_res.errors[1]


@pytest.mark.asyncio
async def test_csv_whitespace_variations_in_headers_and_values(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba tolerancia a espacios en cabeceras y en celdas de equipos/fechas."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Team Space 1",
            tag="TS1",
            slug="team-space-1",
            division=Division.PREMIER,
            discord_role_id=3101,
        )
        await team_repo.create(
            name="Team Space 2",
            tag="TS2",
            slug="team-space-2",
            division=Division.PREMIER,
            discord_role_id=3102,
        )
        await session.commit()

    role1 = make_mock_role(3101, "Team Space 1")
    role2 = make_mock_role(3102, "Team Space 2")
    guild = make_mock_guild(test_settings, roles=[role1, role2])

    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    csv_spaced = (
        "  equipo1  ,  equipo2  ,  fecha  ,  hora  \n"
        "   Team Space 1   ,   Team Space 2   , 21/09/2026 , 20:00 \n"
    )

    j_res = await service.create_jornada_from_csv(guild=guild, jornada=1, csv_content=csv_spaced)
    assert j_res.total_rows == 1
    assert j_res.success_count == 1
    assert j_res.error_count == 0
    assert j_res.matches[0].team1_name == "Team Space 1"


@pytest.mark.asyncio
async def test_csv_mixed_delimiters_graceful_handling(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba delimitadores inconsistentes (cabecera con comas pero filas con punto y coma)."""
    guild = make_mock_guild(test_settings)
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    # Cabecera con coma pero filas con punto y coma
    csv_mixed = "equipo1,equipo2,fecha,hora\nTeam A;Team B;20/09/2026;21:00\n"
    j_res = await service.create_jornada_from_csv(guild=guild, jornada=1, csv_content=csv_mixed)
    assert j_res.total_rows == 1
    assert j_res.success_count == 0
    assert j_res.error_count == 1
    assert "faltan nombres de equipos" in j_res.errors[0]


@pytest.mark.asyncio
async def test_csv_missing_required_columns(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba rechazo inmediato de CSV con columnas faltantes."""
    guild = make_mock_guild(test_settings)
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    csv_incomplete = "equipo1,equipo2\nTeam A,Team B\n"
    j_res = await service.create_jornada_from_csv(
        guild=guild, jornada=1, csv_content=csv_incomplete
    )
    assert j_res.total_rows == 0
    assert j_res.error_count == 1
    assert "Cabeceras incompletas" in j_res.errors[0]
    assert "fecha" in j_res.errors[0] and "hora" in j_res.errors[0]


@pytest.mark.asyncio
async def test_csv_self_match_and_cross_division(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba auto-partidos y cruce de divisiones en el CSV."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Premier One",
            tag="P1",
            slug="premier-one",
            division=Division.PREMIER,
            discord_role_id=3201,
        )
        await team_repo.create(
            name="Ascend One",
            tag="A1",
            slug="ascend-one",
            division=Division.ASCEND,
            discord_role_id=3202,
        )
        await session.commit()

    role_p1 = make_mock_role(3201, "Premier One")
    role_a1 = make_mock_role(3202, "Ascend One")
    guild = make_mock_guild(test_settings, roles=[role_p1, role_a1])

    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    csv_invalid = (
        "equipo1,equipo2,fecha,hora\n"
        "Premier One,Premier One,20/09/2026,21:00\n"
        "Premier One,Ascend One,20/09/2026,21:00\n"
    )

    j_res = await service.create_jornada_from_csv(guild=guild, jornada=1, csv_content=csv_invalid)
    assert j_res.total_rows == 2
    assert j_res.success_count == 0
    assert j_res.error_count == 2
    assert "no puede enfrentarse a sí mismo" in j_res.errors[0]
    assert "Conflicto de división" in j_res.errors[1]


@pytest.mark.asyncio
async def test_csv_duplicate_and_reverse_duplicate_in_same_csv(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba detección de duplicados directos (A vs B) e invertidos (B vs A) en el mismo CSV."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Team X", tag="TX", slug="team-x", division=Division.PREMIER, discord_role_id=3301
        )
        await team_repo.create(
            name="Team Y", tag="TY", slug="team-y", division=Division.PREMIER, discord_role_id=3302
        )
        await session.commit()

    role1 = make_mock_role(3301, "Team X")
    role2 = make_mock_role(3302, "Team Y")
    guild = make_mock_guild(test_settings, roles=[role1, role2])

    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    csv_duplicates = (
        "equipo1,equipo2,fecha,hora\n"
        "Team X,Team Y,20/09/2026,21:00\n"
        "Team X,Team Y,20/09/2026,22:00\n"
        "Team Y,Team X,20/09/2026,23:00\n"
    )

    j_res = await service.create_jornada_from_csv(
        guild=guild, jornada=1, csv_content=csv_duplicates
    )
    assert j_res.total_rows == 3
    assert j_res.success_count == 1
    assert j_res.error_count == 2
    assert "Fila 3: enfrentamiento duplicado" in j_res.errors[0]
    assert "Fila 4: enfrentamiento duplicado" in j_res.errors[1]


@pytest.mark.asyncio
async def test_csv_duplicate_cross_name_and_slug(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """
    Prueba que si la primera fila usa el nombre oficial ('Planar Shock Pingus') y la
    segunda usa el slug ('planar-shock-pingus'), la segunda es rechazada como duplicada por la BD.
    """
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Planar Shock Pingus",
            tag="PSP",
            slug="planar-shock-pingus",
            division=Division.PREMIER,
            discord_role_id=3401,
        )
        await team_repo.create(
            name="Fnix Esports",
            tag="FNX",
            slug="fnix-esports",
            division=Division.PREMIER,
            discord_role_id=3402,
        )
        await session.commit()

    role1 = make_mock_role(3401, "Planar Shock Pingus")
    role2 = make_mock_role(3402, "Fnix Esports")
    guild = make_mock_guild(test_settings, roles=[role1, role2])

    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    csv_mixed_name_slug = (
        "equipo1,equipo2,fecha,hora\n"
        "Planar Shock Pingus,Fnix Esports,20/09/2026,21:00\n"
        "planar-shock-pingus,fnix-esports,20/09/2026,21:00\n"
    )

    j_res = await service.create_jornada_from_csv(
        guild=guild, jornada=1, csv_content=csv_mixed_name_slug
    )
    assert j_res.total_rows == 2
    assert j_res.success_count == 1
    assert j_res.error_count == 1
    assert "ya existe para la jornada 1" in j_res.errors[0]


@pytest.mark.asyncio
async def test_csv_invalid_team_names_and_partial_success_atomic_isolation(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """
    Prueba completa de ingesta con múltiples filas adversariales:
    - Fila 1: Válida (Team 1 vs Team 2) -> Éxito
    - Fila 2: Equipo inexistente (Team 1 vs GhostTeam) -> Error registrado
    - Fila 3: Cruce de divisiones (Team 1 [Premier] vs Ascend 1 [Ascend]) -> Error registrado
    - Fila 4: Auto-partido (Team 1 vs Team 1) -> Error registrado
    - Fila 5: Duplicado inverso dentro del mismo CSV (Team 2 vs Team 1) -> Error registrado
    - Fila 6: Válida (Team 3 vs Team 4) -> Éxito

    Verifica:
    - total_rows == 6, success_count == 2, error_count == 4.
    - Base de datos solo contiene 2 partidos.
    - Guild de Discord solo tiene 2 canales creados (0 huérfanos).
    """
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Team One",
            tag="T1",
            slug="team-one",
            division=Division.PREMIER,
            discord_role_id=4001,
        )
        await team_repo.create(
            name="Team Two",
            tag="T2",
            slug="team-two",
            division=Division.PREMIER,
            discord_role_id=4002,
        )
        await team_repo.create(
            name="Team Three",
            tag="T3",
            slug="team-three",
            division=Division.PREMIER,
            discord_role_id=4003,
        )
        await team_repo.create(
            name="Team Four",
            tag="T4",
            slug="team-four",
            division=Division.PREMIER,
            discord_role_id=4004,
        )
        await team_repo.create(
            name="Ascend Team",
            tag="ASC",
            slug="ascend-team",
            division=Division.ASCEND,
            discord_role_id=4005,
        )
        await session.commit()

    roles = [
        make_mock_role(4001, "Team One"),
        make_mock_role(4002, "Team Two"),
        make_mock_role(4003, "Team Three"),
        make_mock_role(4004, "Team Four"),
        make_mock_role(4005, "Ascend Team"),
    ]
    guild = make_mock_guild(test_settings, roles=roles)
    service = ScheduleService(session_factory=session_factory, settings=test_settings)

    csv_multi_adversarial = (
        "equipo1,equipo2,fecha,hora\n"
        "Team One,Team Two,20/09/2026,18:00\n"
        "Team One,Ghost Team,20/09/2026,19:00\n"
        "Team One,Ascend Team,20/09/2026,20:00\n"
        "Team One,Team One,20/09/2026,21:00\n"
        "Team Two,Team One,20/09/2026,22:00\n"
        "Team Three,Team Four,20/09/2026,23:00\n"
    )

    j_res = await service.create_jornada_from_csv(
        guild=guild, jornada=1, csv_content=csv_multi_adversarial
    )

    assert j_res.total_rows == 6
    assert j_res.success_count == 2
    assert j_res.error_count == 4
    assert len(j_res.matches) == 5  # Fila 5 fue omitida por seen_pairs antes de create_match

    # Verificación en Base de Datos: exactamente 2 partidos
    async with session_factory() as session:
        match_repo = MatchRepository(session)
        matches = await match_repo.list_by_jornada(1)
        assert len(matches) == 2

    # Verificación en Discord: exactamente 2 canales creados en la categoría
    assert guild.create_text_channel.await_count == 2


@pytest.mark.asyncio
async def test_rollback_discord_send_failure_deletes_channel_and_no_db_record(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Simula fallo en discord send: verifica channel.delete() y 0 registros en DB."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Fail Team 1",
            tag="FT1",
            slug="fail-team-1",
            division=Division.PREMIER,
            discord_role_id=3501,
        )
        await team_repo.create(
            name="Fail Team 2",
            tag="FT2",
            slug="fail-team-2",
            division=Division.PREMIER,
            discord_role_id=3502,
        )
        await session.commit()

    guild = make_mock_guild(
        test_settings,
        roles=[make_mock_role(3501, "Fail Team 1"), make_mock_role(3502, "Fail Team 2")],
    )

    # Interceptar canal creado para que send lance excepción
    created_channel_ref: list[AsyncMock] = []
    original_create = guild.create_text_channel

    async def mock_create_and_fail(*args, **kwargs):
        chan = await original_create(*args, **kwargs)
        chan.send.side_effect = discord.HTTPException(
            response=MagicMock(), message="Discord rate limit / 500"
        )
        created_channel_ref.append(chan)
        return chan

    guild.create_text_channel = AsyncMock(side_effect=mock_create_and_fail)

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_match(
        guild=guild, jornada=1, team1_name="Fail Team 1", team2_name="Fail Team 2"
    )

    assert res.success is False
    assert "Error durante el aprovisionamiento" in res.error
    assert len(created_channel_ref) == 1
    created_channel = created_channel_ref[0]

    # COMPROBACIÓN EMPÍRICA: await channel.delete() fue invocado con razón de rollback
    created_channel.delete.assert_awaited_once_with(reason="Rollback: error de aprovisionamiento")

    # COMPROBACIÓN EMPÍRICA: No hay registros en DB
    async with session_factory() as session:
        match_repo = MatchRepository(session)
        matches = await match_repo.list_by_jornada(1)
        assert len(matches) == 0


@pytest.mark.asyncio
async def test_rollback_discord_send_second_message_failure_deletes_channel(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Simula fallo en el segundo mensaje de coordinación: verifica rollback de canal y 0 en DB."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Send2 Team 1",
            tag="S21",
            slug="send2-team-1",
            division=Division.PREMIER,
            discord_role_id=3551,
        )
        await team_repo.create(
            name="Send2 Team 2",
            tag="S22",
            slug="send2-team-2",
            division=Division.PREMIER,
            discord_role_id=3552,
        )
        await session.commit()

    guild = make_mock_guild(
        test_settings,
        roles=[make_mock_role(3551, "Send2 Team 1"), make_mock_role(3552, "Send2 Team 2")],
    )

    created_channel_ref: list[AsyncMock] = []
    original_create = guild.create_text_channel

    async def mock_create_and_fail_second_send(*args, **kwargs):
        chan = await original_create(*args, **kwargs)
        # El primer send pasa, el segundo falla
        chan.send.side_effect = [
            MagicMock(spec=discord.Message),
            discord.HTTPException(response=MagicMock(), message="Discord drop on message 2"),
        ]
        created_channel_ref.append(chan)
        return chan

    guild.create_text_channel = AsyncMock(side_effect=mock_create_and_fail_second_send)

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_match(
        guild=guild, jornada=1, team1_name="Send2 Team 1", team2_name="Send2 Team 2"
    )

    assert res.success is False
    assert "Error durante el aprovisionamiento" in res.error
    assert len(created_channel_ref) == 1
    created_channel = created_channel_ref[0]

    # COMPROBACIÓN EMPÍRICA: await channel.delete() invocado tras fallo en mensaje 2
    created_channel.delete.assert_awaited_once_with(reason="Rollback: error de aprovisionamiento")

    # COMPROBACIÓN EMPÍRICA: Base de datos limpia (0 registros)
    async with session_factory() as session:
        match_repo = MatchRepository(session)
        matches = await match_repo.list_by_jornada(1)
        assert len(matches) == 0


@pytest.mark.asyncio
async def test_rollback_db_failure_deletes_channel_and_leaves_clean_db(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    monkeypatch,
):
    """Simula fallo en la inserción en base de datos: verifica rollback del canal de Discord."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="DB Team 1",
            tag="DT1",
            slug="db-team-1",
            division=Division.PREMIER,
            discord_role_id=3601,
        )
        await team_repo.create(
            name="DB Team 2",
            tag="DT2",
            slug="db-team-2",
            division=Division.PREMIER,
            discord_role_id=3602,
        )
        await session.commit()

    guild = make_mock_guild(
        test_settings,
        roles=[make_mock_role(3601, "DB Team 1"), make_mock_role(3602, "DB Team 2")],
    )

    created_channel_ref: list[AsyncMock] = []
    original_create = guild.create_text_channel

    async def mock_create_and_track(*args, **kwargs):
        chan = await original_create(*args, **kwargs)
        created_channel_ref.append(chan)
        return chan

    guild.create_text_channel = AsyncMock(side_effect=mock_create_and_track)

    # Forzar fallo en MatchRepository.create
    async def mock_fail_create(*args, **kwargs):
        raise RuntimeError("Simulated Database I/O Failure during match persistence")

    monkeypatch.setattr(MatchRepository, "create", mock_fail_create)

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_match(
        guild=guild, jornada=1, team1_name="DB Team 1", team2_name="DB Team 2"
    )

    assert res.success is False
    assert "Simulated Database I/O Failure" in res.error
    assert len(created_channel_ref) == 1

    # COMPROBACIÓN EMPÍRICA: Rollback de Discord activado
    created_channel_ref[0].delete.assert_awaited_once_with(
        reason="Rollback: error de aprovisionamiento"
    )

    # COMPROBACIÓN EMPÍRICA: Base de datos limpia
    async with session_factory() as session:
        match_repo = MatchRepository(session)
        matches = await match_repo.list_by_jornada(1)
        assert len(matches) == 0


@pytest.mark.asyncio
async def test_rollback_resilient_if_channel_delete_raises(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    monkeypatch,
):
    """
    Prueba que si el canal falla al borrarse durante el rollback (p.ej. Discord completamente
    caído), ScheduleService no propaga una excepción no controlada sino que retorna
    MatchResult(success=False).
    """
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE matches, teams CASCADE;"))
        team_repo = TeamRepository(session)
        await team_repo.create(
            name="Delete Fail 1",
            tag="DF1",
            slug="delete-fail-1",
            division=Division.PREMIER,
            discord_role_id=3701,
        )
        await team_repo.create(
            name="Delete Fail 2",
            tag="DF2",
            slug="delete-fail-2",
            division=Division.PREMIER,
            discord_role_id=3702,
        )
        await session.commit()

    guild = make_mock_guild(
        test_settings,
        roles=[make_mock_role(3701, "Delete Fail 1"), make_mock_role(3702, "Delete Fail 2")],
    )

    async def mock_create_unreachable(*args, **kwargs):
        chan = AsyncMock(spec=discord.TextChannel)
        chan.id = 8888
        chan.name = "test-unreachable"
        chan.send = AsyncMock(side_effect=RuntimeError("Channel send failed"))
        chan.delete = AsyncMock(
            side_effect=discord.HTTPException(response=MagicMock(), message="API down")
        )
        return chan

    guild.create_text_channel = AsyncMock(side_effect=mock_create_unreachable)

    service = ScheduleService(session_factory=session_factory, settings=test_settings)
    res = await service.create_match(
        guild=guild, jornada=1, team1_name="Delete Fail 1", team2_name="Delete Fail 2"
    )

    # No crasheó el hilo/corutina
    assert res.success is False
    assert "Channel send failed" in res.error


# ---------------------------------------------------------------------------
# Vector 3: Extreme Formatting & Unicode Edge Cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "team1,team2,jornada",
    [
        ("A" * 150, "B" * 150, 1),
        ("🔥🚀🎮💀👑🏆⚡", "🛡️⚔️🎯🎲", 2),
        ("مرحبا بالعالم", "שלום עולם", 3),
        ("Łódź Žalgiris Äpfel", "Niño Español São Paulo", 4),
        ("日本語チーム", "한국팀", 5),
        ("!@#$%^&*()_+{}[]|:;'<>?,./`~", "====----====++++", 6),
        ("Team" + " " * 50 + "A", "Team" + "\t\n" + "B", 7),
        ("🔥 Team Alpha 🚀", "🛡️ Team Beta ⚔️", 8),
    ],
)
def test_format_channel_name_length_and_prefix_invariant(team1: str, team2: str, jornada: int):
    """
    Invariante 1: Longitud estrictamente menor o igual a 100 caracteres y prefijo canónico.
    Se cumple universalmente para cualquier entrada Unicode, longitud extrema o símbolos.
    """
    channel_name = format_match_channel_name(jornada, team1, team2)

    assert len(channel_name) <= 100
    assert len(channel_name) > 0
    assert channel_name.startswith(f"j{jornada}-")
    assert " " not in channel_name
    for char in channel_name:
        assert char.islower() or char.isdigit() or char == "-"


@pytest.mark.parametrize(
    "team1,team2,jornada,expected_slug",
    [
        (
            "Real Madrid Baloncesto",
            "Fútbol Club Barcelona",
            1,
            "j1-real-madrid-baloncesto-vs-futbol-club-barcelona",
        ),
        (
            "Atlético Dragón Über",
            "Niño Español São Paulo",
            4,
            "j4-atletico-dragon-uber-vs-nino-espanol-sao-paulo",
        ),
        ("Team" + " " * 50 + "A", "Team" + "\t\n" + "B", 7, "j7-team-a-vs-team-b"),
        ("🔥 Team Alpha 🚀", "🛡️ Team Beta ⚔️", 8, "j8-team-alpha-vs-team-beta"),
    ],
)
def test_format_channel_name_valid_discord_slug_for_latin_and_mixed(
    team1: str, team2: str, jornada: int, expected_slug: str
):
    """
    Invariante 2: Nombres con caracteres latinos, acentos en español/europeos o emojis mixtos
    producen slugs de Discord válidos y limpios sin guiones dobles ni finales.
    """
    channel_name = format_match_channel_name(jornada, team1, team2)
    assert channel_name == expected_slug
    assert not channel_name.endswith("-")
    assert "--" not in channel_name


def test_format_channel_name_truncation_200_chars_drops_team2_but_bounds_to_100():
    """
    Hallazgo de Auditoría Adversarial:
    Si team1 supera 97 caracteres, la concatenación 'j{jornada}-{slug1}-vs-{slug2}'
    es truncada a 100 caracteres exactos, eliminando por completo a team2 y '-vs-'.
    El invariante de longitud <= 100 y rstrip('-') se mantiene estrictamente.
    """
    channel_name = format_match_channel_name(1, "A" * 150, "B" * 150)
    assert len(channel_name) == 100
    assert not channel_name.endswith("-")
    assert channel_name == "j1-" + ("a" * 97)


def test_format_channel_name_non_decomposable_stroke_characters():
    """
    Hallazgo de Auditoría Adversarial:
    Caracteres con barra/trazo como 'Ł', 'Ø' o ligaduras 'ß' no tienen marcas diacríticas
    combinadas en NFKD, por lo que son filtrados por [^a-z0-9] en vez de transliterados
    (ej. 'l', 'o', 'ss'). 'Łódź' se convierte en 'odz' en lugar de 'lodz'.
    """
    assert normalize_slug("Łódź") == "odz"
    assert normalize_slug("München") == "munchen"
    assert normalize_slug("Niño") == "nino"


def test_format_channel_name_purely_non_latin_emojis_reveals_trailing_hyphen():
    """
    Hallazgo de Auditoría Adversarial:
    Cuando ambos nombres de equipo son puramente no latinos (ej. solo emojis '🔥' vs '⚡'),
    normalize_slug() retorna cadena vacía '', produciendo 'j1--vs-' con guión final.
    """
    channel_name = format_match_channel_name(1, "🔥", "⚡")
    assert len(channel_name) <= 100
    # Observación empírica: produce 'j1--vs-' debido a que slug1 y slug2 son vacíos
    assert channel_name == "j1--vs-"


def test_normalize_slug_exact_boundary_100():
    """Prueba el corte exacto en el límite de 100 caracteres."""
    long_input = "a" * 120
    slug = normalize_slug(long_input, max_length=100)
    assert len(slug) == 100
    assert slug == "a" * 100


def test_normalize_slug_trailing_hyphen_stripped_at_cutoff():
    """Prueba que si el corte cae sobre un guión, este sea eliminado con rstrip('-')."""
    # 99 'a's, un guion, luego mas 'a's
    text_input = ("a" * 99) + "-" + ("b" * 20)
    slug = normalize_slug(text_input, max_length=100)
    assert len(slug) == 99
    assert slug == "a" * 99
    assert not slug.endswith("-")


def test_normalize_tag_edge_cases():
    """Prueba normalización estricta de tags de equipo."""
    assert normalize_tag("  psp  ") == "PSP"
    assert normalize_tag("LongTag12345") == "LONG"
    assert normalize_tag("a b c d e") == "ABCD"
    assert normalize_tag("") == ""
    assert normalize_tag("   ") == ""


def test_normalize_name_diacritics_and_spaces():
    """Prueba normalización de nombres para comparaciones."""
    assert normalize_name("  Niño   Español!  ") == "nino espanol"
    assert normalize_name("Planar-Shock   Pingus") == "planarshock pingus"
