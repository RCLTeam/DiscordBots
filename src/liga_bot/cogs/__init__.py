"""
Cogs package for LigaBot Discord presentation layer.
"""

from liga_bot.cogs.admin import AdminCog
from liga_bot.cogs.roles import RolesCog
from liga_bot.cogs.schedule import ScheduleCog
from liga_bot.cogs.teams import TeamsCog
from liga_bot.cogs.tickets import TicketsCog

__all__ = [
    "AdminCog",
    "RolesCog",
    "ScheduleCog",
    "TeamsCog",
    "TicketsCog",
]
