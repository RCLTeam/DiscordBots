"""Domain enums for LigaBot entities."""

from __future__ import annotations

import enum
import logging

logger = logging.getLogger(__name__)


class Division(str, enum.Enum):
    """División competitiva de la liga."""

    PREMIER = "PREMIER"
    ASCEND = "ASCEND"

    @classmethod
    def from_name(cls, name: object) -> Division | None:
        """Convierte un nombre de división en el enum sin distinguir mayúsculas ni espacios.

        La web guarda el nombre con su propio formato en
        ``seasons_divisions.division_name`` (por ejemplo ``Ascend``), mientras que el
        enum usa mayúsculas. Devuelve ``None`` si el nombre no corresponde a ninguna
        división conocida, para que quien llama decida cómo tratarlo.
        """
        if isinstance(name, cls):
            return name
        if name is None:
            return None
        try:
            return cls(str(name).strip().upper())
        except ValueError:
            return None


def resolve_entity_division(
    season_division: object | None,
    override: Division | None,
    owner: object,
) -> Division:
    """Resuelve la división de un equipo o partido a partir de su ``season_division``.

    Orden: nombre de ``season_division`` normalizado con ``Division.from_name``, valor
    asignado en memoria (``override``) y, por último, ``PREMIER``. Si la fila de
    ``seasons_divisions`` tiene un nombre que no corresponde a ninguna división, deja un
    aviso en el log en lugar de caer a ``PREMIER`` en silencio.
    """
    raw_name = getattr(season_division, "division_name", None) if season_division else None
    if raw_name:
        division = Division.from_name(raw_name)
        if division is not None:
            return division
        fallback = override or Division.PREMIER
        logger.warning(
            "División desconocida %r en seasons_divisions (id=%s) para %s id=%s; se usa %s.",
            raw_name,
            getattr(season_division, "id", None),
            type(owner).__name__,
            # Lee el id ya cargado sin disparar una carga perezosa en la sesión asíncrona.
            vars(owner).get("id"),
            fallback.value,
        )
        return fallback
    return override or Division.PREMIER


def ensure_division_assignable(
    season_division: object | None,
    value: Division | str | None,
) -> Division | None:
    """Valida el valor que se asigna a ``Team.division`` o ``Match.division``.

    ``seasons_divisions`` es una tabla compartida que gestiona la web: el bot nunca
    modifica su ``division_name``. Si la entidad ya tiene cargada su ``season_division``
    y el valor pide otra división, lanza ``ValueError``: el cambio de división se hace
    reasignando la fila de ``seasons_divisions`` (``TeamRepository.update``).
    """
    if value is None:
        return None
    division = Division.from_name(value)
    if division is None:
        raise ValueError(f"{value!r} no es una división válida")
    raw_name = getattr(season_division, "division_name", None) if season_division else None
    current = Division.from_name(raw_name) if raw_name else None
    if current is not None and current != division:
        raise ValueError(
            f"La entidad pertenece a la división {current.value}; para pasarla a "
            f"{division.value} hay que reasignar su season_division."
        )
    return division


class MatchStatus(str, enum.Enum):
    """Ciclo de vida y estado de un partido alineado con match_status en PostgreSQL."""

    SCHEDULED = "scheduled"
    LIVE = "live"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FORFEIT = "forfeit"


class RoleRequestStatus(str, enum.Enum):
    """Ciclo de vida y estado operativo de una solicitud de rol de equipo."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"


class AppRole(str, enum.Enum):
    """Rol de aplicación en la plataforma RCL (alineado con app_role en RCL-Next)."""

    VIEWER = "viewer"
    ADMIN = "admin"
    OWNER = "owner"


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
