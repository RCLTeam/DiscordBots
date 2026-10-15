"""Resolución de la temporada vigente sobre la tabla compartida ``seasons``.

``seasons`` no tiene ninguna columna que marque la temporada activa: la web la
gestiona con ``name``, ``starts_on`` y ``ends_on`` (ver ``models/roster.py``), así que
la vigencia se deduce de las fechas.

Criterio aplicado (acordado para el issue #63):

1. Vigente es la temporada con ``starts_on <= hoy`` y ``ends_on`` nulo o ``>= hoy``.
2. Si hay varias vigentes, manda la de ``starts_on`` más reciente.
3. Si no hay ninguna vigente, manda la de ``created_at`` más reciente.

Vive en un único módulo a propósito: el repositorio de equipos y el de plantillas
deben coincidir en qué temporada es la actual.
"""

from __future__ import annotations

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.roster import Season

__all__ = ["current_season_name", "current_season_name_select"]


def current_season_name_select() -> Select[tuple[str]]:
    """``SELECT`` que devuelve el nombre de la temporada vigente, o ninguna fila."""
    # coalesce: con starts_on nulo la comparación da NULL, y en PostgreSQL un
    # ORDER BY ... DESC coloca los NULL por delante de los TRUE.
    vigente = func.coalesce(
        (Season.starts_on <= func.current_date())
        & ((Season.ends_on.is_(None)) | (Season.ends_on >= func.current_date())),
        False,
    )
    return (
        select(Season.name)
        .order_by(
            vigente.desc(),
            # Solo desempata entre vigentes: con ninguna vigente queda nulo en todas
            # las filas y decide created_at.
            case((vigente, Season.starts_on), else_=None).desc().nullslast(),
            Season.created_at.desc(),
            Season.name.asc(),
        )
        .limit(1)
    )


async def current_season_name(session: AsyncSession) -> str | None:
    """Nombre de la temporada vigente, o ``None`` si la tabla está vacía."""
    result = await session.execute(current_season_name_select())
    return result.scalars().first()
