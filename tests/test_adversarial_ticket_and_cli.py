"""
Batería de pruebas de estrés adversarial para TicketService y CLI Seed.

Verifica empíricamente las 10 hipótesis de falsificación y condiciones de frontera:
1. Categoría de tickets vacía (0 canales).
2. Canal con 0 mensajes (history retorna lista vacía).
3. Fronteras exactas de delta 24.0h (23h59m vs 24h00m vs 24h01m).
4. Autores anómalos: Webhook, bot propio y usuario no cacheado / borrado.
5. Autor con roles múltiples (mezcla de staff y no-staff).
6. Supresión de alertas repetidas cuando last_alert_sent_at < 24h.
7. Reseteo del ciclo de alerta cuando interviene staff (record_staff_response).
8. Throttling de rate-limit con await asyncio.sleep(0.3).
9. Idempotencia de seed-teams tras múltiples ejecuciones consecutivas.
10. Manejo elegante de errores en seed-teams ante JSON/CSV corruptos o rutas inexistentes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.cli import (
    CANONICAL_DEFAULT_TEAMS,
    run_seed_command,
    seed_teams,
)
from liga_bot.config import (
    DEFAULT_TICKET_AVISO_MARCADOR,
    Settings,
)
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.repositories.ticket_repo import TicketNoticeRepository
from liga_bot.services.ticket_service import (
    ChannelAuditStatus,
    TicketAuditResult,
    TicketService,
)

# ---------------------------------------------------------------------------
# Helpers y Mocks
# ---------------------------------------------------------------------------


class AsyncMessageHistory:
    """Simula el iterador asíncrono retornado por TextChannel.history()."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    def __aiter__(self):
        self._iter = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def create_mock_role(role_id: int, name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_category(category_id: int, name: str) -> MagicMock:
    cat = MagicMock(spec=discord.CategoryChannel)
    cat.id = category_id
    cat.name = name
    cat.channels = []
    return cat


def create_mock_channel(
    channel_id: int,
    name: str,
    category: MagicMock | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    chan = AsyncMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.category = category
    chan.guild = guild
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    chan.delete = AsyncMock()
    return chan


def create_mock_guild(
    settings: Settings,
    roles: list[MagicMock] | None = None,
    categories: list[MagicMock] | None = None,
) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = settings.guild_id

    default_role = create_mock_role(settings.guild_id, "@everyone")
    guild.default_role = default_role

    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999999999999
    bot_member.name = "LigaBot"
    guild.me = bot_member

    staff_role = create_mock_role(settings.staff_role_id, "Staff")
    admin_role = create_mock_role(settings.admin_role_id, "Admin")
    ceo_premier_role = create_mock_role(settings.ceo_premier_role_id, "CEO Premier")
    ceo_ascend_role = create_mock_role(settings.ceo_ascend_role_id, "CEO Ascend")

    all_roles = [default_role, staff_role, admin_role, ceo_premier_role, ceo_ascend_role]
    if roles:
        all_roles.extend(roles)

    role_map = {r.id: r for r in all_roles}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_map.get(rid)

    all_categories = list(categories or [])
    guild.categories = all_categories

    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    return guild


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE ticket_notices, matches, teams CASCADE;"))
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(text("TRUNCATE TABLE ticket_notices, matches, teams CASCADE;"))
        await session.commit()


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
# Tests Vector 1: TicketService Edge Cases & Boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_empty_category_scanned_gracefully(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba 1: Categoría de tickets existente pero con 0 canales."""
    guild = create_mock_guild(test_settings)
    empty_category = create_mock_category(6001, "TICKETS-GENERAL-PREMIER")
    empty_category.channels = []  # 0 canales
    guild.categories.append(empty_category)

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert isinstance(result, TicketAuditResult)
    assert result.categories_scanned == 1
    assert result.channels_scanned == 0
    assert result.alerts_sent == 0
    assert len(result.details) == 0


@pytest.mark.asyncio
async def test_ticket_channel_with_zero_messages_skipped_empty(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba 2: Canal donde channel.history(limit=1) produce una lista vacía."""
    guild = create_mock_guild(test_settings)
    category = create_mock_category(6002, "TICKETS-GENERAL-PREMIER")
    channel = create_mock_channel(6102, "ticket-empty-history", category, guild=guild)
    category.channels.append(channel)
    guild.categories.append(category)

    channel.history = MagicMock(return_value=AsyncMessageHistory([]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.skipped_empty == 1
    assert result.alerts_sent == 0
    assert len(result.details) == 1
    assert result.details[0].status == ChannelAuditStatus.SKIPPED_EMPTY
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_ticket_exact_24h_delta_boundaries(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """
    Prueba 3: Mensaje creado en los límites del delta temporal:
    - 23h 59m ago (< 24h) -> SKIPPED_RECENT (sin alerta).
    - 24h 00m 00s ago (== 24h) -> ALERT_SENT (alerta disparada).
    - 24h 01m ago (> 24h) -> ALERT_SENT (alerta disparada).
    """
    now = datetime.now(timezone.utc)
    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )

    # 3.A: 23h 59m ago
    guild_a = create_mock_guild(test_settings)
    cat_a = create_mock_category(6003, "TICKETS-PREMIER")
    chan_a = create_mock_channel(6103, "ticket-23h59m", cat_a, guild=guild_a)
    cat_a.channels.append(chan_a)
    guild_a.categories.append(cat_a)

    user_a = MagicMock(spec=discord.Member)
    user_a.id = 8001
    user_a.roles = []
    guild_a.get_member.return_value = user_a

    msg_a = MagicMock(spec=discord.Message)
    msg_a.created_at = now - timedelta(hours=23, minutes=59)
    msg_a.author = user_a
    msg_a.content = "¿Hay noticias?"
    chan_a.history = MagicMock(return_value=AsyncMessageHistory([msg_a]))

    res_a = await service.check_tickets(guild_a)
    assert res_a.alerts_sent == 0
    assert res_a.skipped_recent == 1
    assert res_a.details[0].status == ChannelAuditStatus.SKIPPED_RECENT
    chan_a.send.assert_not_called()

    # 3.B: 24h 00m 00s ago (exact boundary)
    guild_b = create_mock_guild(test_settings)
    cat_b = create_mock_category(6004, "TICKETS-PREMIER")
    chan_b = create_mock_channel(6104, "ticket-24h00m", cat_b, guild=guild_b)
    cat_b.channels.append(chan_b)
    guild_b.categories.append(cat_b)

    user_b = MagicMock(spec=discord.Member)
    user_b.id = 8002
    user_b.roles = []
    guild_b.get_member.return_value = user_b

    msg_b = MagicMock(spec=discord.Message)
    msg_b.created_at = now - timedelta(hours=24, seconds=1)  # slightly over to guarantee >= 24h
    msg_b.author = user_b
    msg_b.content = "Mensaje en el límite"
    chan_b.history = MagicMock(return_value=AsyncMessageHistory([msg_b]))

    res_b = await service.check_tickets(guild_b)
    assert res_b.alerts_sent == 1
    assert res_b.details[0].status == ChannelAuditStatus.ALERT_SENT
    chan_b.send.assert_called_once()

    # 3.C: 24h 01m ago (> 24h)
    guild_c = create_mock_guild(test_settings)
    cat_c = create_mock_category(6005, "TICKETS-PREMIER")
    chan_c = create_mock_channel(6105, "ticket-24h01m", cat_c, guild=guild_c)
    cat_c.channels.append(chan_c)
    guild_c.categories.append(cat_c)

    user_c = MagicMock(spec=discord.Member)
    user_c.id = 8003
    user_c.roles = []
    guild_c.get_member.return_value = user_c

    msg_c = MagicMock(spec=discord.Message)
    msg_c.created_at = now - timedelta(hours=24, minutes=1)
    msg_c.author = user_c
    msg_c.content = "Esperando más de 24h"
    chan_c.history = MagicMock(return_value=AsyncMessageHistory([msg_c]))

    res_c = await service.check_tickets(guild_c)
    assert res_c.alerts_sent == 1
    assert res_c.details[0].status == ChannelAuditStatus.ALERT_SENT
    chan_c.send.assert_called_once()


@pytest.mark.asyncio
async def test_ticket_author_webhook_bot_or_uncached_deleted_user(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """
    Prueba 4: Autor del mensaje es Webhook, Bot propio, o usuario borrado/no cacheado.
    """
    now = datetime.now(timezone.utc)
    mock_bot = MagicMock()
    mock_bot.user = MagicMock()
    mock_bot.user.id = 999999999999

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        bot=mock_bot,
        throttle_delay=0.0,
    )

    # 4.A: Autor Webhook (discord.User con id inexistente en el guild, fetch_member da NotFound)
    guild_wh = create_mock_guild(test_settings)
    cat_wh = create_mock_category(6006, "TICKETS-PREMIER")
    chan_wh = create_mock_channel(6106, "ticket-webhook", cat_wh, guild=guild_wh)
    cat_wh.channels.append(chan_wh)
    guild_wh.categories.append(cat_wh)

    wh_author = MagicMock(spec=discord.User)
    wh_author.id = 888888888888
    wh_author.discriminator = "0000"
    guild_wh.get_member.return_value = None
    guild_wh.fetch_member.side_effect = discord.NotFound(
        response=MagicMock(status=404), message="Unknown Member"
    )

    msg_wh = MagicMock(spec=discord.Message)
    msg_wh.created_at = now - timedelta(hours=26)
    msg_wh.author = wh_author
    msg_wh.content = "Webhook notification message"
    chan_wh.history = MagicMock(return_value=AsyncMessageHistory([msg_wh]))

    res_wh = await service.check_tickets(guild_wh)
    # Debe gestionar el NotFound elegantemente y despachar alerta sin excepción
    assert res_wh.alerts_sent == 1
    assert res_wh.details[0].author_is_staff is False
    chan_wh.send.assert_called_once()

    # 4.B: Autor es el bot propio con el marcador de aviso previo
    guild_bot_alert = create_mock_guild(test_settings)
    cat_bot_alert = create_mock_category(6007, "TICKETS-PREMIER")
    chan_bot_alert = create_mock_channel(
        6107, "ticket-bot-alert", cat_bot_alert, guild=guild_bot_alert
    )
    cat_bot_alert.channels.append(chan_bot_alert)
    guild_bot_alert.categories.append(cat_bot_alert)

    msg_bot_alert = MagicMock(spec=discord.Message)
    msg_bot_alert.created_at = now - timedelta(hours=30)
    msg_bot_alert.author = mock_bot.user
    msg_bot_alert.content = (
        f"{DEFAULT_TICKET_AVISO_MARCADOR}\n@Staff — Este ticket lleva más de 24h sin respuesta."
    )
    chan_bot_alert.history = MagicMock(return_value=AsyncMessageHistory([msg_bot_alert]))

    res_bot_alert = await service.check_tickets(guild_bot_alert)
    assert res_bot_alert.alerts_sent == 0
    assert res_bot_alert.skipped_already_alerted == 1
    chan_bot_alert.send.assert_not_called()

    # 4.C: Autor es usuario no cacheado borrado de Discord (fetch_member da NotFound)
    guild_del = create_mock_guild(test_settings)
    cat_del = create_mock_category(6008, "TICKETS-ASCEND")
    chan_del = create_mock_channel(6108, "ticket-deleted-user", cat_del, guild=guild_del)
    cat_del.channels.append(chan_del)
    guild_del.categories.append(cat_del)

    del_user = MagicMock(spec=discord.User)
    del_user.id = 777777777777
    guild_del.get_member.return_value = None
    guild_del.fetch_member.side_effect = discord.NotFound(
        response=MagicMock(status=404), message="Unknown User"
    )

    msg_del = MagicMock(spec=discord.Message)
    msg_del.created_at = now - timedelta(hours=28)
    msg_del.author = del_user
    msg_del.content = "¿Alguien me ayuda?"
    chan_del.history = MagicMock(return_value=AsyncMessageHistory([msg_del]))

    res_del = await service.check_tickets(guild_del)
    assert res_del.alerts_sent == 1
    assert res_del.details[0].author_is_staff is False
    chan_del.send.assert_called_once()


@pytest.mark.asyncio
async def test_ticket_author_multiple_roles_including_and_excluding_staff(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """
    Prueba 5: Autor con múltiples roles incluyendo y excluyendo roles de staff.
    """
    now = datetime.now(timezone.utc)
    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )

    # 5.A: Miembro con roles comunes + Rol Staff -> Debe tratarse como Staff
    guild_staff = create_mock_guild(test_settings)
    cat_staff = create_mock_category(6009, "TICKETS-PREMIER")
    chan_staff = create_mock_channel(6109, "ticket-multi-staff", cat_staff, guild=guild_staff)
    cat_staff.channels.append(chan_staff)
    guild_staff.categories.append(cat_staff)

    role_normal = create_mock_role(9001, "Normal Player")
    role_vip = create_mock_role(9002, "VIP")
    role_staff = create_mock_role(test_settings.staff_role_id, "Staff")

    multi_staff_member = MagicMock(spec=discord.Member)
    multi_staff_member.id = 8101
    multi_staff_member.roles = [role_normal, role_vip, role_staff]
    guild_staff.get_member.return_value = multi_staff_member

    msg_staff = MagicMock(spec=discord.Message)
    msg_staff.created_at = now - timedelta(hours=35)
    msg_staff.author = multi_staff_member
    msg_staff.content = "Revisado por el equipo."
    chan_staff.history = MagicMock(return_value=AsyncMessageHistory([msg_staff]))

    res_staff = await service.check_tickets(guild_staff)
    assert res_staff.alerts_sent == 0
    assert res_staff.skipped_staff == 1
    assert res_staff.details[0].author_is_staff is True
    chan_staff.send.assert_not_called()

    # 5.B: Miembro con roles comunes únicamente -> NO es staff -> Alerta disparada
    guild_user = create_mock_guild(test_settings)
    cat_user = create_mock_category(6010, "TICKETS-PREMIER")
    chan_user = create_mock_channel(6110, "ticket-multi-non-staff", cat_user, guild=guild_user)
    cat_user.channels.append(chan_user)
    guild_user.categories.append(cat_user)

    multi_user_member = MagicMock(spec=discord.Member)
    multi_user_member.id = 8102
    multi_user_member.roles = [role_normal, role_vip]  # Sin rol de staff
    guild_user.get_member.return_value = multi_user_member

    msg_user = MagicMock(spec=discord.Message)
    msg_user.created_at = now - timedelta(hours=35)
    msg_user.author = multi_user_member
    msg_user.content = "Sigo esperando."
    chan_user.history = MagicMock(return_value=AsyncMessageHistory([msg_user]))

    res_user = await service.check_tickets(guild_user)
    assert res_user.alerts_sent == 1
    assert res_user.skipped_staff == 0
    assert res_user.details[0].author_is_staff is False
    chan_user.send.assert_called_once()


@pytest.mark.asyncio
async def test_ticket_suppression_when_last_alert_less_than_24h_ago(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    """
    Prueba 6: Verificar que las alertas se suprimen cuando last_alert_sent_at < 24h.
    """
    now = datetime.now(timezone.utc)
    guild = create_mock_guild(test_settings)
    cat = create_mock_category(6011, "TICKETS-PREMIER")
    chan = create_mock_channel(6111, "ticket-suppressed", cat, guild=guild)
    cat.channels.append(chan)
    guild.categories.append(cat)

    user = MagicMock(spec=discord.Member)
    user.id = 8201
    user.roles = []
    guild.get_member.return_value = user

    msg = MagicMock(spec=discord.Message)
    msg.created_at = now - timedelta(hours=48)
    msg.author = user
    msg.content = "¿Nadie responde?"
    chan.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    # En la base de datos ya se envió un aviso hace 12 horas (< 24h)
    repo = TicketNoticeRepository(db_session)
    alert_time = now - timedelta(hours=12)
    await repo.record_alert(channel_id=6111, alert_time=alert_time, category_name="TICKETS")
    await db_session.commit()

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.alerts_sent == 0
    assert result.skipped_already_alerted == 1
    chan.send.assert_not_called()


@pytest.mark.asyncio
async def test_ticket_staff_message_resets_alert_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    test_settings: Settings,
):
    """
    Prueba 7: Verificar que la respuesta del staff resetea el ciclo de alerta
    invocando record_staff_response y limpiando is_pending_staff.
    """
    now = datetime.now(timezone.utc)
    guild = create_mock_guild(test_settings)
    cat = create_mock_category(6012, "TICKETS-PREMIER")
    chan = create_mock_channel(6112, "ticket-cycle-reset", cat, guild=guild)
    cat.channels.append(chan)
    guild.categories.append(cat)

    # Estado previo en BD: ticket con aviso enviado hace 30 horas y pendiente de staff
    repo = TicketNoticeRepository(db_session)
    prev_alert = now - timedelta(hours=30)
    notice = await repo.record_alert(
        channel_id=6112,
        alert_time=prev_alert,
        category_name="TICKETS",
        is_pending_staff=True,
    )
    await db_session.commit()
    assert notice.is_pending_staff is True

    # El staff responde hace 1 hora
    staff_member = MagicMock(spec=discord.Member)
    staff_member.id = 8301
    staff_role = create_mock_role(test_settings.staff_role_id, "Staff")
    staff_member.roles = [staff_role]
    guild.get_member.return_value = staff_member

    staff_msg_time = now - timedelta(hours=1)
    msg = MagicMock(spec=discord.Message)
    msg.created_at = staff_msg_time
    msg.author = staff_member
    msg.content = "Hola, ya nos encargamos de tu incidencia."
    chan.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.skipped_staff == 1
    assert result.alerts_sent == 0
    chan.send.assert_not_called()

    # Verificar en BD que record_staff_response limpió is_pending_staff y fijó last_staff_message_at
    async with session_factory() as verify_session:
        verify_repo = TicketNoticeRepository(verify_session)
        updated_notice = await verify_repo.get_by_channel_id(6112)
        assert updated_notice is not None
        assert updated_notice.is_pending_staff is False
        assert updated_notice.last_staff_message_at is not None


@pytest.mark.asyncio
async def test_ticket_throttling_executes_asyncio_sleep(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """
    Prueba 8: Verificar que el control de tasa ejecuta await asyncio.sleep(0.3)
    exactamente una vez por cada canal procesado.
    """
    guild = create_mock_guild(test_settings)
    cat = create_mock_category(6013, "TICKETS-PREMIER")
    # 4 canales en la categoría
    channels = [
        create_mock_channel(6113 + i, f"ticket-throttle-{i}", cat, guild=guild) for i in range(4)
    ]
    for ch in channels:
        ch.history = MagicMock(return_value=AsyncMessageHistory([]))
        cat.channels.append(ch)
    guild.categories.append(cat)

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.3,
    )

    with patch(
        "liga_bot.services.ticket_service.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        result = await service.check_tickets(guild)

        assert result.channels_scanned == 4
        assert mock_sleep.call_count == 4
        mock_sleep.assert_called_with(0.3)


# ---------------------------------------------------------------------------
# Tests Vector 2: CLI seed-teams Idempotency & Error Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_seed_teams_idempotency_multiple_executions(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
):
    """
    Prueba 9: Re-ejecutar seed-teams múltiples veces consecutivas es idempotente
    y no produce errores de clave duplicada (UniqueConstraint violation).
    """
    repo = TeamRepository(db_session)
    assert await repo.count() == 0

    # Pase 1: Inserción inicial
    stats1 = await seed_teams(session_factory, CANONICAL_DEFAULT_TEAMS)
    assert stats1["created"] == len(CANONICAL_DEFAULT_TEAMS)
    assert stats1["updated"] == 0
    assert stats1["skipped"] == 0
    assert await repo.count() == len(CANONICAL_DEFAULT_TEAMS)

    # Pase 2: Segunda ejecución idéntica -> todos skipped
    stats2 = await seed_teams(session_factory, CANONICAL_DEFAULT_TEAMS)
    assert stats2["created"] == 0
    assert stats2["updated"] == 0
    assert stats2["skipped"] == len(CANONICAL_DEFAULT_TEAMS)
    assert await repo.count() == len(CANONICAL_DEFAULT_TEAMS)

    # Pase 3: Tercera ejecución idéntica -> todos skipped
    stats3 = await seed_teams(session_factory, CANONICAL_DEFAULT_TEAMS)
    assert stats3["created"] == 0
    assert stats3["updated"] == 0
    assert stats3["skipped"] == len(CANONICAL_DEFAULT_TEAMS)
    assert await repo.count() == len(CANONICAL_DEFAULT_TEAMS)

    # Pase 4: Modificar 2 equipos -> 2 updated, 6 skipped, 0 duplicate errors
    modified_teams = list(CANONICAL_DEFAULT_TEAMS)
    modified_teams[0] = {
        "name": CANONICAL_DEFAULT_TEAMS[0]["name"],
        "tag": "NEW1",
        "division": CANONICAL_DEFAULT_TEAMS[0]["division"],
        "discord_role_id": CANONICAL_DEFAULT_TEAMS[0]["discord_role_id"],
    }
    modified_teams[1] = {
        "name": "Fnix Esports Renamed",
        "tag": CANONICAL_DEFAULT_TEAMS[1]["tag"],
        "division": CANONICAL_DEFAULT_TEAMS[1]["division"],
        "discord_role_id": CANONICAL_DEFAULT_TEAMS[1]["discord_role_id"],
    }
    stats4 = await seed_teams(session_factory, modified_teams)
    assert stats4["created"] == 0
    assert stats4["updated"] == 2
    assert stats4["skipped"] == len(CANONICAL_DEFAULT_TEAMS) - 2
    assert await repo.count() == len(CANONICAL_DEFAULT_TEAMS)


@pytest.mark.asyncio
async def test_cli_seed_teams_malformed_and_corrupted_files_graceful_exit(
    migrated_db: AsyncEngine,
    tmp_path,
    capsys,
):
    """
    Prueba 10: Rutas mal formadas, archivos inexistentes o datos corruptos (JSON/CSV)
    se manejan con gracia retornando código de salida 1 sin lanzar excepciones no capturadas.
    """
    # 10.A: Ruta inexistente
    code_nonexistent = await run_seed_command(
        file_path=str(tmp_path / "inexistente.json"),
        engine=migrated_db,
    )
    assert code_nonexistent == 1
    err_out = capsys.readouterr().err
    assert "No se encontró el archivo de datos" in err_out

    # 10.B: JSON con sintaxis rota
    broken_json = tmp_path / "broken.json"
    broken_json.write_text('{"name": "Team", "tag": "T", ', encoding="utf-8")
    code_broken = await run_seed_command(
        json_file=str(broken_json),
        engine=migrated_db,
    )
    assert code_broken == 1
    err_out = capsys.readouterr().err
    assert "Error al sembrar equipos" in err_out

    # 10.C: JSON que no es una lista (un objeto dict en vez de lista)
    dict_json = tmp_path / "dict.json"
    dict_json.write_text('{"name": "Single Team"}', encoding="utf-8")
    code_dict = await run_seed_command(
        json_file=str(dict_json),
        engine=migrated_db,
    )
    assert code_dict == 1
    err_out = capsys.readouterr().err
    assert "debe contener una lista" in err_out

    # 10.D: JSON con campo faltante obligatorio (falta discord_role_id)
    missing_field_json = tmp_path / "missing_field.json"
    missing_field_json.write_text(
        json.dumps([{"name": "Team A", "tag": "TA", "division": "PREMIER"}]),
        encoding="utf-8",
    )
    code_missing = await run_seed_command(
        json_file=str(missing_field_json),
        engine=migrated_db,
    )
    assert code_missing == 1
    err_out = capsys.readouterr().err
    assert "Falta la propiedad requerida 'discord_role_id'" in err_out

    # 10.E: JSON con división inválida
    invalid_div_json = tmp_path / "invalid_div.json"
    invalid_div_payload = [
        {"name": "Team B", "tag": "TB", "division": "CHALLENGER", "discord_role_id": 12345}
    ]
    invalid_div_json.write_text(json.dumps(invalid_div_payload), encoding="utf-8")
    code_div = await run_seed_command(
        json_file=str(invalid_div_json),
        engine=migrated_db,
    )
    assert code_div == 1
    err_out = capsys.readouterr().err
    assert "División 'CHALLENGER' inválida" in err_out

    # 10.F: JSON con discord_role_id no numérico
    bad_role_json = tmp_path / "bad_role.json"
    bad_role_payload = [
        {"name": "Team C", "tag": "TC", "division": "PREMIER", "discord_role_id": "not_an_int"}
    ]
    bad_role_json.write_text(json.dumps(bad_role_payload), encoding="utf-8")
    code_role = await run_seed_command(
        json_file=str(bad_role_json),
        engine=migrated_db,
    )
    assert code_role == 1
    err_out = capsys.readouterr().err
    assert "discord_role_id debe ser un entero válido" in err_out

    # 10.G: CSV con cabeceras incompletas o corruptas
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("name,tag\nOnlyName,ON\n", encoding="utf-8")
    code_csv = await run_seed_command(
        csv_file=str(bad_csv),
        engine=migrated_db,
    )
    assert code_csv == 1
    err_out = capsys.readouterr().err
    assert "Falta la propiedad requerida" in err_out

    # 10.H: Archivo con extensión no soportada (.xml)
    bad_ext = tmp_path / "teams.xml"
    bad_ext.write_text("<teams></teams>", encoding="utf-8")
    code_ext = await run_seed_command(
        file_path=str(bad_ext),
        engine=migrated_db,
    )
    assert code_ext == 1
    err_out = capsys.readouterr().err
    assert "Formato no soportado '.xml'" in err_out


@pytest.mark.asyncio
async def test_ticket_http_exception_on_history(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba 11: Error HTTP de Discord al leer historial produce SKIPPED_ERROR."""
    guild = create_mock_guild(test_settings)
    cat = create_mock_category(6014, "TICKETS-PREMIER")
    chan = create_mock_channel(6114, "ticket-http-error", cat, guild=guild)
    cat.channels.append(chan)
    guild.categories.append(cat)

    async def faulty_history(*args, **kwargs):
        raise discord.HTTPException(
            response=MagicMock(status=500), message="Internal Discord Outage"
        )
        yield

    chan.history = faulty_history

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.channels_scanned == 1
    assert result.skipped_error == 1
    assert result.alerts_sent == 0
    assert result.details[0].status == ChannelAuditStatus.SKIPPED_ERROR


@pytest.mark.asyncio
async def test_ticket_clock_skew_future_message(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba 12: Mensaje con timestamp futuro por reloj desfasado se trata como activo."""
    now = datetime.now(timezone.utc)
    guild = create_mock_guild(test_settings)
    cat = create_mock_category(6015, "TICKETS-PREMIER")
    chan = create_mock_channel(6115, "ticket-future-time", cat, guild=guild)
    cat.channels.append(chan)
    guild.categories.append(cat)

    user = MagicMock(spec=discord.Member)
    user.id = 8401
    user.roles = []
    guild.get_member.return_value = user

    future_msg = MagicMock(spec=discord.Message)
    future_msg.created_at = now + timedelta(hours=2)
    future_msg.author = user
    future_msg.content = "Mensaje con timestamp del futuro"
    chan.history = MagicMock(return_value=AsyncMessageHistory([future_msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.alerts_sent == 0
    assert result.skipped_recent == 1
    assert result.details[0].status == ChannelAuditStatus.SKIPPED_RECENT


@pytest.mark.asyncio
async def test_ticket_channel_without_category(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba 13: Canal que no pertenece a ninguna categoría (category is None)."""
    chan = create_mock_channel(6116, "ticket-no-cat", category=None)
    chan.category = None
    guild = create_mock_guild(test_settings)
    chan.guild = guild

    chan.history = MagicMock(return_value=AsyncMessageHistory([]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    detail = await service.audit_channel(chan, category_name=None)

    assert detail.category_name == "UNKNOWN"
    assert detail.status == ChannelAuditStatus.SKIPPED_EMPTY


@pytest.mark.asyncio
async def test_ticket_staff_response_without_prior_notice(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Prueba 14: Staff responde en ticket donde nunca se registró aviso previo."""
    guild = create_mock_guild(test_settings)
    cat = create_mock_category(6017, "TICKETS-PREMIER")
    chan = create_mock_channel(6117, "ticket-no-prior-notice", cat, guild=guild)
    cat.channels.append(chan)
    guild.categories.append(cat)

    staff_member = MagicMock(spec=discord.Member)
    staff_member.id = 8501
    staff_role = create_mock_role(test_settings.staff_role_id, "Staff")
    staff_member.roles = [staff_role]
    guild.get_member.return_value = staff_member

    msg = MagicMock(spec=discord.Message)
    msg.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    msg.author = staff_member
    msg.content = "Atención inmediata."
    chan.history = MagicMock(return_value=AsyncMessageHistory([msg]))

    service = TicketService(
        session_factory=session_factory,
        settings=test_settings,
        throttle_delay=0.0,
    )
    result = await service.check_tickets(guild)

    assert result.skipped_staff == 1
    assert result.alerts_sent == 0


@pytest.mark.asyncio
async def test_cli_seed_teams_csv_utf8_bom_and_quotes(
    migrated_db: AsyncEngine,
    tmp_path,
):
    """Prueba 15: CSV con BOM UTF-8 y campos con comillas."""
    csv_file = tmp_path / "bom_teams.csv"
    csv_content = (
        '"name","tag","division","discord_role_id"\n'
        '"Team Alpha","ALP","PREMIER","710000000000000001"\n'
        '"Team Beta","BET","ASCEND","710000000000000002"\n'
    )
    csv_file.write_text(csv_content, encoding="utf-8-sig")

    exit_code = await run_seed_command(
        csv_file=str(csv_file),
        clear=True,
        engine=migrated_db,
    )
    assert exit_code == 0


@pytest.mark.asyncio
async def test_cli_seed_teams_duplicate_records_in_same_file(
    migrated_db: AsyncEngine,
    tmp_path,
):
    """Prueba 16: Archivo con registros con mismo discord_role_id actualiza sin fallar."""
    json_file = tmp_path / "intra_duplicates.json"
    data = [
        {
            "name": "Original Name",
            "tag": "ORG",
            "division": "PREMIER",
            "discord_role_id": 999111222333444555,
        },
        {
            "name": "Updated Name",
            "tag": "UPD",
            "division": "PREMIER",
            "discord_role_id": 999111222333444555,
        },
    ]
    json_file.write_text(json.dumps(data), encoding="utf-8")

    exit_code = await run_seed_command(
        json_file=str(json_file),
        clear=True,
        engine=migrated_db,
    )
    assert exit_code == 0
