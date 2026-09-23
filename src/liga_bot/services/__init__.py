"""Módulo de servicios de dominio de LigaBot."""

from liga_bot.services.bridge_protocol import extract_request_id
from liga_bot.services.rate_limiter import SlidingWindowRateLimiter
from liga_bot.services.role_service import RoleService
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    InvalidCaptainRoleError,
    PlayerNotTeamMemberError,
    RosterSyncError,
    RosterSyncService,
)
from liga_bot.services.schedule_service import (
    JornadaResult,
    MatchError,
    MatchResult,
    ScheduleService,
)
from liga_bot.services.suggestion_service import (
    SuggestionDeliveryError,
    SuggestionService,
)
from liga_bot.services.ticket_service import (
    ChannelAuditDetail,
    ChannelAuditStatus,
    TicketAuditResult,
    TicketService,
)
from liga_bot.services.websocket_bridge_service import WebsocketBridgeService

__all__ = [
    "ChannelAuditDetail",
    "ChannelAuditStatus",
    "CompetitivePositionConflictError",
    "InvalidCaptainRoleError",
    "JornadaResult",
    "MatchError",
    "MatchResult",
    "PlayerNotTeamMemberError",
    "RoleService",
    "RosterSyncError",
    "RosterSyncService",
    "ScheduleService",
    "SlidingWindowRateLimiter",
    "SuggestionDeliveryError",
    "SuggestionService",
    "TicketAuditResult",
    "TicketService",
    "WebsocketBridgeService",
    "extract_request_id",
]
