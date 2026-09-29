"""
Módulo de interfaz de usuario desacoplada de Discord para LigaBot.

Exporta modales, selectores, vistas y elementos dinámicos interactivos.
"""

from liga_bot.ui.casters import (
    CasterActionButton,
    MatchCasterView,
    build_match_caster_embed,
)
from liga_bot.ui.roles import (
    ConfirmarRolButton,
    EquipoSelect,
    EquipoSelectView,
    PanelPedirRolView,
    PosicionSelect,
    PosicionSelectView,
    SolicitudRolModal,
    TicketView,
    build_panel_rol_embed,
)
from liga_bot.ui.roster import (
    CancelButton,
    GestionarPosicionView,
    PositionSelect,
    SaveButton,
    SavePositionButton,
    TeamSelect,
)

__all__ = [
    "CancelButton",
    "CasterActionButton",
    "ConfirmarRolButton",
    "EquipoSelect",
    "EquipoSelectView",
    "GestionarPosicionView",
    "MatchCasterView",
    "PanelPedirRolView",
    "PosicionSelect",
    "PosicionSelectView",
    "PositionSelect",
    "SaveButton",
    "SavePositionButton",
    "SolicitudRolModal",
    "TeamSelect",
    "TicketView",
    "build_match_caster_embed",
    "build_panel_rol_embed",
]
