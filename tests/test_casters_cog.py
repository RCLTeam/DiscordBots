"""
Unit and integration tests for CastersCog, commands, idempotent publishing,
schedule sync, and LigaBot integration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from liga_bot.bot import DEFAULT_EXTENSIONS, LigaBot
from liga_bot.cogs.casters import CastersCog
from liga_bot.cogs.casters import setup as casters_setup
from liga_bot.config import Settings
from liga_bot.models.caster import MatchCasterCard
from liga_bot.models.enums import Division
from liga_bot.models.match import Match
from liga_bot.models.team import Team
from liga_bot.services.caster_service import CasterService, MatchCastersData
from liga_bot.ui.casters import CasterActionButton

# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


def make_test_match(
    match_id: uuid.UUID | None = None,
    jornada: int = 1,
    team1_name: str = "Team Alpha",
    team2_name: str = "Team Beta",
    scheduled_at: datetime | None = None,
    division: Division = Division.PREMIER,
) -> Match:
    """Crea una entidad Match de prueba."""
    m_id = match_id or uuid.uuid4()
    t1 = Team(id=uuid.uuid4(), name=team1_name, tag="ALP")
    t2 = Team(id=uuid.uuid4(), name=team2_name, tag="BET")
    match = Match(
        id=m_id,
        jornada=jornada,
        team1_id=t1.id,
        team2_id=t2.id,
        scheduled_at=scheduled_at,
    )
    match.team1 = t1
    match.team2 = t2
    match.division = division
    return match


def make_mock_member(
    user_id: int = 123456789,
    name: str = "TestUser",
    roles: list[discord.Role] | None = None,
    is_admin: bool = False,
    can_manage_guild: bool = False,
) -> MagicMock:
    """Crea un mock de discord.Member con roles y permisos."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = is_admin
    member.guild_permissions.manage_guild = can_manage_guild
    return member


def make_mock_role(role_id: int, name: str = "TestRole") -> MagicMock:
    """Crea un mock de discord.Role."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


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
    return msg


_SENTINEL = object()


def make_mock_interaction(
    user: discord.Member | None = None,
    guild: discord.Guild | None | object = _SENTINEL,
    channel: discord.TextChannel | None = None,
    client: Any = None,
) -> MagicMock:
    """Crea un mock de discord.Interaction."""
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user or make_mock_member()

    if guild is _SENTINEL:
        g = MagicMock(spec=discord.Guild)
        g.id = 1547725310508667010
        g.name = "RCL Server"
        g.get_channel = MagicMock(return_value=None)
        inter.guild = g
    else:
        inter.guild = guild

    inter.channel = channel or make_mock_channel()
    inter.channel_id = inter.channel.id if inter.channel else 0
    inter.client = client or MagicMock()

    response = MagicMock(spec=discord.InteractionResponse)
    response.send_message = AsyncMock()
    response.defer = AsyncMock()
    inter.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock()
    inter.followup = followup

    return inter


def make_mock_bot(
    settings: Settings | None = None,
    caster_service: CasterService | None = None,
) -> MagicMock:
    """Crea un mock de LigaBot."""
    bot = MagicMock(spec=LigaBot)
    bot.settings = settings or Settings(
        staff_role_id=101,
        admin_role_id=102,
        ceo_premier_role_id=103,
        ceo_ascend_role_id=104,
        casters_channel_id=987654321,
    )
    bot.caster_service = caster_service
    bot.session_factory = MagicMock(spec=async_sessionmaker)
    bot.cogs = {}
    bot.add_cog = AsyncMock()
    bot.add_dynamic_items = MagicMock()
    return bot


# ===========================================================================
# 1. Ciclo de Vida, Inicialización y Bot Setup
# ===========================================================================


class TestCastersCogLifecycle:
    """Pruebas del ciclo de vida de CastersCog y configuración en LigaBot."""

    def test_default_extensions_contains_casters_cog(self) -> None:
        """Verifica que DEFAULT_EXTENSIONS en bot.py incluya 'liga_bot.cogs.casters'."""
        assert "liga_bot.cogs.casters" in DEFAULT_EXTENSIONS

    @pytest.mark.asyncio
    async def test_bot_setup_hook_initializes_caster_service(self) -> None:
        """Verifica que LigaBot.setup_hook inicialice caster_service cuando es None."""
        bot = LigaBot(
            settings=Settings(bridge_enabled=False),
            extensions=(),
        )
        assert bot.caster_service is None

        # Simular setup_hook sin base de datos real
        bot.engine = MagicMock()
        bot.session_factory = MagicMock(spec=async_sessionmaker)

        await bot.setup_hook()

        assert bot.caster_service is not None
        assert isinstance(bot.caster_service, CasterService)
        assert bot.caster_service.session_factory == bot.session_factory
        assert bot.caster_service.settings == bot.settings

    def test_bot_init_accepts_custom_caster_service(self) -> None:
        """Verifica que LigaBot.__init__ admita inyección directa de caster_service."""
        mock_service = MagicMock(spec=CasterService)
        bot = LigaBot(
            settings=Settings(bridge_enabled=False),
            caster_service=mock_service,
            extensions=(),
        )
        assert bot.caster_service is mock_service

    @pytest.mark.asyncio
    async def test_cog_load_registers_dynamic_items(self) -> None:
        """Verifica que cog_load registre CasterActionButton en el bot."""
        bot = make_mock_bot()
        cog = CastersCog(bot)

        await cog.cog_load()

        bot.add_dynamic_items.assert_called_once_with(CasterActionButton)

    @pytest.mark.asyncio
    async def test_setup_registers_cog(self) -> None:
        """Verifica que la función setup registre CastersCog en el bot."""
        bot = make_mock_bot()
        await casters_setup(bot)

        bot.add_cog.assert_awaited_once()
        added_cog = bot.add_cog.await_args[0][0]
        assert isinstance(added_cog, CastersCog)

    @pytest.mark.asyncio
    async def test_setup_is_idempotent(self) -> None:
        """Verifica que setup no intente volver a registrar CastersCog si ya existe."""
        bot = make_mock_bot()
        existing_cog = CastersCog(bot)
        bot.cogs = {"CastersCog": existing_cog}

        await casters_setup(bot)

        bot.add_cog.assert_not_awaited()

    def test_cog_resilient_properties(self) -> None:
        """Verifica que session_factory y caster_service se resuelvan de forma resiliente."""
        # 1. Con servicio y session_factory inyectados explícitamente
        custom_factory = MagicMock(spec=async_sessionmaker)
        custom_service = MagicMock(spec=CasterService)
        bot = make_mock_bot()
        cog1 = CastersCog(
            bot,
            caster_service=custom_service,
            session_factory=custom_factory,
        )
        assert cog1.session_factory is custom_factory
        assert cog1.caster_service is custom_service

        # 2. Resuelto desde bot
        cog2 = CastersCog(bot)
        assert cog2.session_factory is bot.session_factory
        assert isinstance(cog2.caster_service, CasterService)

    def test_commands_metadata_and_permissions(self) -> None:
        """Verifica nombres, descripciones y permisos por defecto de los comandos slash."""
        bot = make_mock_bot()
        cog = CastersCog(bot)

        # panel-casters
        assert cog.panel_casters.name == "panel-casters"
        assert cog.panel_casters.default_permissions is not None
        assert cog.panel_casters.default_permissions.manage_guild is True
        assert len(cog.panel_casters.description) > 0

        # cartelera-casters
        assert cog.cartelera_casters.name == "cartelera-casters"
        assert cog.cartelera_casters.default_permissions is not None
        assert cog.cartelera_casters.default_permissions.manage_guild is True
        assert len(cog.cartelera_casters.description) > 0


# ===========================================================================
# 2. Verificación de Permisos
# ===========================================================================


class TestCastersCogPermissions:
    """Pruebas de autorización y control de acceso en los comandos de cartelera."""

    @pytest.mark.asyncio
    async def test_panel_casters_denied_when_unauthorized(self) -> None:
        """Verifica que un usuario sin roles autorizados reciba rechazo efímero."""
        bot = make_mock_bot()
        cog = CastersCog(bot)
        unauth_user = make_mock_member(roles=[], is_admin=False, can_manage_guild=False)
        inter = make_mock_interaction(user=unauth_user)

        await cog.panel_casters.callback(cog, inter)

        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.await_args[0][0]
        assert "❌ No tienes permisos" in msg
        assert inter.response.send_message.await_args[1].get("ephemeral") is True
        inter.response.defer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_panel_casters_allowed_for_admin_permissions(self) -> None:
        """Verifica que un usuario con permiso nativo de administrador sea admitido."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=None)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        admin_user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=admin_user)

        await cog.panel_casters.callback(cog, inter)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_panel_casters_allowed_for_staff_role(self) -> None:
        """Verifica que un miembro con rol de Staff sea admitido."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=None)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        staff_role = make_mock_role(bot.settings.staff_role_id)
        staff_user = make_mock_member(roles=[staff_role])
        inter = make_mock_interaction(user=staff_user)

        await cog.panel_casters.callback(cog, inter)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_panel_casters_allowed_for_admin_role(self) -> None:
        """Verifica que un miembro con rol de Admin sea admitido."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=None)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        admin_role = make_mock_role(bot.settings.admin_role_id)
        admin_user = make_mock_member(roles=[admin_role])
        inter = make_mock_interaction(user=admin_user)

        await cog.panel_casters.callback(cog, inter)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_panel_casters_allowed_for_ceo_premier(self) -> None:
        """Verifica que un miembro con rol de CEO Premier sea admitido."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=None)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        ceo_role = make_mock_role(bot.settings.ceo_premier_role_id)
        ceo_user = make_mock_member(roles=[ceo_role])
        inter = make_mock_interaction(user=ceo_user)

        await cog.panel_casters.callback(cog, inter)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_panel_casters_allowed_for_ceo_ascend(self) -> None:
        """Verifica que un miembro con rol de CEO Ascend sea admitido."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=None)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        ceo_role = make_mock_role(bot.settings.ceo_ascend_role_id)
        ceo_user = make_mock_member(roles=[ceo_role])
        inter = make_mock_interaction(user=ceo_user)

        await cog.panel_casters.callback(cog, inter)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_panel_casters_rejected_outside_guild(self) -> None:
        """Verifica que invocaciones fuera de un servidor (DMs) sean rechazadas."""
        bot = make_mock_bot()
        cog = CastersCog(bot)
        inter = make_mock_interaction(guild=None)

        await cog.panel_casters.callback(cog, inter)

        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.await_args[0][0]
        assert "servidor de Discord" in msg
        assert inter.response.send_message.await_args[1].get("ephemeral") is True


# ===========================================================================
# 3. Resolución de Canal de Destino
# ===========================================================================


class TestCastersCogChannelResolution:
    """Pruebas para la resolución jerárquica del canal de publicación."""

    @pytest.mark.asyncio
    async def test_explicit_canal_parameter_used(self) -> None:
        """Verifica que el parámetro canal explícito tenga máxima prioridad."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=None)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user)
        custom_chan = make_mock_channel(channel_id=777888999, name="custom-casters")

        await cog.panel_casters.callback(cog, inter, canal=custom_chan)

        # Defer ejecutado con éxito
        inter.response.defer.assert_awaited_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_channel_from_settings_used_when_canal_none(self) -> None:
        """Verifica que si no se indica canal, se resuelva el casters_channel_id de settings."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=1)
        match = make_test_match()
        service.get_matches_for_jornada = AsyncMock(return_value=[match])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )
        service.get_card = AsyncMock(return_value=None)
        service.record_card = AsyncMock()

        configured_chan = make_mock_channel(channel_id=987654321, name="canal-oficial-casters")
        configured_chan.send.return_value = make_mock_message(111222)

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user)
        inter.guild.get_channel = MagicMock(return_value=configured_chan)

        await cog.panel_casters.callback(cog, inter, canal=None)

        inter.guild.get_channel.assert_called_with(bot.settings.casters_channel_id)
        configured_chan.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_channel_fallback_to_interaction_channel(self) -> None:
        """Verifica que ante ausencia de configuración se use interaction.channel."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=1)
        match = make_test_match()
        service.get_matches_for_jornada = AsyncMock(return_value=[match])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )
        service.get_card = AsyncMock(return_value=None)
        service.record_card = AsyncMock()

        current_chan = make_mock_channel(channel_id=333444, name="general")
        current_chan.send.return_value = make_mock_message(111222)

        bot = make_mock_bot(caster_service=service)
        bot.settings.casters_channel_id = 0  # No configurado
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=current_chan)
        inter.guild.get_channel = MagicMock(return_value=None)

        await cog.panel_casters.callback(cog, inter, canal=None)

        current_chan.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_channel_not_sendable_returns_error(self) -> None:
        """Verifica que si el canal no es apto para enviar mensajes, se envíe error."""
        service = MagicMock(spec=CasterService)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        bad_channel = MagicMock()
        del bad_channel.send  # Sin método send
        inter = make_mock_interaction(user=user, channel=bad_channel)
        inter.guild.get_channel = MagicMock(return_value=None)

        await cog.panel_casters.callback(cog, inter, canal=None)

        inter.followup.send.assert_awaited_once()
        msg = inter.followup.send.await_args[0][0]
        assert "canal válido" in msg


# ===========================================================================
# 4. Manejo de Jornadas y Base de Datos Vacía
# ===========================================================================


class TestCastersCogJornadaHandling:
    """Pruebas para la resolución de jornada activa y listas de partidos."""

    @pytest.mark.asyncio
    async def test_empty_database_no_active_jornada(self) -> None:
        """Verifica que si no hay jornadas activas, se informe al usuario."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=None)
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user)

        await cog.panel_casters.callback(cog, inter, jornada=None)

        service.get_active_jornada.assert_awaited_once()
        inter.followup.send.assert_awaited_once()
        msg = inter.followup.send.await_args[0][0]
        assert "No hay jornadas activas" in msg

    @pytest.mark.asyncio
    async def test_specified_jornada_with_no_matches(self) -> None:
        """Verifica que si la jornada especificada no tiene partidos, se informe."""
        service = MagicMock(spec=CasterService)
        service.get_matches_for_jornada = AsyncMock(return_value=[])
        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user)

        await cog.panel_casters.callback(cog, inter, jornada=7)

        service.get_matches_for_jornada.assert_awaited_once_with(7)
        inter.followup.send.assert_awaited_once()
        msg = inter.followup.send.await_args[0][0]
        assert "No hay partidos programados para la Jornada 7" in msg

    @pytest.mark.asyncio
    async def test_uses_explicit_jornada(self) -> None:
        """Verifica que si se pasa jornada=3, se consulte directamente la jornada 3."""
        service = MagicMock(spec=CasterService)
        match = make_test_match(jornada=3)
        service.get_matches_for_jornada = AsyncMock(return_value=[match])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )
        service.get_card = AsyncMock(return_value=None)
        service.record_card = AsyncMock()

        chan = make_mock_channel()
        chan.send.return_value = make_mock_message()

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=chan)

        await cog.panel_casters.callback(cog, inter, jornada=3)

        service.get_active_jornada.assert_not_awaited()
        service.get_matches_for_jornada.assert_awaited_once_with(3)


# ===========================================================================
# 5. Publicación Idempotente y Sincronización In-Place
# ===========================================================================


class TestCastersCogPublishing:
    """Pruebas del bucle de publicación incremental, idempotencia y sync."""

    @pytest.mark.asyncio
    async def test_initial_publication_multiple_matches(self) -> None:
        """Verifica la publicación inicial de tarjetas para todos los partidos."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=1)

        m1 = make_test_match(jornada=1, team1_name="T1", team2_name="T2")
        m2 = make_test_match(jornada=1, team1_name="T3", team2_name="T4")
        service.get_matches_for_jornada = AsyncMock(return_value=[m1, m2])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )
        # No existen tarjetas registradas
        service.get_card = AsyncMock(return_value=None)
        service.record_card = AsyncMock()

        chan = make_mock_channel(channel_id=987654321)
        msg1 = make_mock_message(1001)
        msg2 = make_mock_message(1002)
        chan.send = AsyncMock(side_effect=[msg1, msg2])

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=chan)

        await cog.panel_casters.callback(cog, inter, jornada=1)

        assert chan.send.await_count == 2
        assert service.record_card.await_count == 2
        service.record_card.assert_any_await(m1.id, chan.id, 1001)
        service.record_card.assert_any_await(m2.id, chan.id, 1002)

        # Comprobar reporte efímero final
        inter.followup.send.assert_awaited_once()
        summary = inter.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **2**" in summary
        assert "Tarjetas sincronizadas/actualizadas: **0**" in summary

    @pytest.mark.asyncio
    async def test_idempotent_second_run_syncs_existing_cards(self) -> None:
        """Verifica que una segunda ejecución no duplique mensajes y los actualice in-place."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=1)

        m1 = make_test_match(jornada=1)
        m2 = make_test_match(jornada=1)
        service.get_matches_for_jornada = AsyncMock(return_value=[m1, m2])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )

        card1 = MatchCasterCard(match_id=m1.id, channel_id=987654321, message_id=2001)
        card2 = MatchCasterCard(match_id=m2.id, channel_id=987654321, message_id=2002)
        service.get_card = AsyncMock(side_effect=[card1, card2])
        service.record_card = AsyncMock()

        chan = make_mock_channel(channel_id=987654321)
        msg1 = make_mock_message(2001)
        msg2 = make_mock_message(2002)
        chan.fetch_message = AsyncMock(side_effect=[msg1, msg2])
        chan.send = AsyncMock()

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=chan)

        await cog.panel_casters.callback(cog, inter, jornada=1)

        # No debe enviar nuevos mensajes
        chan.send.assert_not_awaited()
        # Debe editar in-place los dos mensajes existentes
        msg1.edit.assert_awaited_once()
        msg2.edit.assert_awaited_once()

        # Resumen debe reflejar 0 publicadas y 2 sincronizadas
        inter.followup.send.assert_awaited_once()
        summary = inter.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **0**" in summary
        assert "Tarjetas sincronizadas/actualizadas: **2**" in summary

    @pytest.mark.asyncio
    async def test_schedule_sync_in_place_when_time_changes(self) -> None:
        """Verifica que el embed editado in-place incorpore el horario reprogramado."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=2)

        # Partido reprogramado con nuevo horario
        new_time = datetime(2026, 10, 15, 20, 0, 0, tzinfo=timezone.utc)
        m = make_test_match(jornada=2, scheduled_at=new_time)
        service.get_matches_for_jornada = AsyncMock(return_value=[m])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )

        card = MatchCasterCard(match_id=m.id, channel_id=987654321, message_id=3001)
        service.get_card = AsyncMock(return_value=card)

        chan = make_mock_channel(channel_id=987654321)
        msg = make_mock_message(3001)
        chan.fetch_message = AsyncMock(return_value=msg)

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=chan)

        await cog.panel_casters.callback(cog, inter, jornada=2)

        msg.edit.assert_awaited_once()
        edited_embed = msg.edit.await_args[1]["embed"]
        # Comprobar que el embed contiene el nuevo timestamp
        expected_ts = int(new_time.timestamp())
        horario_field = next(f for f in edited_embed.fields if f.name == "Horario")
        assert f"<t:{expected_ts}:F>" in horario_field.value

    @pytest.mark.asyncio
    async def test_recovery_when_message_deleted_in_discord(self) -> None:
        """Verifica que si el mensaje fue borrado en Discord (NotFound), se recupere."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=1)

        m = make_test_match(jornada=1)
        service.get_matches_for_jornada = AsyncMock(return_value=[m])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )

        card = MatchCasterCard(match_id=m.id, channel_id=987654321, message_id=4001)
        service.get_card = AsyncMock(return_value=card)
        service.delete_card = AsyncMock(return_value=True)
        service.record_card = AsyncMock()

        chan = make_mock_channel(channel_id=987654321)
        # fetch_message lanza NotFound porque el mensaje fue borrado manualmente
        chan.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "Mensaje no encontrado")
        )
        new_msg = make_mock_message(5002)
        chan.send = AsyncMock(return_value=new_msg)

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=chan)

        await cog.panel_casters.callback(cog, inter, jornada=1)

        # 1. Elimina el registro huérfano
        service.delete_card.assert_awaited_once_with(m.id, chan.id)
        # 2. Envía un mensaje nuevo
        chan.send.assert_awaited_once()
        # 3. Registra la nueva tarjeta con el nuevo message_id
        service.record_card.assert_awaited_once_with(m.id, chan.id, 5002)

        # 4. Reporte final
        inter.followup.send.assert_awaited_once()
        summary = inter.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **1**" in summary
        assert "Tarjetas sincronizadas/actualizadas: **0**" in summary

    @pytest.mark.asyncio
    async def test_recovery_when_message_fetch_http_exception(self) -> None:
        """Verifica que ante un fallo de red o error HTTP en fetch, se reposteé de forma segura."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=1)

        m = make_test_match(jornada=1)
        service.get_matches_for_jornada = AsyncMock(return_value=[m])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )

        card = MatchCasterCard(match_id=m.id, channel_id=987654321, message_id=6001)
        service.get_card = AsyncMock(return_value=card)
        service.delete_card = AsyncMock(return_value=True)
        service.record_card = AsyncMock()

        chan = make_mock_channel(channel_id=987654321)
        chan.fetch_message = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(), "Error desconocido")
        )
        new_msg = make_mock_message(6002)
        chan.send = AsyncMock(return_value=new_msg)

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=chan)

        await cog.panel_casters.callback(cog, inter, jornada=1)

        service.delete_card.assert_awaited_once_with(m.id, chan.id)
        chan.send.assert_awaited_once()
        service.record_card.assert_awaited_once_with(m.id, chan.id, 6002)


# ===========================================================================
# 6. Alias /cartelera-casters
# ===========================================================================


class TestCastersCogAlias:
    """Pruebas del comando alias /cartelera-casters."""

    @pytest.mark.asyncio
    async def test_cartelera_casters_alias_delegates_to_same_logic(self) -> None:
        """Verifica que /cartelera-casters produzca exactamente el mismo resultado."""
        service = MagicMock(spec=CasterService)
        service.get_active_jornada = AsyncMock(return_value=3)

        m = make_test_match(jornada=3)
        service.get_matches_for_jornada = AsyncMock(return_value=[m])
        service.get_match_casters_data = AsyncMock(
            return_value=MatchCastersData(has_streamer=False, streamer=None, casters=[])
        )
        service.get_card = AsyncMock(return_value=None)
        service.record_card = AsyncMock()

        chan = make_mock_channel(channel_id=987654321)
        msg = make_mock_message(7001)
        chan.send = AsyncMock(return_value=msg)

        bot = make_mock_bot(caster_service=service)
        cog = CastersCog(bot, caster_service=service)

        user = make_mock_member(is_admin=True)
        inter = make_mock_interaction(user=user, channel=chan)

        await cog.cartelera_casters.callback(cog, inter, jornada=3)

        chan.send.assert_awaited_once()
        service.record_card.assert_awaited_once_with(m.id, chan.id, 7001)
        inter.followup.send.assert_awaited_once()
        summary = inter.followup.send.await_args[0][0]
        assert "Tarjetas publicadas: **1**" in summary
