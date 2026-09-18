"""Módulo de servicios de dominio de LigaBot."""

from liga_bot.services.schedule_service import (
    JornadaResult,
    MatchError,
    MatchResult,
    ScheduleService,
)
from liga_bot.services.ticket_service import (
    ChannelAuditDetail,
    ChannelAuditStatus,
    TicketAuditResult,
    TicketService,
)

__all__ = [
    "ChannelAuditDetail",
    "ChannelAuditStatus",
    "JornadaResult",
    "MatchError",
    "MatchResult",
    "ScheduleService",
    "TicketAuditResult",
    "TicketService",
]
