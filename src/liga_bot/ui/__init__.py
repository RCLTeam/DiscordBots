"""
Módulo de interfaz de usuario desacoplada de Discord para LigaBot.

Exporta modales, selectores, vistas y elementos dinámicos interactivos.
"""

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
    "ConfirmarRolButton",
    "EquipoSelect",
    "EquipoSelectView",
    "GestionarPosicionView",
    "PanelPedirRolView",
    "PosicionSelect",
    "PosicionSelectView",
    "PositionSelect",
    "SaveButton",
    "SavePositionButton",
    "SolicitudRolModal",
    "TeamSelect",
    "TicketView",
    "build_panel_rol_embed",
]
