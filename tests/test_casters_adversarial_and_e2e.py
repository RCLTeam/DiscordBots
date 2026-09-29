"""
Comprehensive 5-Tier E2E and Adversarial test suite for casters panel,
dynamic button interactions, concurrency hardening, schedule synchronization,
and self-healing idempotency.

Tiers:
- Tier 1: Feature Coverage (Full E2E commands, all 4 role buttons, in-place embed edit)
- Tier 2: Boundary & Corner Cases (64-bit snowflakes, unscheduled matches, role enforcement)
- Tier 3: Cross-Feature Interactions (asyncio.gather race conditions, multi-match participation)
- Tier 4: Real-World Scenarios (idempotent rerun sync, match rescheduling, simulated bot restart)
- Tier 5: Adversarial Hardening (message deletion self-healing, multi-user churn & contention)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from py_pglite.sqlalchemy.manager_async import SQLAlchemyAsyncPGliteManager
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.cogs.casters import CastersCog
from liga_bot.config import Settings
from liga_bot.database import _engine_locks, _pglite_managers
from liga_bot.models.caster import CasterRole, MatchCaster
from liga_bot.models.enums import Division
from liga_bot.models.match import Match
from liga_bot.models.team import Team
from liga_bot.services.caster_service import CasterService
from liga_bot.ui.casters import (
    CasterActionButton,
    MatchCasterView,
    build_match_caster_embed,
)

# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def clean_database(
    migrated_db: AsyncEngine,
    pglite_manager: SQLAlchemyAsyncPGliteManager,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None, None]:
    """Limpia las tablas relacionadas y serializa el acceso al motor PGlite."""
    _pglite_managers[migrated_db] = pglite_manager
    _engine_locks[migrated_db] = asyncio.Lock()
    async with session_factory() as s:
        await s.execute(
            text("TRUNCATE TABLE match_casters, match_caster_cards, matches, teams CASCADE;")
        )
        await s.commit()
    yield
    _engine_locks[migrated_db] = asyncio.Lock()
    async with session_factory() as s:
        await s.execute(
            text("TRUNCATE TABLE match_casters, match_caster_cards, matches, teams CASCADE;")
        )
        await s.commit()


@pytest.fixture
def test_settings() -> Settings:
    """Configuración predeterminada de prueba."""
    s = Settings()
    s.casters_channel_id = 987654321
    s.caster_role_id = 0
    return s


@pytest.fixture
def caster_service(
    session_factory: async_sessionmaker[AsyncSession], test_settings: Settings
) -> CasterService:
    """Instancia real de CasterService con factoría de sesiones de prueba."""
    return CasterService(session_factory=session_factory, settings=test_settings)


def make_mock_user(
    user_id: int = 123456789,
    name: str = "TestUser",
    role_ids: list[int] | None = None,
    is_admin: bool = False,
    is_manage_guild: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Member con roles y permisos."""
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.name = name
    user.display_name = name
    user.mention = f"<@{user_id}>"

    roles = []
    for rid in role_ids or []:
        role = MagicMock(spec=discord.Role)
        role.id = rid
        roles.append(role)
    user.roles = roles

    user.guild_permissions = MagicMock()
    user.guild_permissions.administrator = is_admin
    user.guild_permissions.manage_guild = is_manage_guild
    return user


def make_mock_channel(channel_id: int = 987654321, name: str = "casters-panel") -> MagicMock:
    """Crea un mock de discord.TextChannel."""
    chan = MagicMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock()
    chan.fetch_message = AsyncMock()
    return chan


def make_mock_message(message_id: int = 555666777) -> MagicMock:
    """Crea un mock de discord.Message."""
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def make_mock_interaction(
    user: MagicMock | None = None,
    channel: MagicMock | None = None,
    settings: Settings | None = None,
    caster_service: CasterService | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    is_done: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Interaction con response y followup asíncronos."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user or make_mock_user(is_admin=True)
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = 1547725310508667010
    target_channel = channel or make_mock_channel()
    interaction.channel = target_channel
    interaction.guild.get_channel = MagicMock(return_value=target_channel)
    interaction.guild.get_member = MagicMock(return_value=interaction.user)
    interaction.guild.fetch_member = AsyncMock(return_value=interaction.user)

    client = MagicMock()
    client.settings = settings or Settings()
    client.caster_service = caster_service
    client.session_factory = session_factory
    interaction.client = client

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(return_value=is_done)
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    response.defer = AsyncMock()
    interaction.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    interaction.followup = followup

    interaction.edit_original_response = AsyncMock()
    return interaction


_team_counter = 100000000000000000


async def create_db_teams(
    session_factory: async_sessionmaker[AsyncSession],
    name1: str = "Team Alpha",
    name2: str = "Team Beta",
) -> tuple[Team, Team]:
    """Crea y persiste dos equipos en la base de datos con roles y slugs únicos."""
    global _team_counter
    _team_counter += 2
    r1 = _team_counter
    r2 = _team_counter + 1
    async with session_factory() as session:
        t1 = Team(
            name=name1,
            tag=name1[:3].upper(),
            slug=f"{name1.lower().replace(' ', '-')}-{r1}",
            division=Division.PREMIER,
            discord_role_id=r1,
        )
        t2 = Team(
            name=name2,
            tag=name2[:3].upper(),
            slug=f"{name2.lower().replace(' ', '-')}-{r2}",
            division=Division.PREMIER,
            discord_role_id=r2,
        )
        session.add_all([t1, t2])
        await session.commit()
        await session.refresh(t1)
        await session.refresh(t2)
        return t1, t2


async def create_db_match(
    session_factory: async_sessionmaker[AsyncSession],
    team1_id: uuid.UUID,
    team2_id: uuid.UUID,
    jornada: int = 1,
    scheduled_at: datetime | None = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
    division: Division = Division.PREMIER,
) -> Match:
    """Crea y persiste un partido en la base de datos."""
    async with session_factory() as session:
        match = Match(
            id=uuid.uuid4(),
            jornada=jornada,
            division=division,
            team1_id=team1_id,
            team2_id=team2_id,
            scheduled_at=scheduled_at,
        )
        session.add(match)
        await session.commit()
        await session.refresh(match)
        return match


# ===========================================================================
# Tier 1 - Feature Coverage
# ===========================================================================


class TestTier1FeatureCoverage:
    """
    Tier 1: Cobertura exhaustiva de extremo a extremo (E2E).
    - Ejecución completa de /panel-casters y /cartelera-casters contra base de datos.
    - Clics interactivos en los 4 botones (cast, stream, both, leave).
    - Verificación rigurosa de edición in-place (interaction.response.edit_message).
    """

    @pytest.mark.asyncio
    async def test_full_e2e_panel_casters_command(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """Verifica la ejecución E2E del comando /panel-casters con partidos reales."""
        t1, t2 = await create_db_teams(session_factory, "Fnatic", "G2 Esports")
        m1 = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        channel = make_mock_channel(channel_id=987654321)
        sent_msg = make_mock_message(message_id=777001)
        channel.send = AsyncMock(return_value=sent_msg)

        bot_mock = MagicMock()
        bot_mock.settings = test_settings
        bot_mock.session_factory = session_factory
        bot_mock.caster_service = caster_service

        cog = CastersCog(
            bot_mock,
            caster_service=caster_service,
            session_factory=session_factory,
            settings=test_settings,
        )

        admin_user = make_mock_user(user_id=9990001, is_admin=True)
        interaction = make_mock_interaction(
            user=admin_user,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )

        await cog.panel_casters.callback(cog, interaction, jornada=1, canal=channel)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        channel.send.assert_awaited_once()

        send_args, send_kwargs = channel.send.await_args
        embed: discord.Embed = send_kwargs.get("embed")
        view: MatchCasterView = send_kwargs.get("view")

        assert embed is not None
        assert "PREMIER · Jornada 1 · Fnatic vs G2 Esports" in embed.title
        assert "⚪ Vacante (sin cubrir)" in [f.value for f in embed.fields]

        assert view is not None
        assert len(view.children) == 4

        # Comprobar persistencia de MatchCasterCard en BD
        card = await caster_service.get_card(m1.id, channel.id)
        assert card is not None
        assert card.message_id == 777001

        # Comprobar respuesta efímera final
        interaction.followup.send.assert_awaited_once()
        summary = interaction.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **1**" in summary
        assert "Tarjetas sincronizadas/actualizadas: **0**" in summary

    @pytest.mark.asyncio
    async def test_full_e2e_cartelera_casters_alias(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """Verifica la ejecución E2E del comando alias /cartelera-casters."""
        t1, t2 = await create_db_teams(session_factory, "Movistar KOI", "Team Heretics")
        m1 = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        channel = make_mock_channel(channel_id=987654321)
        sent_msg = make_mock_message(message_id=777002)
        channel.send = AsyncMock(return_value=sent_msg)

        bot_mock = MagicMock()
        cog = CastersCog(
            bot_mock,
            caster_service=caster_service,
            session_factory=session_factory,
            settings=test_settings,
        )

        admin_user = make_mock_user(user_id=9990002, is_admin=True)
        interaction = make_mock_interaction(
            user=admin_user,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )

        await cog.cartelera_casters.callback(cog, interaction, jornada=1, canal=channel)

        channel.send.assert_awaited_once()
        card = await caster_service.get_card(m1.id, channel.id)
        assert card is not None
        assert card.message_id == 777002

        summary = interaction.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **1**" in summary

    @pytest.mark.asyncio
    async def test_button_clicks_all_four_roles_in_place_edit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Verifica el ciclo completo de pulsación de botones para los 4 roles/acciones:
        1. Castear (cast) -> In-place edit muestra caster, botones siguen habilitados.
        2. Retransmitir (stream) -> Muestra streamer (Solo PC), botones stream/both deshabilitados.
        3. Ambas mezcladas (both) -> Muestra (Caster + PC), botones stream/both deshabilitados.
        4. Desapuntarse (leave) -> Restablece vacante, botones stream/both rehabilitados.
        """
        t1, t2 = await create_db_teams(session_factory, "GiantX", "MAD Lions KOI")
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        user_id = 888111222
        user = make_mock_user(user_id=user_id, name="ProCaster")

        # 1. CLIC: Castear (cast)
        btn_cast = CasterActionButton(action="cast", match_id=match.id)
        inter_cast = make_mock_interaction(
            user=user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_cast.callback(inter_cast)

        inter_cast.response.edit_message.assert_awaited_once()
        kwargs_cast = inter_cast.response.edit_message.await_args[1]
        embed_cast: discord.Embed = kwargs_cast["embed"]
        view_cast: MatchCasterView = kwargs_cast["view"]

        assert f"<@{user_id}>" in [f.value for f in embed_cast.fields if f.name == "🎙️ Casters"][0]
        assert view_cast.btn_stream.disabled is False
        assert view_cast.btn_both.disabled is False
        inter_cast.followup.send.assert_awaited_once_with(
            "✅ Tu asignación ha sido actualizada.", ephemeral=True
        )

        # Verificar BD
        casters_data = await caster_service.get_match_casters_data(match.id)
        assert len(casters_data.casters) == 1
        assert casters_data.has_streamer is False

        # 2. CLIC: Retransmitir (stream)
        btn_stream = CasterActionButton(action="stream", match_id=match.id)
        inter_stream = make_mock_interaction(
            user=user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_stream.callback(inter_stream)

        inter_stream.response.edit_message.assert_awaited_once()
        kwargs_stream = inter_stream.response.edit_message.await_args[1]
        embed_stream: discord.Embed = kwargs_stream["embed"]
        view_stream: MatchCasterView = kwargs_stream["view"]

        streamer_field = [f.value for f in embed_stream.fields if f.name == "📺 Retransmisión"][0]
        assert f"<@{user_id}> (Solo PC)" == streamer_field
        assert view_stream.btn_stream.disabled is True
        assert view_stream.btn_both.disabled is True

        casters_data = await caster_service.get_match_casters_data(match.id)
        assert casters_data.has_streamer is True
        assert casters_data.streamer.caster_role == CasterRole.STREAMER

        # 3. CLIC: Ambas mezcladas (both)
        btn_both = CasterActionButton(action="both", match_id=match.id)
        inter_both = make_mock_interaction(
            user=user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_both.callback(inter_both)

        inter_both.response.edit_message.assert_awaited_once()
        kwargs_both = inter_both.response.edit_message.await_args[1]
        embed_both: discord.Embed = kwargs_both["embed"]
        view_both: MatchCasterView = kwargs_both["view"]

        streamer_both_field = [f.value for f in embed_both.fields if f.name == "📺 Retransmisión"][
            0
        ]
        assert f"<@{user_id}> (Caster + PC)" == streamer_both_field
        assert view_both.btn_stream.disabled is True
        assert view_both.btn_both.disabled is True

        casters_data = await caster_service.get_match_casters_data(match.id)
        assert casters_data.has_streamer is True
        assert casters_data.streamer.caster_role == CasterRole.BOTH

        # 4. CLIC: Desapuntarse (leave)
        btn_leave = CasterActionButton(action="leave", match_id=match.id)
        inter_leave = make_mock_interaction(
            user=user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_leave.callback(inter_leave)

        inter_leave.response.edit_message.assert_awaited_once()
        kwargs_leave = inter_leave.response.edit_message.await_args[1]
        embed_leave: discord.Embed = kwargs_leave["embed"]
        view_leave: MatchCasterView = kwargs_leave["view"]

        vacant_field = [f.value for f in embed_leave.fields if f.name == "📺 Retransmisión"][0]
        assert "*Vacante (disponible)*" == vacant_field
        # Botones de stream y both deben estar rehabilitados
        assert view_leave.btn_stream.disabled is False
        assert view_leave.btn_both.disabled is False

        casters_data = await caster_service.get_match_casters_data(match.id)
        assert casters_data.has_streamer is False
        assert len(casters_data.casters) == 0


# ===========================================================================
# Tier 2 - Boundary & Corner Cases
# ===========================================================================


class TestTier2BoundaryAndCornerCases:
    """
    Tier 2: Casos límite y extremos.
    - Snowflakes extremos de 64 bits (hasta 2^63 - 1 = 9223372036854775807).
    - Partidos sin programar (scheduled_at = None).
    - Restricción y bypass de caster_role_id > 0.
    """

    @pytest.mark.asyncio
    async def test_snowflake_extremes_persistence_and_rendering(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """Verifica la persistencia y renderizado con los valores máximos de enteros de 64 bits."""
        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        max_snowflake_user = (1 << 63) - 1  # 9223372036854775807
        max_snowflake_channel = (1 << 63) - 2
        max_snowflake_msg = (1 << 63) - 3
        min_snowflake_user = 1

        # 1. Asignar usuario con máximo snowflake como STREAMER
        res_streamer = await caster_service.assign_caster(
            match.id, max_snowflake_user, CasterRole.STREAMER
        )
        assert res_streamer.success is True

        # 2. Asignar usuario con mínimo snowflake como CASTER
        res_caster = await caster_service.assign_caster(
            match.id, min_snowflake_user, CasterRole.CASTER
        )
        assert res_caster.success is True

        # 3. Registrar tarjeta con snowflakes extremos
        card = await caster_service.record_card(match.id, max_snowflake_channel, max_snowflake_msg)
        assert card.channel_id == max_snowflake_channel
        assert card.message_id == max_snowflake_msg

        # 4. Validar recuperación desde BD
        recovered_card = await caster_service.get_card(match.id, max_snowflake_channel)
        assert recovered_card is not None
        assert recovered_card.message_id == max_snowflake_msg

        casters_data = await caster_service.get_match_casters_data(match.id)
        assert casters_data.has_streamer is True
        assert casters_data.streamer.discord_user_id == max_snowflake_user
        assert casters_data.casters[0].discord_user_id == min_snowflake_user

        # 5. Renderizado en embed sin desbordamiento ni truncamiento
        embed = build_match_caster_embed(match, casters_data)
        assert (
            f"<@{max_snowflake_user}> (Solo PC)"
            in [f.value for f in embed.fields if f.name == "📺 Retransmisión"][0]
        )
        assert (
            f"<@{min_snowflake_user}>"
            in [f.value for f in embed.fields if f.name == "🎙️ Casters"][0]
        )

        # 6. Ejecución de botón con interacción de usuario max_snowflake
        user_max = make_mock_user(user_id=max_snowflake_user)
        btn_leave = CasterActionButton(action="leave", match_id=match.id)
        inter = make_mock_interaction(
            user=user_max,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_leave.callback(inter)
        inter.response.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unscheduled_matches_null_scheduled_at(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """Verifica que un partido con scheduled_at=None renderice 'Por determinar'
        y funcione el flujo.
        """
        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1, scheduled_at=None)

        casters_data = await caster_service.get_match_casters_data(match.id)
        embed = build_match_caster_embed(match, casters_data)

        horario_field = [f.value for f in embed.fields if f.name == "Horario"][0]
        assert horario_field == "*Por determinar*"

        # Clic de botón en partido no programado
        user = make_mock_user(user_id=12345)
        btn = CasterActionButton(action="cast", match_id=match.id)
        inter = make_mock_interaction(
            user=user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn.callback(inter)
        inter.response.edit_message.assert_awaited_once()
        new_embed = inter.response.edit_message.await_args[1]["embed"]
        assert [f.value for f in new_embed.fields if f.name == "Horario"][0] == "*Por determinar*"

    @pytest.mark.asyncio
    async def test_role_check_enforcement_and_staff_bypass(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
    ) -> None:
        """
        Verifica que si caster_role_id > 0:
        1. Usuarios sin el rol son rechazados con mensaje efímero.
        2. Usuarios con el rol son autorizados.
        3. Miembros con permisos Staff/Admin hacen bypass sin tener el rol.
        """
        settings = Settings()
        settings.caster_role_id = 777666555
        settings.staff_role_id = 888999111

        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        # 1. Usuario no autorizado
        unauth_user = make_mock_user(user_id=101, role_ids=[12345], is_admin=False)
        inter_unauth = make_mock_interaction(
            user=unauth_user,
            settings=settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        btn = CasterActionButton(action="cast", match_id=match.id)
        await btn.callback(inter_unauth)

        inter_unauth.response.send_message.assert_awaited_once_with(
            "❌ No tienes el rol necesario para apuntarte como caster.",
            ephemeral=True,
        )
        inter_unauth.response.edit_message.assert_not_awaited()

        # 2. Usuario autorizado con caster_role_id
        auth_user = make_mock_user(user_id=102, role_ids=[777666555], is_admin=False)
        inter_auth = make_mock_interaction(
            user=auth_user,
            settings=settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn.callback(inter_auth)
        inter_auth.response.edit_message.assert_awaited_once()

        # 3. Usuario Staff sin caster_role_id (Staff bypass)
        staff_user = make_mock_user(user_id=103, role_ids=[888999111], is_admin=False)
        inter_staff = make_mock_interaction(
            user=staff_user,
            settings=settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn.callback(inter_staff)
        inter_staff.response.edit_message.assert_awaited_once()


# ===========================================================================
# Tier 3 - Cross-Feature Interactions & Combinations
# ===========================================================================


class TestTier3CrossFeatureInteractions:
    """
    Tier 3: Interacciones cruzadas y concurrencia.
    - Condición de carrera concurrente mediante asyncio.gather al disputar la retransmisión.
    - Participación multipartido del mismo usuario con roles independientes por partido.
    """

    @pytest.mark.asyncio
    async def test_concurrent_race_condition_streamer_slot(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Prueba de condición de carrera con asyncio.gather:
        Dos usuarios pulsan 'Retransmitir' concurrentemente en el mismo partido.
        Exactamente 1 gana la asignación; el otro recibe rechazo elegante.
        La base de datos mantiene exactamente 1 registro de streamer.
        """
        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        user_a = make_mock_user(user_id=10001, name="RacerA")
        user_b = make_mock_user(user_id=10002, name="RacerB")

        inter_a = make_mock_interaction(
            user=user_a,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        inter_b = make_mock_interaction(
            user=user_b,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )

        btn_a = CasterActionButton(action="stream", match_id=match.id)
        btn_b = CasterActionButton(action="stream", match_id=match.id)

        # Disparo concurrente exacto
        await asyncio.gather(btn_a.callback(inter_a), btn_b.callback(inter_b))

        # Uno de los dos debió editar el mensaje, y el otro debió recibir send_message con error
        edits_a = inter_a.response.edit_message.await_count
        edits_b = inter_b.response.edit_message.await_count
        assert edits_a + edits_b == 1, "Exactamente 1 interacción debe editar el mensaje con éxito."

        errors_a = inter_a.response.send_message.await_count
        errors_b = inter_b.response.send_message.await_count
        assert errors_a + errors_b == 1, "Exactamente 1 interacción debe ser rechazada."

        if edits_a == 1:
            winner_user = 10001
            assert "Ya hay una persona asignada" in inter_b.response.send_message.await_args[0][0]
        else:
            winner_user = 10002
            assert "Ya hay una persona asignada" in inter_a.response.send_message.await_args[0][0]

        # Verificar integridad estricta en base de datos
        async with session_factory() as s:
            stmt = select(MatchCaster).where(
                MatchCaster.match_id == match.id,
                MatchCaster.caster_role.in_([CasterRole.STREAMER, CasterRole.BOTH]),
            )
            res = await s.execute(stmt)
            streamers = res.scalars().all()
            assert len(streamers) == 1
            assert streamers[0].discord_user_id == winner_user

    @pytest.mark.asyncio
    async def test_concurrent_race_stream_vs_both(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Disputa concurrente entre un botón 'stream' y un botón 'both' usando asyncio.gather.
        Ambos compiten por la exclusividad de retransmisión.
        """
        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        user_stream = make_mock_user(user_id=20001, name="SoloStream")
        user_both = make_mock_user(user_id=20002, name="DualCast")

        inter_stream = make_mock_interaction(
            user=user_stream,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        inter_both = make_mock_interaction(
            user=user_both,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )

        btn_stream = CasterActionButton(action="stream", match_id=match.id)
        btn_both = CasterActionButton(action="both", match_id=match.id)

        await asyncio.gather(btn_stream.callback(inter_stream), btn_both.callback(inter_both))

        assert (
            inter_stream.response.edit_message.await_count
            + inter_both.response.edit_message.await_count
            == 1
        )

        async with session_factory() as s:
            stmt = select(MatchCaster).where(
                MatchCaster.match_id == match.id,
                MatchCaster.caster_role.in_([CasterRole.STREAMER, CasterRole.BOTH]),
            )
            res = await s.execute(stmt)
            assert len(res.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_multi_match_participation_same_user(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Verifica que el mismo usuario pueda tener roles distintos en múltiples partidos:
        - STREAMER en Partido 1
        - CASTER en Partido 2
        - BOTH en Partido 3
        Y que la exclusividad se aplique estrictamente por match_id.
        """
        t1, t2 = await create_db_teams(session_factory, "Team 1", "Team 2")
        t3, t4 = await create_db_teams(session_factory, "Team 3", "Team 4")
        t5, t6 = await create_db_teams(session_factory, "Team 5", "Team 6")
        m1 = await create_db_match(session_factory, t1.id, t2.id, jornada=1)
        m2 = await create_db_match(session_factory, t3.id, t4.id, jornada=1)
        m3 = await create_db_match(session_factory, t5.id, t6.id, jornada=1)

        multi_user = make_mock_user(user_id=30001, name="Omnipresent")

        # Asignar en M1 como STREAMER
        btn1 = CasterActionButton(action="stream", match_id=m1.id)
        inter1 = make_mock_interaction(
            user=multi_user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn1.callback(inter1)
        inter1.response.edit_message.assert_awaited_once()

        # Asignar en M2 como CASTER
        btn2 = CasterActionButton(action="cast", match_id=m2.id)
        inter2 = make_mock_interaction(
            user=multi_user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn2.callback(inter2)
        inter2.response.edit_message.assert_awaited_once()

        # Asignar en M3 como BOTH
        btn3 = CasterActionButton(action="both", match_id=m3.id)
        inter3 = make_mock_interaction(
            user=multi_user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn3.callback(inter3)
        inter3.response.edit_message.assert_awaited_once()

        # Validar los 3 partidos en BD
        data1 = await caster_service.get_match_casters_data(m1.id)
        data2 = await caster_service.get_match_casters_data(m2.id)
        data3 = await caster_service.get_match_casters_data(m3.id)

        assert data1.has_streamer is True
        assert data1.streamer.discord_user_id == 30001
        assert data1.streamer.caster_role == CasterRole.STREAMER

        assert data2.has_streamer is False
        assert len(data2.casters) == 1
        assert data2.casters[0].discord_user_id == 30001

        assert data3.has_streamer is True
        assert data3.streamer.discord_user_id == 30001
        assert data3.streamer.caster_role == CasterRole.BOTH
        assert len(data3.casters) == 1
        assert data3.casters[0].discord_user_id == 30001

        # Desapuntarse de M1 no debe afectar M2 ni M3
        btn_leave_1 = CasterActionButton(action="leave", match_id=m1.id)
        inter_leave_1 = make_mock_interaction(
            user=multi_user,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_leave_1.callback(inter_leave_1)

        data1_after = await caster_service.get_match_casters_data(m1.id)
        data2_after = await caster_service.get_match_casters_data(m2.id)
        assert data1_after.has_streamer is False
        assert data2_after.casters[0].discord_user_id == 30001


# ===========================================================================
# Tier 4 - Real-World Scenarios
# ===========================================================================


class TestTier4RealWorldScenarios:
    """
    Tier 4: Escenarios de ciclo de vida del mundo real.
    - Publicación completa de jornada y reejecución idempotente (0 nuevas, N sincronizadas).
    - Reprogramación de horario de partido y sincronización in-place en Discord.
    - Simulación de reinicio del bot y reconstrucción de DynamicItem desde custom_id crudo.
    """

    @pytest.mark.asyncio
    async def test_full_jornada_publication_and_idempotent_sync_rerun(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Verifica el ciclo de vida:
        1. Primera ejecución: se publican 3 tarjetas.
        2. Segunda ejecución: 0 nuevas, 3 sincronizadas in-place con msg.edit.
        3. Añadir un 4º partido: 1 nueva, 3 sincronizadas.
        """
        t1, t2 = await create_db_teams(session_factory, "Alpha", "Beta")
        t3, t4 = await create_db_teams(session_factory, "Gamma", "Delta")
        t5, t6 = await create_db_teams(session_factory, "Epsilon", "Zeta")
        m1 = await create_db_match(session_factory, t1.id, t2.id, jornada=2)
        m2 = await create_db_match(session_factory, t3.id, t4.id, jornada=2)
        m3 = await create_db_match(session_factory, t5.id, t6.id, jornada=2)
        assert m1.id is not None and m2.id is not None and m3.id is not None

        channel = make_mock_channel(channel_id=987654321)
        msg1 = make_mock_message(message_id=601)
        msg2 = make_mock_message(message_id=602)
        msg3 = make_mock_message(message_id=603)
        msg_map = {601: msg1, 602: msg2, 603: msg3}

        channel.send = AsyncMock(side_effect=[msg1, msg2, msg3])
        channel.fetch_message = AsyncMock(side_effect=lambda mid: msg_map.get(mid))

        bot_mock = MagicMock()
        cog = CastersCog(
            bot_mock,
            caster_service=caster_service,
            session_factory=session_factory,
            settings=test_settings,
        )

        admin = make_mock_user(user_id=111, is_admin=True)

        # RUN 1: Publicación inicial de 3 partidos
        inter1 = make_mock_interaction(
            user=admin,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await cog.panel_casters.callback(cog, inter1, jornada=2, canal=channel)

        assert channel.send.await_count == 3
        summary1 = inter1.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **3**" in summary1
        assert "Tarjetas sincronizadas/actualizadas: **0**" in summary1

        # RUN 2: Reejecución inmediata (idempotencia pura)
        channel.send.reset_mock()
        inter2 = make_mock_interaction(
            user=admin,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await cog.panel_casters.callback(cog, inter2, jornada=2, canal=channel)

        assert channel.send.await_count == 0
        assert msg1.edit.await_count == 1
        assert msg2.edit.await_count == 1
        assert msg3.edit.await_count == 1

        summary2 = inter2.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **0**" in summary2
        assert "Tarjetas sincronizadas/actualizadas: **3**" in summary2

        # RUN 3: Se añade un 4º partido al calendario
        t7, t8 = await create_db_teams(session_factory, "Eta", "Theta")
        m4 = await create_db_match(session_factory, t7.id, t8.id, jornada=2)
        assert m4.id is not None
        msg4 = make_mock_message(message_id=604)
        msg_map[604] = msg4
        channel.send = AsyncMock(return_value=msg4)

        inter3 = make_mock_interaction(
            user=admin,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await cog.panel_casters.callback(cog, inter3, jornada=2, canal=channel)

        channel.send.assert_awaited_once()
        summary3 = inter3.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **1**" in summary3
        assert "Tarjetas sincronizadas/actualizadas: **3**" in summary3

    @pytest.mark.asyncio
    async def test_match_rescheduling_updates_timestamp_preserves_casters(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Verifica que al reprogramar el horario de un partido en BD:
        - Al reejecutar /panel-casters, msg.edit actualiza el timestamp en el embed.
        - Se preservan íntegros los casters y streamers ya apuntados.
        - Se preserva el color y estado de los botones.
        """
        t1, t2 = await create_db_teams(session_factory)
        initial_time = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)
        match = await create_db_match(
            session_factory, t1.id, t2.id, jornada=1, scheduled_at=initial_time
        )

        # Asignar streamer y caster
        await caster_service.assign_caster(match.id, 4001, CasterRole.STREAMER)
        await caster_service.assign_caster(match.id, 4002, CasterRole.CASTER)

        # Publicar panel inicial
        channel = make_mock_channel(channel_id=987654321)
        mock_msg = make_mock_message(message_id=8888)
        channel.send = AsyncMock(return_value=mock_msg)
        channel.fetch_message = AsyncMock(return_value=mock_msg)

        bot_mock = MagicMock()
        cog = CastersCog(
            bot_mock,
            caster_service=caster_service,
            session_factory=session_factory,
            settings=test_settings,
        )
        admin = make_mock_user(is_admin=True)
        inter = make_mock_interaction(
            user=admin,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await cog.panel_casters.callback(cog, inter, jornada=1, canal=channel)

        # Reprogramar partido en BD
        new_time = datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)
        async with session_factory() as s:
            m_db = await s.get(Match, match.id)
            assert m_db is not None
            m_db.scheduled_at = new_time
            await s.commit()

        # Reejecutar sincronización
        inter_sync = make_mock_interaction(
            user=admin,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await cog.panel_casters.callback(cog, inter_sync, jornada=1, canal=channel)

        mock_msg.edit.assert_awaited_once()
        updated_embed: discord.Embed = mock_msg.edit.await_args[1]["embed"]

        # Validar nuevo timestamp de Discord
        new_ts = int(new_time.timestamp())
        horario_val = [f.value for f in updated_embed.fields if f.name == "Horario"][0]
        assert f"<t:{new_ts}:F>" in horario_val

        # Validar preservación de streamer y caster
        retransmision_val = [f.value for f in updated_embed.fields if f.name == "📺 Retransmisión"][
            0
        ]
        assert "<@4001> (Solo PC)" in retransmision_val
        casters_val = [f.value for f in updated_embed.fields if f.name == "🎙️ Casters"][0]
        assert "<@4002>" in casters_val

        status_val = [f.value for f in updated_embed.fields if f.name == "Estado"][0]
        assert "🟢 Cobertura lista" in status_val

    @pytest.mark.asyncio
    async def test_simulated_bot_restart_dynamic_item_reconstruction(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_settings: Settings,
    ) -> None:
        """
        Simulación de reinicio del bot:
        1. Se crea un partido en BD.
        2. La instancia anterior del bot se destruye.
        3. Se inicializa una nueva instancia de bot/servicio completamente limpia.
        4. Llega una interacción cruda de Discord con custom_id 'caster:stream:{match.id}'.
        5. from_custom_id reconstruye el CasterActionButton y ejecuta su callback.
        6. Se persiste la asignación en BD y se edita in-place el mensaje.
        """
        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        # Simular nueva instancia fresca de bot tras reinicio
        fresh_service = CasterService(session_factory=session_factory, settings=test_settings)

        custom_id = f"caster:stream:{match.id}"
        pattern = CasterActionButton.__discord_ui_compiled_template__
        regex_match = pattern.match(custom_id)
        assert regex_match is not None

        user = make_mock_user(user_id=60001, name="PostRestartUser")
        inter = make_mock_interaction(
            user=user,
            settings=test_settings,
            caster_service=fresh_service,
            session_factory=session_factory,
        )

        mock_button_item = MagicMock(spec=discord.ui.Button)
        mock_button_item.disabled = False

        # Reconstruir botón mediante from_custom_id
        reconstructed_button = await CasterActionButton.from_custom_id(
            inter, mock_button_item, regex_match
        )
        assert isinstance(reconstructed_button, CasterActionButton)
        assert reconstructed_button.action == "stream"
        assert reconstructed_button.match_id == match.id
        assert reconstructed_button.disabled is False

        # Ejecutar callback del botón reconstruido
        await reconstructed_button.callback(inter)

        inter.response.edit_message.assert_awaited_once()

        # Validar persistencia real en base de datos
        casters_data = await fresh_service.get_match_casters_data(match.id)
        assert casters_data.has_streamer is True
        assert casters_data.streamer.discord_user_id == 60001

        # Segundo clic con otro usuario debe ser rechazado
        user2 = make_mock_user(user_id=60002, name="OtherUser")
        inter2 = make_mock_interaction(
            user=user2,
            settings=test_settings,
            caster_service=fresh_service,
            session_factory=session_factory,
        )
        await reconstructed_button.callback(inter2)
        inter2.response.send_message.assert_awaited_once()
        assert "Ya hay una persona asignada" in inter2.response.send_message.await_args[0][0]


# ===========================================================================
# Tier 5 - Adversarial Coverage Hardening
# ===========================================================================


class TestTier5AdversarialCoverageHardening:
    """
    Tier 5: Endurecimiento adversarial y resiliencia ante fallos.
    - Autorreparación (self-healing) ante eliminación de mensaje en Discord (republicación limpia).
    - Desgaste (churn) continuo de casters: altas, bajas y contención concurrente de slots.
    """

    @pytest.mark.asyncio
    async def test_message_deletion_self_healing_republication(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Escenario adversarial:
        Un mensaje publicado en Discord es eliminado externamente por un moderador.
        Al ejecutarse /panel-casters, fetch_message lanza discord.NotFound.
        El sistema detecta la tarjeta huérfana, la elimina de BD, publica una nueva y
        registra el nuevo message_id sin duplicar ni fallar con 500.
        """
        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        channel = make_mock_channel(channel_id=987654321)
        stale_message_id = 999111
        new_message_id = 999222

        # Preinsertar la tarjeta con el message_id que será 'eliminado'
        await caster_service.record_card(match.id, channel.id, stale_message_id)

        # Simular que fetch_message lanza NotFound
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "Unknown Message")
        )
        new_mock_msg = make_mock_message(message_id=new_message_id)
        channel.send = AsyncMock(return_value=new_mock_msg)

        bot_mock = MagicMock()
        cog = CastersCog(
            bot_mock,
            caster_service=caster_service,
            session_factory=session_factory,
            settings=test_settings,
        )

        admin = make_mock_user(is_admin=True)
        inter = make_mock_interaction(
            user=admin,
            channel=channel,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )

        await cog.panel_casters.callback(cog, inter, jornada=1, canal=channel)

        channel.send.assert_awaited_once()

        # Comprobar reporte efímero
        summary = inter.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **1**" in summary
        assert "Tarjetas sincronizadas/actualizadas: **0**" in summary

        # Comprobar que en base de datos la tarjeta vieja fue purgada y la nueva está guardada
        updated_card = await caster_service.get_card(match.id, channel.id)
        assert updated_card is not None
        assert updated_card.message_id == new_message_id

    @pytest.mark.asyncio
    async def test_multi_user_churn_and_slot_contention_stress(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caster_service: CasterService,
        test_settings: Settings,
    ) -> None:
        """
        Prueba de estrés y contención dinámica:
        - 4 usuarios se unen como casters.
        - Un streamer se une; deshabilita retransmisión.
        - Un 5º caster se une (abierto ilimitado).
        - Otros usuarios intentan unirse a stream o both y son rechazados.
        - Un caster se desapunta (disminuye conteo).
        - El streamer se desapunta (libera exclusividad en caliente).
        - Otro usuario toma el rol 'both' en la vacante liberada.
        - El usuario de 'both' conmuta a 'cast', reabriendo la exclusividad de streamer.
        - Finalmente otro usuario toma el rol 'stream'.
        """
        t1, t2 = await create_db_teams(session_factory)
        match = await create_db_match(session_factory, t1.id, t2.id, jornada=1)

        # 1. 4 casters se unen
        for uid in range(101, 105):
            u = make_mock_user(user_id=uid)
            btn = CasterActionButton(action="cast", match_id=match.id)
            inter = make_mock_interaction(
                user=u,
                settings=test_settings,
                caster_service=caster_service,
                session_factory=session_factory,
            )
            await btn.callback(inter)
            inter.response.edit_message.assert_awaited_once()

        data = await caster_service.get_match_casters_data(match.id)
        assert len(data.casters) == 4
        assert data.has_streamer is False

        # 2. Streamer se une
        streamer_u = make_mock_user(user_id=201, name="StreamerOne")
        btn_stream = CasterActionButton(action="stream", match_id=match.id)
        inter_s = make_mock_interaction(
            user=streamer_u,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_stream.callback(inter_s)
        view_s: MatchCasterView = inter_s.response.edit_message.await_args[1]["view"]
        assert view_s.btn_stream.disabled is True
        assert view_s.btn_both.disabled is True

        # 3. 5º caster se une (abierto)
        c5 = make_mock_user(user_id=105)
        btn_c5 = CasterActionButton(action="cast", match_id=match.id)
        inter_c5 = make_mock_interaction(
            user=c5,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_c5.callback(inter_c5)
        inter_c5.response.edit_message.assert_awaited_once()

        data = await caster_service.get_match_casters_data(match.id)
        assert len(data.casters) == 5
        assert data.has_streamer is True

        # 4. Intentos de contención bloqueados
        intruder = make_mock_user(user_id=301)
        inter_block = make_mock_interaction(
            user=intruder,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        btn_both_try = CasterActionButton(action="both", match_id=match.id)
        await btn_both_try.callback(inter_block)
        inter_block.response.send_message.assert_awaited_once()
        assert "Ya hay una persona asignada" in inter_block.response.send_message.await_args[0][0]

        # 5. Caster 102 se desapunta
        c2 = make_mock_user(user_id=102)
        btn_leave_c2 = CasterActionButton(action="leave", match_id=match.id)
        inter_leave_c2 = make_mock_interaction(
            user=c2,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_leave_c2.callback(inter_leave_c2)

        data = await caster_service.get_match_casters_data(match.id)
        assert len(data.casters) == 4
        assert 102 not in [c.discord_user_id for c in data.casters]
        assert data.has_streamer is True

        # 6. Streamer 201 se desapunta -> Libera exclusividad
        btn_leave_s = CasterActionButton(action="leave", match_id=match.id)
        inter_leave_s = make_mock_interaction(
            user=streamer_u,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_leave_s.callback(inter_leave_s)
        view_freed: MatchCasterView = inter_leave_s.response.edit_message.await_args[1]["view"]
        assert view_freed.btn_stream.disabled is False
        assert view_freed.btn_both.disabled is False

        data = await caster_service.get_match_casters_data(match.id)
        assert data.has_streamer is False

        # 7. Intruso 301 ahora toma 'both'
        inter_win = make_mock_interaction(
            user=intruder,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_both_try.callback(inter_win)
        inter_win.response.edit_message.assert_awaited_once()

        data = await caster_service.get_match_casters_data(match.id)
        assert data.has_streamer is True
        assert data.streamer.discord_user_id == 301
        assert data.streamer.caster_role == CasterRole.BOTH

        # 8. Intruso 301 conmuta a 'cast' únicamente -> Libera de nuevo el streamer
        btn_switch_cast = CasterActionButton(action="cast", match_id=match.id)
        inter_switch = make_mock_interaction(
            user=intruder,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_switch_cast.callback(inter_switch)
        inter_switch.response.edit_message.assert_awaited_once()
        view_switch: MatchCasterView = inter_switch.response.edit_message.await_args[1]["view"]
        assert view_switch.btn_stream.disabled is False
        assert view_switch.btn_both.disabled is False

        data = await caster_service.get_match_casters_data(match.id)
        assert data.has_streamer is False

        # 9. Usuario 401 toma el streamer vacante
        u401 = make_mock_user(user_id=401)
        btn_401 = CasterActionButton(action="stream", match_id=match.id)
        inter_401 = make_mock_interaction(
            user=u401,
            settings=test_settings,
            caster_service=caster_service,
            session_factory=session_factory,
        )
        await btn_401.callback(inter_401)
        inter_401.response.edit_message.assert_awaited_once()

        data_final = await caster_service.get_match_casters_data(match.id)
        assert data_final.has_streamer is True
        assert data_final.streamer.discord_user_id == 401
        assert data_final.streamer.caster_role == CasterRole.STREAMER
        assert 301 in [c.discord_user_id for c in data_final.casters]
