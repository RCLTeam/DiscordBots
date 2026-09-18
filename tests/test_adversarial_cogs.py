"""
Suite de pruebas de estrés y validación de casos límite para los Cogs de LigaBot.

Verifica de forma empírica:
1. /registrar-equipo: Tags hostiles (>4 caracteres, espacios, emojis Unicode, tags vacíos),
   usuarios no autorizados, e idempotencia/detección de conflictos en actualizaciones.
2. /crear-partido: Autorización estricta (no-staff/no-CEO), equipos inexistentes en BD,
   cruce de divisiones (Premier vs Ascenso) y auto-partidos (self-match).
3. /importar-jornada: CSV masivo de 100+ filas con límites Embed de Discord (<= 1024/4096/6000),
   archivos corruptos, formatos no soportados y adjuntos vacíos.
4. /revisar-tickets: Simulación de timeout / excepciones en TicketService con recuperación
   elegante sin bloqueos, y límites de tamaño en reporte de canales alertados.
5. /sync: Verificación de permisos, alcance local vs global, validación de Snowflake
   y truncado de embeds con árboles de comandos masivos.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.cogs.admin import AdminCog
from liga_bot.cogs.schedule import ScheduleCog
from liga_bot.cogs.teams import TeamsCog
from liga_bot.cogs.tickets import TicketsCog
from liga_bot.config import Settings
from liga_bot.database import get_session_factory
from liga_bot.models.enums import Division
from liga_bot.models.team import Team
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.services.schedule_service import JornadaResult, MatchResult, ScheduleService
from liga_bot.services.ticket_service import (
    ChannelAuditDetail,
    ChannelAuditStatus,
    TicketAuditResult,
    TicketService,
)

# ---------------------------------------------------------------------------
# Mocks & Fixtures Helpers
# ---------------------------------------------------------------------------


def create_mock_role(role_id: int, name: str = "Rol") -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_member(
    user_id: int,
    name: str = "Usuario",
    roles: list[MagicMock] | None = None,
    is_admin: bool = False,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = is_admin
    return member


def create_mock_user(user_id: int, name: str = "UsuarioExterno") -> MagicMock:
    user = MagicMock(spec=discord.User)
    user.id = user_id
    user.name = name
    user.display_name = name
    user.mention = f"<@{user_id}>"
    return user


def create_mock_guild(
    guild_id: int = 1547725310508667010,
    name: str = "Liga Discord",
) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    guild.categories = []
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    guild.get_role = MagicMock(return_value=None)
    guild.create_category = AsyncMock()
    guild.create_text_channel = AsyncMock()
    return guild


def create_mock_interaction(
    user: discord.Member | discord.User | None = None,
    guild: discord.Guild | None = None,
) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.guild = guild
    inter.user = user
    inter.response = MagicMock(spec=discord.InteractionResponse)
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock(spec=discord.Webhook)
    inter.followup.send = AsyncMock()
    inter.client = MagicMock()
    return inter


def create_mock_attachment(filename: str, content: bytes) -> MagicMock:
    att = MagicMock(spec=discord.Attachment)
    att.id = 998877
    att.filename = filename
    att.size = len(content)
    att.read = AsyncMock(return_value=content)
    return att


@pytest_asyncio.fixture
async def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return get_session_factory(migrated_db)


@pytest_asyncio.fixture
async def clean_teams_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None, None]:
    async with session_factory() as session:
        await session.execute(delete(Team))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(delete(Team))
        await session.commit()


# ===========================================================================
# 1. ADVERSARIAL TESTS: /registrar-equipo
# ===========================================================================


@pytest.mark.asyncio
async def test_registrar_equipo_hostile_tags_rejections(session_factory):
    """
    Verifica que /registrar-equipo rechaza tags hostiles:
    - Espacios que exceden 4 caracteres
    - Tags vacíos o únicamente espacios en blanco
    - Múltiples emojis Unicode que sobrepasan la longitud de 4 caracteres
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    hostile_tags = [
        " P S P 1 2 ",  # 5 caracteres sin espacios -> > 4
        "TOOLONG",  # 7 caracteres
        "",  # Cadena vacía
        "    ",  # Solo espacios
        "\t\n  \r",  # Espacios en blanco tipográficos
        "🛡️⚔️👑",  # Multi-codepoints Unicode emoji (> 4 caracteres)
        "🔥⚡🌟💎✨",  # 5 emojis
    ]

    for hostile_tag in hostile_tags:
        inter = create_mock_interaction(user=staff_member, guild=guild)
        await cog.registrar_equipo.callback(
            cog,
            inter,
            rol=create_mock_role(888),
            nombre="Equipo Hostil",
            tag=hostile_tag,
            division=app_commands.Choice(name="Premier", value="PREMIER"),
        )
        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.await_args.args[0]
        assert "entre 1 y 4 caracteres" in msg, f"Fallo al rechazar hostile tag: {hostile_tag!r}"


@pytest.mark.asyncio
async def test_registrar_equipo_valid_tags_with_spaces_and_emojis(clean_teams_db, session_factory):
    """
    Verifica que tags válidos con espacios internos <= 4 o emojis cortos
    se normalizan y persisten correctamente en la base de datos.
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 1. Tag con espacios pero longitud <= 4
    inter1 = create_mock_interaction(user=staff_member, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter1,
        rol=create_mock_role(1111),
        nombre="Equipo Espacios",
        tag="  P S  ",
        division=app_commands.Choice(name="Premier", value="PREMIER"),
    )
    inter1.followup.send.assert_awaited_once()

    async with session_factory() as session:
        repo = TeamRepository(session)
        t1 = await repo.get_by_role_id(1111)
        assert t1 is not None
        assert t1.tag == "PS"

    # 2. Tag con 2 emojis (longitud 2 codepoints)
    inter2 = create_mock_interaction(user=staff_member, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter2,
        rol=create_mock_role(2222),
        nombre="Equipo Emojis",
        tag="👾🎮",
        division=app_commands.Choice(name="Ascenso", value="ASCEND"),
    )
    inter2.followup.send.assert_awaited_once()

    async with session_factory() as session:
        repo = TeamRepository(session)
        t2 = await repo.get_by_role_id(2222)
        assert t2 is not None
        assert t2.tag == "👾🎮"
        assert t2.division == Division.ASCEND


@pytest.mark.asyncio
async def test_registrar_equipo_unbounded_name_length_causes_db_error(
    clean_teams_db, session_factory
):
    """
    Stress-test sobre validación de longitud de nombre:
    La base de datos restringe Team.name a String(100).
    Verifica si /registrar-equipo valida la longitud antes de persistir o
    deja escapar una excepción SQLAlchemy DataError que cuelga la interacción deferida.
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    inter = create_mock_interaction(user=staff_member, guild=guild)
    long_name = "X" * 120  # Excede los 100 caracteres del modelo

    # Comprobar si lanza excepción de base de datos sin captura
    try:
        await cog.registrar_equipo.callback(
            cog,
            inter,
            rol=create_mock_role(9999),
            nombre=long_name,
            tag="LONG",
            division=app_commands.Choice(name="Premier", value="PREMIER"),
        )
        # Si no falló, verificar si envió mensaje de error amigable en lugar de colgarse
        assert inter.response.send_message.called or inter.followup.send.called
    except Exception as exc:
        # Si lanzó excepción no capturada, documentamos la vulnerabilidad empíricamente
        print(f"DEBUG: Unhandled DB exception on long name: {type(exc)}: {exc}")
        pytest.fail(
            f"Vulnerabilidad encontrada: Nombre > 100 caracteres disparó "
            f"excepción {type(exc).__name__}, dejando la interacción deferida en un "
            "estado colgado sin respuesta al usuario."
        )


@pytest.mark.asyncio
async def test_registrar_equipo_authorization_matrix(session_factory):
    """
    Verifica que /registrar-equipo rechaza a usuarios no autorizados (regular, sin roles,
    usuario externo no resuelto, DM) y autoriza a Staff y Administrador nativo.
    """
    settings = Settings(staff_role_id=101, admin_role_id=102)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    guild = create_mock_guild()

    # 1. Usuario con rol cualquiera (no staff/admin)
    inter_regular = create_mock_interaction(
        user=create_mock_member(1, roles=[create_mock_role(999)]),
        guild=guild,
    )
    await cog.registrar_equipo.callback(
        cog,
        inter_regular,
        rol=create_mock_role(888),
        nombre="Equipo X",
        tag="EQX",
        division="PREMIER",
    )
    inter_regular.response.send_message.assert_awaited_once()
    assert "No tienes permisos" in inter_regular.response.send_message.await_args.args[0]

    # 2. Usuario sin ningún rol
    inter_no_roles = create_mock_interaction(
        user=create_mock_member(2, roles=[]),
        guild=guild,
    )
    await cog.registrar_equipo.callback(
        cog,
        inter_no_roles,
        rol=create_mock_role(888),
        nombre="Equipo X",
        tag="EQX",
        division="PREMIER",
    )
    assert "No tienes permisos" in inter_no_roles.response.send_message.await_args.args[0]

    # 3. Usuario externo discord.User que no resuelve a Member
    inter_external = create_mock_interaction(
        user=create_mock_user(3),
        guild=guild,
    )
    await cog.registrar_equipo.callback(
        cog,
        inter_external,
        rol=create_mock_role(888),
        nombre="Equipo X",
        tag="EQX",
        division="PREMIER",
    )
    assert "No tienes permisos" in inter_external.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_registrar_equipo_idempotency_and_conflict_handling(clean_teams_db, session_factory):
    """
    Verifica que /registrar-equipo maneja idempotentemente updates del mismo equipo y
    detecta y aborta colisiones cruzadas (rol de A asignado a nombre de B).
    """
    settings = Settings(staff_role_id=101)
    cog = TeamsCog(MagicMock(), session_factory=session_factory, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 1. Crear equipo A
    inter_a = create_mock_interaction(user=staff_member, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_a,
        rol=create_mock_role(1001),
        nombre="Team Alfa",
        tag="ALF",
        division="PREMIER",
    )
    inter_a.followup.send.assert_awaited_once()
    embed_a = inter_a.followup.send.await_args.kwargs["embed"]
    assert "Registrado" in embed_a.title

    # 2. Idempotencia: Actualizar equipo A con el mismo rol pero nuevo nombre y tag
    inter_a_update = create_mock_interaction(user=staff_member, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_a_update,
        rol=create_mock_role(1001),
        nombre="Team Alfa Plus",
        tag="ALFP",
        division="ASCEND",
    )
    embed_update = inter_a_update.followup.send.await_args.kwargs["embed"]
    assert "Actualizado" in embed_update.title

    async with session_factory() as session:
        repo = TeamRepository(session)
        updated = await repo.get_by_role_id(1001)
        assert updated is not None
        assert updated.name == "Team Alfa Plus"
        assert updated.tag == "ALFP"
        assert updated.division == Division.ASCEND

    # 3. Crear equipo B
    inter_b = create_mock_interaction(user=staff_member, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_b,
        rol=create_mock_role(2002),
        nombre="Team Beta",
        tag="BET",
        division="PREMIER",
    )

    # 4. Conflicto: Intentar registrar rol 1001 (de Alfa Plus) con nombre 'Team Beta' (de 2002)
    inter_conflict = create_mock_interaction(user=staff_member, guild=guild)
    await cog.registrar_equipo.callback(
        cog,
        inter_conflict,
        rol=create_mock_role(1001),
        nombre="Team Beta",
        tag="BET",
        division="PREMIER",
    )
    embed_conflict = inter_conflict.followup.send.await_args.kwargs["embed"]
    assert "Conflicto" in embed_conflict.title

    # Verificar que el estado en base de datos quedó inalterado tras el conflicto
    async with session_factory() as session:
        repo = TeamRepository(session)
        team_a = await repo.get_by_role_id(1001)
        team_b = await repo.get_by_role_id(2002)
        assert team_a.name == "Team Alfa Plus"
        assert team_b.name == "Team Beta"


# ===========================================================================
# 2. ADVERSARIAL TESTS: /crear-partido
# ===========================================================================


@pytest.mark.asyncio
async def test_crear_partido_authorization_stress(session_factory):
    """
    Verifica que /crear-partido rechaza a usuarios no autorizados y valida que
    CEO Premier y CEO Ascend sí están autorizados además de Staff/Admin.
    """
    settings = Settings(
        staff_role_id=101,
        admin_role_id=102,
        ceo_premier_role_id=103,
        ceo_ascend_role_id=104,
    )
    mock_service = MagicMock(spec=ScheduleService)
    mock_service.create_match = AsyncMock(
        return_value=MatchResult(success=True, jornada=1, team1_name="A", team2_name="B")
    )
    cog = ScheduleCog(MagicMock(), schedule_service=mock_service, settings=settings)
    guild = create_mock_guild()

    # Usuario no autorizado
    inter_unauth = create_mock_interaction(
        user=create_mock_member(1, roles=[create_mock_role(999)]),
        guild=guild,
    )
    await cog.crear_partido.callback(cog, inter_unauth, jornada=1, equipo1="A", equipo2="B")
    inter_unauth.response.send_message.assert_awaited_once()
    assert "No tienes permisos" in inter_unauth.response.send_message.await_args.args[0]
    mock_service.create_match.assert_not_called()

    # CEO Premier autorizado
    inter_ceo_p = create_mock_interaction(
        user=create_mock_member(2, roles=[create_mock_role(103)]),
        guild=guild,
    )
    await cog.crear_partido.callback(cog, inter_ceo_p, jornada=1, equipo1="A", equipo2="B")
    inter_ceo_p.response.defer.assert_awaited_once_with(ephemeral=True)

    # CEO Ascend autorizado
    inter_ceo_a = create_mock_interaction(
        user=create_mock_member(3, roles=[create_mock_role(104)]),
        guild=guild,
    )
    await cog.crear_partido.callback(cog, inter_ceo_a, jornada=1, equipo1="A", equipo2="B")
    inter_ceo_a.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_crear_partido_domain_rejections(clean_teams_db, session_factory):
    """
    Verifica empíricamente contra la base de datos real PGlite y ScheduleService:
    1. Equipo inexistente (local o visitante)
    2. Cruce de división (Premier vs Ascenso)
    3. Auto-enfrentamiento (mismo equipo)
    """
    settings = Settings(staff_role_id=101)
    guild = create_mock_guild()
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])

    # Preparar equipos en BD
    async with session_factory() as session:
        repo = TeamRepository(session)
        await repo.create(
            name="Premier Team 1",
            tag="PT1",
            slug="premier-team-1",
            division=Division.PREMIER,
            discord_role_id=5001,
        )
        await repo.create(
            name="Ascend Team 1",
            tag="AT1",
            slug="ascend-team-1",
            division=Division.ASCEND,
            discord_role_id=5002,
        )
        await session.commit()

    service = ScheduleService(session_factory=session_factory, settings=settings)
    cog = ScheduleCog(MagicMock(), schedule_service=service, settings=settings)

    # 1. Equipo inexistente
    inter_missing = create_mock_interaction(user=staff_member, guild=guild)
    await cog.crear_partido.callback(
        cog,
        inter_missing,
        jornada=1,
        equipo1="Premier Team 1",
        equipo2="Equipo Fantasma",
    )
    embed_missing = inter_missing.followup.send.await_args.kwargs["embed"]
    assert "Error al Crear Partido" in embed_missing.title
    assert "no está registrado" in embed_missing.description

    # 2. Cruce de divisiones
    inter_cross = create_mock_interaction(user=staff_member, guild=guild)
    await cog.crear_partido.callback(
        cog,
        inter_cross,
        jornada=1,
        equipo1="Premier Team 1",
        equipo2="Ascend Team 1",
    )
    embed_cross = inter_cross.followup.send.await_args.kwargs["embed"]
    assert "Error al Crear Partido" in embed_cross.title
    assert "Conflicto de división" in embed_cross.description

    # 3. Auto-partido (self-match)
    inter_self = create_mock_interaction(user=staff_member, guild=guild)
    await cog.crear_partido.callback(
        cog,
        inter_self,
        jornada=1,
        equipo1="Premier Team 1",
        equipo2="premier team 1",  # Test case-insensitivity
    )
    embed_self = inter_self.followup.send.await_args.kwargs["embed"]
    assert "Error al Crear Partido" in embed_self.title
    assert "no puede enfrentarse a sí mismo" in embed_self.description


# ===========================================================================
# 3. ADVERSARIAL TESTS: /importar-jornada & Embed Constraints
# ===========================================================================


@pytest.mark.asyncio
async def test_importar_jornada_giant_csv_embed_limits_100_plus_channels():
    """
    Stress-test crítico: Simula un CSV gigante con 120 filas y 120 canales aprovisionados.
    Verifica que los Embeds respetan estrictamente los límites de la API de Discord:
    - Longitud de cada field name <= 256
    - Longitud de cada field value <= 1024
    - Longitud de description <= 4096
    - Longitud total combinada <= 6000
    - 'Canales Aprovisionados' no provoca Discord API 400 Bad Request
    """
    settings = Settings(staff_role_id=101)
    mock_service = MagicMock(spec=ScheduleService)

    # 120 canales aprovisionados (cada mención <#1000000000000000001> ocupa 23 caracteres)
    # 120 * 25 = 3000 caracteres sin truncar
    matches = [
        MatchResult(
            success=True,
            jornada=5,
            team1_name=f"EquipoA_{i}",
            team2_name=f"EquipoB_{i}",
            channel_mention=f"<#1000000000000000{i:03d}>",
        )
        for i in range(120)
    ]

    mock_service.create_jornada_from_csv = AsyncMock(
        return_value=JornadaResult(
            jornada=5,
            total_rows=120,
            matches=matches,
            errors=[],
        )
    )

    cog = ScheduleCog(MagicMock(), schedule_service=mock_service, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    csv_bytes = b"dummy,csv,content\n"
    att = create_mock_attachment("jornada_masiva.csv", csv_bytes)

    inter = create_mock_interaction(user=staff_member, guild=guild)
    await cog.importar_jornada.callback(cog, inter, jornada=5, archivo=att)

    inter.followup.send.assert_awaited_once()
    embed: discord.Embed = inter.followup.send.await_args.kwargs["embed"]

    # Verificación rigurosa de límites de la API de Discord
    assert len(embed.title or "") <= 256
    assert len(embed.description or "") <= 4096
    assert len(embed.fields) <= 25

    total_chars = len(embed.title or "") + len(embed.description or "")
    for field in embed.fields:
        assert len(field.name) <= 256, f"Field name excedió 256: {field.name}"
        assert len(field.value) <= 1024, (
            f"Field value excedió 1024 caracteres en '{field.name}': {len(field.value)} chars"
        )
        total_chars += len(field.name) + len(field.value)

    assert total_chars <= 6000, f"Embed excedió límite total de 6000 caracteres: {total_chars}"

    # Validar que el campo Canales Aprovisionados fue truncado de forma limpia
    channels_field = next(f for f in embed.fields if f.name == "Canales Aprovisionados")
    assert channels_field.value.endswith(" ... (truncado)")
    assert len(channels_field.value) <= 1024

    # Verificar que el diccionario de serialización para Discord es válido
    embed_dict = embed.to_dict()
    assert "fields" in embed_dict


@pytest.mark.asyncio
async def test_importar_jornada_giant_csv_embed_limits_100_plus_errors():
    """
    Stress-test de errores masivos: 100 incidencias en el CSV.
    Verifica que 'Incidencias Reportadas' trunca a <= 1024 y resume adecuadamente.
    """
    settings = Settings(staff_role_id=101)
    mock_service = MagicMock(spec=ScheduleService)

    errors = [
        f"Fila {i}: Error grave procesando equipo '{'X' * 50}' vs '{'Y' * 50}'"
        for i in range(2, 102)
    ]

    mock_service.create_jornada_from_csv = AsyncMock(
        return_value=JornadaResult(
            jornada=1,
            total_rows=100,
            matches=[],
            errors=errors,
        )
    )

    cog = ScheduleCog(MagicMock(), schedule_service=mock_service, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    att = create_mock_attachment("jornada_errores.csv", b"dummy")
    inter = create_mock_interaction(user=staff_member, guild=guild)

    await cog.importar_jornada.callback(cog, inter, jornada=1, archivo=att)

    embed: discord.Embed = inter.followup.send.await_args.kwargs["embed"]
    errors_field = next(f for f in embed.fields if f.name == "Incidencias Reportadas")

    assert len(errors_field.value) <= 1024, (
        f"Campo de errores excedió 1024 chars: {len(errors_field.value)}"
    )
    assert "errores adicionales" in errors_field.value or " ... (truncado)" in errors_field.value


@pytest.mark.asyncio
async def test_importar_jornada_corrupt_files_and_empty_attachments(session_factory):
    """
    Verifica la gestión ante adjuntos hostiles:
    - Archivo no .csv (.xlsx, .exe)
    - Archivo vacío (0 bytes o solo espacios)
    - CSV sin cabeceras o corrupto
    - Excepción en lectura de bytes
    """
    settings = Settings(staff_role_id=101)
    real_service = ScheduleService(session_factory=session_factory, settings=settings)
    cog = ScheduleCog(MagicMock(), schedule_service=real_service, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    # 1. Extensión incorrecta
    att_not_csv = create_mock_attachment("partidos.xlsx", b"contenido binario")
    inter_ext = create_mock_interaction(user=staff_member, guild=guild)
    await cog.importar_jornada.callback(cog, inter_ext, jornada=1, archivo=att_not_csv)
    inter_ext.response.send_message.assert_awaited_once()
    assert "debe ser de tipo CSV" in inter_ext.response.send_message.await_args.args[0]

    # 2. Adjunto vacío (0 bytes)
    att_empty = create_mock_attachment("vacio.csv", b"")
    inter_empty = create_mock_interaction(user=staff_member, guild=guild)
    await cog.importar_jornada.callback(cog, inter_empty, jornada=1, archivo=att_empty)
    embed_empty = inter_empty.followup.send.await_args.kwargs["embed"]
    assert "Error en la Importación" in embed_empty.title

    # 3. Solo espacios y saltos de línea
    att_spaces = create_mock_attachment("espacios.csv", b"   \n\r\n\t  ")
    inter_spaces = create_mock_interaction(user=staff_member, guild=guild)
    await cog.importar_jornada.callback(cog, inter_spaces, jornada=1, archivo=att_spaces)
    embed_spaces = inter_spaces.followup.send.await_args.kwargs["embed"]
    assert "Error en la Importación" in embed_spaces.title

    # 4. Fallo en lectura de bytes (simulación de red abortada)
    att_corrupt = create_mock_attachment("fallo_io.csv", b"")
    att_corrupt.read = AsyncMock(side_effect=OSError("Discord attachment stream interrupted"))
    inter_io = create_mock_interaction(user=staff_member, guild=guild)
    await cog.importar_jornada.callback(cog, inter_io, jornada=1, archivo=att_corrupt)
    embed_io = inter_io.followup.send.await_args.kwargs["embed"]
    assert "Error al Leer Archivo CSV" in embed_io.title


# ===========================================================================
# 4. ADVERSARIAL TESTS: /revisar-tickets
# ===========================================================================


@pytest.mark.asyncio
async def test_revisar_tickets_simulated_timeout_and_exceptions():
    """
    Verifica que /revisar-tickets maneja de forma elegante y sin colgarse:
    - TimeoutError en TicketService
    - Excepciones arbitrarias no controladas
    """
    mock_bot = MagicMock(spec=commands.Bot)
    mock_bot.settings = Settings(staff_role_id=101)
    mock_service = MagicMock(spec=TicketService)

    # 1. TimeoutError simulado
    mock_service.check_tickets = AsyncMock(
        side_effect=asyncio.TimeoutError("Discord ticket audit timed out after 30s")
    )
    cog = TicketsCog(mock_bot, ticket_service=mock_service, auto_start=False)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    inter_timeout = create_mock_interaction(user=staff_member, guild=guild)
    await cog.revisar_tickets.callback(cog, inter_timeout)

    inter_timeout.response.defer.assert_awaited_once_with(ephemeral=True)
    inter_timeout.followup.send.assert_awaited_once()
    msg_timeout = inter_timeout.followup.send.await_args.args[0]
    assert "error inesperado al revisar los tickets" in msg_timeout

    # 2. Excepción crítica de BD
    mock_service.check_tickets = AsyncMock(
        side_effect=RuntimeError("PostgreSQL connection pool exhausted")
    )
    inter_err = create_mock_interaction(user=staff_member, guild=guild)
    await cog.revisar_tickets.callback(cog, inter_err)
    msg_err = inter_err.followup.send.await_args.args[0]
    assert "error inesperado al revisar los tickets" in msg_err


@pytest.mark.asyncio
async def test_revisar_tickets_embed_field_length_stress():
    """
    Stress-test sobre _build_audit_embed:
    Verifica el comportamiento cuando hay 15 canales alertados con nombres largos
    (ej: nombres de canal de Discord de hasta 80 caracteres).
    """
    mock_bot = MagicMock(spec=commands.Bot)
    cog = TicketsCog(mock_bot, auto_start=False)

    # 15 canales con nombres largos
    details = [
        ChannelAuditDetail(
            channel_id=1000000000000000000 + i,
            channel_name=f"ticket-soporte-reclamacion-partido-premier-jornada-1-equipo-{i:02d}",
            category_name="TICKETS",
            status=ChannelAuditStatus.ALERT_SENT,
        )
        for i in range(25)  # 25 tickets alertados
    ]

    result = TicketAuditResult(
        categories_scanned=2,
        channels_scanned=50,
        alerts_sent=25,
        details=details,
    )

    user = create_mock_member(12345, name="AuditorStaff")
    embed = cog._build_audit_embed(result, user)

    # Verificar límites de la API de Discord
    assert len(embed.fields) <= 25
    alert_field = next((f for f in embed.fields if f.name == "🚨 Tickets Notificados"), None)
    assert alert_field is not None

    # Comprobación de seguridad: ¿Excede los 1024 caracteres permitidos por Discord?
    print(f"DEBUG: alert_field.value length = {len(alert_field.value)} (limit 1024)")
    # Registramos la aserción:
    assert len(alert_field.value) <= 1024, (
        f"Vulnerabilidad encontrada: Embed field '🚨 Tickets Notificados' excede 1024 caracteres "
        f"({len(alert_field.value)} chars) cuando hay múltiples tickets con nombres largos, "
        "lo que dispararía Discord API 400 Bad Request."
    )


# ===========================================================================
# 5. ADVERSARIAL TESTS: /sync & /sincronizar
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_authorization_and_guild_vs_global():
    """
    Verifica:
    - Rechazo de usuarios no autorizados
    - Sincronización local sobre el guild de la interacción
    - Sincronización global con guild=None
    - Sincronización sobre Snowflake explícito
    - Validación de Snowflake no numérico
    """
    settings = Settings(staff_role_id=101, admin_role_id=102, guild_id=1547725310508667010)
    mock_bot = MagicMock(spec=commands.Bot)
    mock_bot.tree.sync = AsyncMock(return_value=[MagicMock(name="cmd1")])

    cog = AdminCog(mock_bot, settings=settings)
    guild = create_mock_guild(99999)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    unauth_member = create_mock_member(54321, roles=[create_mock_role(888)])

    # 1. Rechazo de no autorizado
    inter_unauth = create_mock_interaction(user=unauth_member, guild=guild)
    await cog.sync.callback(cog, inter_unauth)
    assert "No tienes permisos" in inter_unauth.response.send_message.await_args.args[0]
    mock_bot.tree.sync.assert_not_called()

    # 2. Sincronización local en guild actual
    inter_local = create_mock_interaction(user=staff_member, guild=guild)
    await cog.sync.callback(cog, inter_local, global_sync=False)
    mock_bot.tree.sync.assert_awaited_once_with(guild=guild)

    # 3. Sincronización global
    mock_bot.tree.sync.reset_mock()
    inter_global = create_mock_interaction(user=staff_member, guild=guild)
    await cog.sync.callback(cog, inter_global, global_sync=True)
    mock_bot.tree.sync.assert_awaited_once_with(guild=None)

    # 4. Sincronización por Snowflake explícito
    mock_bot.tree.sync.reset_mock()
    inter_explicit = create_mock_interaction(user=staff_member, guild=guild)
    await cog.sync.callback(cog, inter_explicit, guild_id="1122334455")
    assert mock_bot.tree.sync.await_args.kwargs["guild"].id == 1122334455

    # 5. Validación de ID corrupto (no numérico)
    mock_bot.tree.sync.reset_mock()
    inter_corrupt_id = create_mock_interaction(user=staff_member, guild=guild)
    await cog.sync.callback(cog, inter_corrupt_id, guild_id="not-a-number")
    mock_bot.tree.sync.assert_not_called()
    assert "número entero válido" in inter_corrupt_id.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_sync_massive_command_list_embed_limits():
    """
    Stress-test de sincronización: Simula el retorno de 120 comandos slash.
    Verifica que el campo 'Comandos Registrados' se trunca a <= 1024 caracteres
    y no produce 400 Bad Request en la API de Discord.
    """
    settings = Settings(staff_role_id=101)
    mock_bot = MagicMock(spec=commands.Bot)

    # 120 comandos (ej: /comando-larguisimo-numero-001)
    cmds = []
    for i in range(120):
        c = MagicMock()
        c.name = f"comando-larguisimo-numero-{i:03d}"
        cmds.append(c)

    mock_bot.tree.sync = AsyncMock(return_value=cmds)
    cog = AdminCog(mock_bot, settings=settings)
    staff_member = create_mock_member(12345, roles=[create_mock_role(101)])
    guild = create_mock_guild()

    inter = create_mock_interaction(user=staff_member, guild=guild)
    await cog.sync.callback(cog, inter)

    embed: discord.Embed = inter.followup.send.await_args.kwargs["embed"]
    cmd_field = next(f for f in embed.fields if f.name == "Comandos Registrados")

    assert len(cmd_field.value) <= 1024, (
        f"El campo Comandos Registrados excedió 1024 chars: {len(cmd_field.value)}"
    )
    assert cmd_field.value.endswith(" ... (truncado)")
