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


class AppRole(str, enum.Enum):
    """Rol de aplicación en la plataforma RCL (alineado con app_role en RCL-Next)."""

    VIEWER = "viewer"
    ADMIN = "admin"


class RosterRole(str, enum.Enum):
    """
    Rol y posición en la plantilla de un equipo (alineado con roster_role en RCL-Next).

    Valores permitidos:
    'top', 'jungle', 'mid', 'adc', 'support', 'substitute', 'coach', 'staff', 'partners'
    """

    TOP = "top"
    JUNGLE = "jungle"
    MID = "mid"
    ADC = "adc"
    SUPPORT = "support"
    SUBSTITUTE = "substitute"
    COACH = "coach"
    STAFF = "staff"
    PARTNERS = "partners"

    def is_competitive(self) -> bool:
        """
        Determina si el rol corresponde a una posición competitiva de juego.

        Conforme a la regla invariante #6: Un usuario solo puede ocupar un rol
        competitivo en como máximo 1 equipo en toda la liga. Los roles no
        competitivos (coach, staff, partners) pueden ejercerse en múltiples clubes.
        """
        return self in (
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
            RosterRole.SUBSTITUTE,
        )

    def is_starter(self) -> bool:
        """
        Determina si el rol corresponde a una de las 5 posiciones titulares.

        Conforme al constraint team_memberships_captain_role_check, solo los
        5 titulares activos pueden ostentar la capitanía del equipo.
        """
        return self in (
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
        )


class RosterMovementAction(str, enum.Enum):
    """
    Tipos de acciones registradas en el historial de movimientos de plantilla.

    Alineado con roster_movement_action en RCL-Next:
    'joined', 'left', 'promoted_to_captain', 'demoted_from_captain', 'role_changed'
    """

    JOINED = "joined"
    LEFT = "left"
    PROMOTED_TO_CAPTAIN = "promoted_to_captain"
    DEMOTED_FROM_CAPTAIN = "demoted_from_captain"
    ROLE_CHANGED = "role_changed"


__all__ = [
    "AppRole",
    "Division",
    "MatchStatus",
    "RoleRequestStatus",
    "RosterMovementAction",
    "RosterRole",
]
