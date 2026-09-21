"""
Alias de reexportación para RosterCog para compatibilidad con rutas de módulo heredadas.
"""

from __future__ import annotations

from liga_bot.cogs.roster import RosterCog, setup

__all__ = ["RosterCog", "setup"]
