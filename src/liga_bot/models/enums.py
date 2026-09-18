"""Domain enums for LigaBot entities."""

import enum


class Division(str, enum.Enum):
    """División competitiva de la liga."""

    PREMIER = "PREMIER"
    ASCEND = "ASCEND"


class MatchStatus(str, enum.Enum):
    """Ciclo de vida y estado operativo de un partido."""

    PENDIENTE = "PENDIENTE"
    CANAL_CREADO = "CANAL_CREADO"
    JUGADO = "JUGADO"
    CANCELADO = "CANCELADO"
