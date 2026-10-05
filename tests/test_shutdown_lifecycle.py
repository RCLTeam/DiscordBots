"""Pruebas del ciclo de vida del apagado: tareas en segundo plano, orden de cierre y señales."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord.ext import commands

from liga_bot import background_tasks
from liga_bot.__main__ import run_bot
from liga_bot.bot import LigaBot
from liga_bot.config import Settings
from liga_bot.services.websocket_bridge_service import WebsocketBridgeService
from liga_bot.ui.roles import ConfirmarRolButton, TicketView

# ---------------------------------------------------------------------------
# Tareas en segundo plano con referencia fuerte
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_retiene_la_tarea_hasta_que_termina() -> None:
    liberar = asyncio.Event()

    async def trabajo() -> None:
        await liberar.wait()

    task = background_tasks.spawn(trabajo())
    assert task in background_tasks.pending_tasks()

    liberar.set()
    await task
    assert task not in background_tasks.pending_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory",
    [lambda: ConfirmarRolButton(user_id=1), lambda: TicketView(user_id=1)],
    ids=["ConfirmarRolButton", "TicketView"],
)
async def test_schedule_deletion_mantiene_la_referencia_hasta_terminar(
    factory: Callable[[], Any],
) -> None:
    component = factory()
    channel = MagicMock()
    channel.delete = AsyncMock()

    task = component._schedule_deletion(channel, delay=0.01)

    assert task is not None
    assert task in background_tasks.pending_tasks()
    await task
    channel.delete.assert_awaited_once()
    assert task not in background_tasks.pending_tasks()


@pytest.mark.asyncio
async def test_drain_espera_las_tareas_que_terminan_a_tiempo() -> None:
    terminado = asyncio.Event()

    async def trabajo() -> None:
        await asyncio.sleep(0.01)
        terminado.set()

    background_tasks.spawn(trabajo())
    await background_tasks.drain(timeout=1.0)

    assert terminado.is_set()
    assert not background_tasks.pending_tasks()


@pytest.mark.asyncio
async def test_drain_cancela_las_tareas_que_exceden_el_plazo() -> None:
    task = background_tasks.spawn(asyncio.sleep(10))

    await background_tasks.drain(timeout=0.01)

    assert task.cancelled()
    assert not background_tasks.pending_tasks()


@pytest.mark.asyncio
async def test_drain_sin_tareas_no_falla() -> None:
    await background_tasks.drain(timeout=0.01)


# ---------------------------------------------------------------------------
# Orden de cierre de LigaBot.close()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_cierra_discord_antes_que_la_base_de_datos() -> None:
    orden: list[str] = []
    settings = Settings(bridge_enabled=True)
    mock_engine = MagicMock()

    async def bridge_stop() -> None:
        orden.append("bridge")

    async def borrado_pendiente() -> None:
        await asyncio.sleep(0.01)
        orden.append("tarea")

    async def discord_close() -> None:
        orden.append("discord")

    async def engine_close(_engine: Any) -> None:
        orden.append("bd")

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock, return_value=mock_engine),
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", side_effect=engine_close),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(WebsocketBridgeService, "stop", side_effect=bridge_stop),
        patch.object(commands.Bot, "close", side_effect=discord_close),
    ):
        bot = LigaBot(settings=settings, extensions=())
        await bot.setup_hook()
        background_tasks.spawn(borrado_pendiente())

        await bot.close()

    assert orden == ["bridge", "tarea", "discord", "bd"]
    assert bot.engine is None


@pytest.mark.asyncio
async def test_close_cancela_las_tareas_creadas_durante_el_cierre_de_discord() -> None:
    tardia: list[asyncio.Task[Any]] = []

    async def discord_close() -> None:
        tardia.append(background_tasks.spawn(asyncio.sleep(10)))

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock),
        patch.object(commands.Bot, "close", side_effect=discord_close),
    ):
        bot = LigaBot(settings=Settings(bridge_enabled=False), extensions=())
        await bot.setup_hook()
        await bot.close()

    assert tardia and tardia[0].cancelled()


# ---------------------------------------------------------------------------
# Señales del sistema operativo en run_bot()
# ---------------------------------------------------------------------------


def _capturar_manejadores() -> tuple[MagicMock, dict[signal.Signals, Callable[[], None]]]:
    handlers: dict[signal.Signals, Callable[[], None]] = {}
    mock_loop = MagicMock()
    mock_loop.add_signal_handler = lambda sig, cb: handlers.__setitem__(sig, cb)
    return mock_loop, handlers


async def _run_bot_con_plazo(settings: Settings, plazo: float = 2.0) -> int:
    """Ejecuta run_bot() y falla si no termina en el plazo (sin esperar a la cancelación)."""
    task = asyncio.ensure_future(run_bot(settings=settings))
    done, _pending = await asyncio.wait({task}, timeout=plazo)
    if not done:
        task.cancel()
        pytest.fail("run_bot() no terminó tras la segunda señal")
    return task.result()


@pytest.mark.asyncio
async def test_run_bot_espera_a_que_termine_el_cierre_iniciado_por_la_senal() -> None:
    mock_loop, handlers = _capturar_manejadores()
    cierre_completo = asyncio.Event()

    async def close_lento() -> None:
        await asyncio.sleep(0.05)
        cierre_completo.set()

    async def start(_token: str) -> None:
        handlers[signal.SIGTERM]()
        # Discord ya se ha cerrado: start() retorna antes de que close() termine.

    with (
        patch("asyncio.get_running_loop", return_value=mock_loop),
        patch.object(LigaBot, "start", side_effect=start),
        patch.object(LigaBot, "close", side_effect=close_lento) as mock_close,
    ):
        exit_code = await run_bot(settings=Settings(discord_token="token_de_prueba"))

    assert exit_code == 0
    assert cierre_completo.is_set()
    mock_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_segunda_senal_durante_el_apagado_fuerza_la_salida() -> None:
    real_loop = asyncio.get_running_loop()
    mock_loop, handlers = _capturar_manejadores()
    bloqueo = asyncio.Event()

    async def close_bloqueado() -> None:
        await bloqueo.wait()

    async def start(_token: str) -> None:
        handlers[signal.SIGTERM]()
        real_loop.call_later(0.02, handlers[signal.SIGINT])
        await asyncio.Event().wait()

    with (
        patch("asyncio.get_running_loop", return_value=mock_loop),
        patch.object(LigaBot, "start", side_effect=start),
        patch.object(LigaBot, "close", side_effect=close_bloqueado),
    ):
        exit_code = await _run_bot_con_plazo(Settings(discord_token="token_de_prueba"))

    assert exit_code != 0


@pytest.mark.asyncio
async def test_segunda_senal_mientras_se_espera_el_cierre_fuerza_la_salida() -> None:
    real_loop = asyncio.get_running_loop()
    mock_loop, handlers = _capturar_manejadores()

    async def close_bloqueado() -> None:
        await asyncio.Event().wait()

    async def start(_token: str) -> None:
        handlers[signal.SIGINT]()
        real_loop.call_later(0.02, handlers[signal.SIGINT])

    with (
        patch("asyncio.get_running_loop", return_value=mock_loop),
        patch.object(LigaBot, "start", side_effect=start),
        patch.object(LigaBot, "close", side_effect=close_bloqueado),
    ):
        exit_code = await _run_bot_con_plazo(Settings(discord_token="token_de_prueba"))

    assert exit_code != 0
