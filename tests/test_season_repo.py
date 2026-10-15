"""Resolución de la temporada vigente (``repositories/season_repo``).

``seasons`` no tiene ninguna columna que marque la temporada activa, así que la vigencia
sale de ``starts_on`` y ``ends_on``. Estas pruebas fijan el criterio acordado en el
issue #63 y los tres escenarios que importan: ninguna vigente, una y varias.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.roster import Season
from liga_bot.repositories.season_repo import current_season_name

HOY = date.today()


async def _limpiar_temporadas(session: AsyncSession) -> None:
    """La migración siembra la temporada 'default'; estos tests parten de cero."""
    await session.execute(text("DELETE FROM seasons_divisions"))
    await session.execute(text("DELETE FROM seasons"))
    await session.flush()


@pytest.mark.asyncio
async def test_sin_temporadas_devuelve_none(session: AsyncSession):
    await _limpiar_temporadas(session)

    assert await current_season_name(session) is None


@pytest.mark.asyncio
async def test_una_temporada_vigente(session: AsyncSession):
    await _limpiar_temporadas(session)
    session.add(Season(name="2026", starts_on=HOY - timedelta(days=5)))
    await session.flush()

    assert await current_season_name(session) == "2026"


@pytest.mark.asyncio
async def test_varias_vigentes_manda_la_de_inicio_mas_reciente(session: AsyncSession):
    """Los nombres van a contrapelo del orden alfabético, que es el último desempate.

    Con nombres como "nueva"/"vieja" el test pasaría aunque se quitara la regla de
    starts_on, porque el desempate final por nombre ya devuelve el resultado esperado.
    """
    await _limpiar_temporadas(session)
    session.add(Season(name="a-vieja", starts_on=HOY - timedelta(days=200)))
    session.add(Season(name="z-nueva", starts_on=HOY - timedelta(days=2)))
    await session.flush()

    assert await current_season_name(session) == "z-nueva"


@pytest.mark.asyncio
async def test_ninguna_vigente_manda_la_creada_mas_tarde(session: AsyncSession):
    """Todas cerradas o por empezar: decide created_at, no las fechas.

    Las marcas de tiempo van explícitas: ``now()`` es constante dentro de una
    transacción, así que dos filas insertadas en el mismo test empatarían.
    """
    await _limpiar_temporadas(session)
    ahora = datetime.now(timezone.utc)
    session.add(
        Season(
            name="cerrada",
            starts_on=HOY - timedelta(days=400),
            ends_on=HOY - timedelta(days=200),
            created_at=ahora - timedelta(days=10),
        )
    )
    session.add(
        Season(
            name="futura",
            starts_on=HOY + timedelta(days=30),
            created_at=ahora,
        )
    )
    await session.flush()

    assert await current_season_name(session) == "futura"


@pytest.mark.asyncio
async def test_temporada_sin_fecha_de_inicio_no_se_considera_vigente(session: AsyncSession):
    """starts_on nulo deja la comparación en NULL: no puede colarse por delante."""
    await _limpiar_temporadas(session)
    session.add(Season(name="sin fechas"))
    await session.flush()
    session.add(Season(name="vigente", starts_on=HOY - timedelta(days=1)))
    await session.flush()

    assert await current_season_name(session) == "vigente"
