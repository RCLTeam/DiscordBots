"""
Servicio de dominio para la auditoría, vigilancia y notificación de inactividad
en canales de tickets de Discord con persistencia en SQLAlchemy 2.0.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.config import (
    DEFAULT_TICKET_AVISO_MARCADOR,
    DEFAULT_TICKET_REVISION_HOURS,
    DEFAULT_TICKETS_CATEGORY_NAMES,
    Settings,
    get_settings,
)
from liga_bot.database import get_session_factory, transactional_session
from liga_bot.repositories.ticket_repo import TicketNoticeRepository

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


class ChannelAuditStatus(str, Enum):
    """Estado resultante de la auditoría de un canal de ticket."""

    ALERT_SENT = "ALERT_SENT"
    SKIPPED_RECENT = "SKIPPED_RECENT"
    SKIPPED_STAFF = "SKIPPED_STAFF"
    SKIPPED_ALREADY_ALERTED = "SKIPPED_ALREADY_ALERTED"
    SKIPPED_EMPTY = "SKIPPED_EMPTY"
    SKIPPED_FORBIDDEN = "SKIPPED_FORBIDDEN"
    SKIPPED_ERROR = "SKIPPED_ERROR"


@dataclass(slots=True)
class ChannelAuditDetail:
    """Detalle individual de la auditoría realizada a un canal."""

    channel_id: int
    channel_name: str
    category_name: str
    status: ChannelAuditStatus
    last_message_at: datetime | None = None
    author_id: int | None = None
    author_is_staff: bool = False
    detail: str = ""


@dataclass(slots=True)
class TicketAuditResult:
    """Resultado acumulado de la auditoría de tickets."""

    categories_scanned: int = 0
    channels_scanned: int = 0
    alerts_sent: int = 0
    skipped_recent: int = 0
    skipped_staff: int = 0
    skipped_already_alerted: int = 0
    skipped_empty: int = 0
    skipped_forbidden: int = 0
    skipped_error: int = 0
    details: list[ChannelAuditDetail] = field(default_factory=list)

    def __iter__(self):
        return iter(self.details)

    def summary(self) -> str:
        """Devuelve un resumen textual de los resultados de la auditoría."""
        return (
            f"Revisión completada: {self.channels_scanned} canal(es) en "
            f"{self.categories_scanned} categoría(s). "
            f"Avisos enviados: {self.alerts_sent} | "
            f"Activos (<24h): {self.skipped_recent} | "
            f"Staff respondido: {self.skipped_staff} | "
            f"Ya avisados (<24h): {self.skipped_already_alerted} | "
            f"Vacíos/Sin permisos: {self.skipped_empty + self.skipped_forbidden}"
        )


def normalize_category_name(texto: str) -> str:
    """Normaliza tipografías Unicode estilizadas a ASCII y minúsculas."""
    return unicodedata.normalize("NFKD", texto).lower()


class TicketService:
    """
    Servicio de auditoría de inactividad de tickets.
    Controla deltas temporales >= 24h, exclusión estricta de staff y
    prevención de spam diario mediante TicketNoticeRepository.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        bot: commands.Bot | discord.Client | None = None,
        throttle_delay: float = 0.3,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.settings = settings or get_settings()
        self.bot = bot
        self.throttle_delay = throttle_delay

    @property
    def staff_role_ids(self) -> set[int]:
        """IDs de roles con privilegios de Staff/Dirección/Admin."""
        ids = {
            self.settings.staff_role_id,
            self.settings.admin_role_id,
            self.settings.ceo_premier_role_id,
            self.settings.ceo_ascend_role_id,
        }
        organizador_id = getattr(self.settings, "organizador_role_id", None)
        if organizador_id:
            ids.add(organizador_id)
        return {r for r in ids if r is not None}

    async def resolve_member(
        self, guild: discord.Guild, author: discord.User | discord.Member
    ) -> discord.Member | None:
        """Resuelve el objeto Member desde la caché o API de Discord."""
        if isinstance(author, discord.Member):
            return author
        member = guild.get_member(author.id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(author.id)
        except (discord.NotFound, discord.HTTPException):
            return None

    async def is_staff_author(
        self, guild: discord.Guild, author: discord.User | discord.Member
    ) -> bool:
        """Determina si el autor de un mensaje tiene algún rol de Staff o Admin."""
        if self.bot and self.bot.user and author.id == self.bot.user.id:
            return False
        member = await self.resolve_member(guild, author)
        if member is None:
            return False
        author_roles = {r.id for r in getattr(member, "roles", [])}
        return bool(author_roles & self.staff_role_ids)

    async def audit_channel(
        self,
        channel: discord.TextChannel,
        category_name: str | None = None,
    ) -> ChannelAuditDetail:
        """Audita un único canal de ticket y ejecuta acciones de alerta/actualización."""
        guild = channel.guild
        cat_name = category_name or (channel.category.name if channel.category else "UNKNOWN")

        try:
            ultimo = [m async for m in channel.history(limit=1)]
        except discord.Forbidden:
            logger.warning(
                "Permiso denegado para leer historial en %s (%s)",
                channel.name,
                channel.id,
            )
            return ChannelAuditDetail(
                channel_id=channel.id,
                channel_name=channel.name,
                category_name=cat_name,
                status=ChannelAuditStatus.SKIPPED_FORBIDDEN,
                detail="Bot lacks read history permission",
            )
        except discord.HTTPException as exc:
            logger.error("Error HTTP al leer canal %s: %s", channel.id, exc)
            return ChannelAuditDetail(
                channel_id=channel.id,
                channel_name=channel.name,
                category_name=cat_name,
                status=ChannelAuditStatus.SKIPPED_ERROR,
                detail=str(exc),
            )

        if not ultimo:
            return ChannelAuditDetail(
                channel_id=channel.id,
                channel_name=channel.name,
                category_name=cat_name,
                status=ChannelAuditStatus.SKIPPED_EMPTY,
                detail="Channel is empty",
            )

        mensaje = ultimo[0]
        now = datetime.now(timezone.utc)
        msg_time = mensaje.created_at
        if msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=timezone.utc)

        # Evitar auto-bucle si el último mensaje es el aviso del bot
        if (
            self.bot
            and self.bot.user
            and mensaje.author.id == self.bot.user.id
            and DEFAULT_TICKET_AVISO_MARCADOR in mensaje.content
        ):
            return ChannelAuditDetail(
                channel_id=channel.id,
                channel_name=channel.name,
                category_name=cat_name,
                status=ChannelAuditStatus.SKIPPED_ALREADY_ALERTED,
                last_message_at=msg_time,
                author_id=mensaje.author.id,
                detail="Bot alert already is last message",
            )

        is_staff = await self.is_staff_author(guild, mensaje.author)
        delta = now - msg_time
        revision_hours = getattr(
            self.settings, "ticket_revision_hours", DEFAULT_TICKET_REVISION_HOURS
        )
        threshold = timedelta(hours=revision_hours)

        async with transactional_session(self.session_factory) as session:
            repo = TicketNoticeRepository(session)

            if is_staff:
                await repo.record_staff_response(channel.id, response_time=msg_time)
                return ChannelAuditDetail(
                    channel_id=channel.id,
                    channel_name=channel.name,
                    category_name=cat_name,
                    status=ChannelAuditStatus.SKIPPED_STAFF,
                    last_message_at=msg_time,
                    author_id=mensaje.author.id,
                    author_is_staff=True,
                    detail="Last message is from staff",
                )

            if delta < threshold:
                return ChannelAuditDetail(
                    channel_id=channel.id,
                    channel_name=channel.name,
                    category_name=cat_name,
                    status=ChannelAuditStatus.SKIPPED_RECENT,
                    last_message_at=msg_time,
                    author_id=mensaje.author.id,
                    author_is_staff=False,
                    detail=f"Active ticket (delta: {delta} < {threshold})",
                )

            # Delta >= 24h y autor no es staff: comprobar si ya se envió aviso en últimas 24h
            notice = await repo.get_by_channel_id(channel.id)
            if notice is not None and notice.last_alert_sent_at is not None:
                last_alert = notice.last_alert_sent_at
                if last_alert.tzinfo is None:
                    last_alert = last_alert.replace(tzinfo=timezone.utc)
                if (now - last_alert) < threshold:
                    return ChannelAuditDetail(
                        channel_id=channel.id,
                        channel_name=channel.name,
                        category_name=cat_name,
                        status=ChannelAuditStatus.SKIPPED_ALREADY_ALERTED,
                        last_message_at=msg_time,
                        author_id=mensaje.author.id,
                        author_is_staff=False,
                        detail="Notice already sent within threshold",
                    )

            # Enviar aviso por Discord
            await self._send_alert_message(channel, guild, revision_hours)

            # Registrar aviso en BD
            await repo.record_alert(
                channel_id=channel.id,
                alert_time=now,
                category_name=cat_name,
                is_pending_staff=True,
            )

            return ChannelAuditDetail(
                channel_id=channel.id,
                channel_name=channel.name,
                category_name=cat_name,
                status=ChannelAuditStatus.ALERT_SENT,
                last_message_at=msg_time,
                author_id=mensaje.author.id,
                author_is_staff=False,
                detail="Inactivity alert dispatched and recorded in database",
            )

    async def _send_alert_message(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        revision_hours: int,
    ) -> None:
        """Construye y envía el mensaje canónico de aviso."""
        staff_role = guild.get_role(self.settings.staff_role_id)
        admin_role = guild.get_role(self.settings.admin_role_id)
        mentions_list: list[str] = []
        for r in (staff_role, admin_role):
            if r is not None:
                mention = getattr(r, "mention", None)
                if isinstance(mention, str):
                    mentions_list.append(mention)
                elif hasattr(r, "id") and isinstance(r.id, int):
                    mentions_list.append(f"<@&{r.id}>")
        menciones = " ".join(mentions_list) if mentions_list else "@Staff"

        texto = (
            f"{DEFAULT_TICKET_AVISO_MARCADOR}\n"
            f"{menciones} — Este ticket lleva más de {revision_hours}h sin respuesta del staff."
        )
        await channel.send(texto)

    async def check_tickets(
        self,
        guild: discord.Guild,
        category_name: str | None = None,
    ) -> TicketAuditResult:
        """Audita todas las categorías o una específica en el guild."""
        result = TicketAuditResult()

        if category_name:
            target_names = {normalize_category_name(category_name)}
        else:
            target_names = {normalize_category_name(n) for n in DEFAULT_TICKETS_CATEGORY_NAMES}
            custom_cat = getattr(self.settings, "tickets_category_name", None)
            if custom_cat:
                target_names.add(normalize_category_name(custom_cat))
            target_names.add("tickets")

        matching_categories = [
            cat
            for cat in guild.categories
            if normalize_category_name(cat.name) in target_names
            or any(t in normalize_category_name(cat.name) for t in target_names)
        ]
        result.categories_scanned = len(matching_categories)

        for category in matching_categories:
            channels = [c for c in category.channels if isinstance(c, discord.TextChannel)]
            for channel in channels:
                result.channels_scanned += 1
                detail = await self.audit_channel(channel, category_name=category.name)
                result.details.append(detail)

                if detail.status == ChannelAuditStatus.ALERT_SENT:
                    result.alerts_sent += 1
                elif detail.status == ChannelAuditStatus.SKIPPED_RECENT:
                    result.skipped_recent += 1
                elif detail.status == ChannelAuditStatus.SKIPPED_STAFF:
                    result.skipped_staff += 1
                elif detail.status == ChannelAuditStatus.SKIPPED_ALREADY_ALERTED:
                    result.skipped_already_alerted += 1
                elif detail.status == ChannelAuditStatus.SKIPPED_EMPTY:
                    result.skipped_empty += 1
                elif detail.status == ChannelAuditStatus.SKIPPED_FORBIDDEN:
                    result.skipped_forbidden += 1
                elif detail.status == ChannelAuditStatus.SKIPPED_ERROR:
                    result.skipped_error += 1

                if self.throttle_delay > 0:
                    await asyncio.sleep(self.throttle_delay)

        return result

    async def audit_tickets(
        self,
        guild: discord.Guild,
        category_name: str | None = None,
    ) -> TicketAuditResult:
        """Alias para compatibilidad de contrato con PROJECT.md."""
        return await self.check_tickets(guild=guild, category_name=category_name)
