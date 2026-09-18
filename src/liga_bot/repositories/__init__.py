"""
Módulo de repositorios asíncronos de persistencia para LigaBot.
"""

from liga_bot.repositories.base import BaseRepository
from liga_bot.repositories.match_repo import MatchRepository
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.repositories.ticket_repo import TicketNoticeRepository, TicketRepository

__all__ = [
    "BaseRepository",
    "MatchRepository",
    "TeamRepository",
    "TicketNoticeRepository",
    "TicketRepository",
]
