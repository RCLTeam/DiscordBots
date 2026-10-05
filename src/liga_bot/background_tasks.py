"""
Registro de tareas en segundo plano de LigaBot.

El bucle de eventos solo guarda referencias débiles a las tareas creadas con
``asyncio.create_task``; si nadie más las referencia, el recolector de basura puede
eliminarlas antes de que terminen. Este módulo mantiene una referencia fuerte a cada
tarea lanzada con :func:`spawn` hasta que finaliza, y permite a ``LigaBot.close()``
esperarlas o cancelarlas durante el apagado (:func:`drain`, :func:`cancel_all`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
    """Crea una tarea y la retiene en el registro hasta que termine."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def pending_tasks() -> frozenset[asyncio.Task[Any]]:
    """Devuelve las tareas registradas que aún no han terminado."""
    return frozenset(task for task in _tasks if not task.done())


def _tasks_of_running_loop() -> list[asyncio.Task[Any]]:
    """Tareas pendientes del bucle actual; descarta las de bucles ya cerrados."""
    loop = asyncio.get_running_loop()
    own: list[asyncio.Task[Any]] = []
    for task in list(_tasks):
        if task.done() or task.get_loop() is not loop:
            _tasks.discard(task)
            continue
        own.append(task)
    return own


async def drain(timeout: float) -> None:
    """Espera hasta ``timeout`` segundos a las tareas pendientes y cancela las restantes."""
    tasks = _tasks_of_running_loop()
    if not tasks:
        return
    logger.info("Esperando %d tarea(s) en segundo plano pendiente(s)...", len(tasks))
    _done, still_pending = await asyncio.wait(tasks, timeout=timeout)
    if still_pending:
        logger.warning(
            "Cancelando %d tarea(s) en segundo plano que no terminaron en %.1f s.",
            len(still_pending),
            timeout,
        )
        await _cancel(still_pending)


async def cancel_all() -> None:
    """Cancela todas las tareas pendientes del bucle actual y espera a que terminen."""
    tasks = _tasks_of_running_loop()
    if tasks:
        await _cancel(tasks)


async def _cancel(tasks: Any) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
