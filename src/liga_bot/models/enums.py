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


class RoleRequestStatus(str, enum.Enum):
    """Ciclo de vida y estado operativo de una solicitud de rol de equipo."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"


__all__ = [
    "Division",
    "MatchStatus",
    "RoleRequestStatus",
]
