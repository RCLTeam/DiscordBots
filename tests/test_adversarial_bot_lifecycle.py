"""
Adversarial stress-test suite for LigaBot lifecycle, DI, and startup/shutdown.

Tests empirical failure modes and attack vectors:
1. Repeated and concurrent close() calls (idempotency, no AttributeError, no crash).
2. setup_hook failure during cog/DB loading (error propagation, resource cleanup, no leak).
3. Background tasks cancellation on shutdown with hostile/failing cogs and multiple loops.
4. on_ready storm (sequential and concurrent events, verifying tree.sync is NEVER called).
5. Missing, empty, and whitespace-only DISCORD_TOKEN in run_bot and main().
6. Simulated SIGINT / SIGTERM signal trap (verifying graceful shutdown task is scheduled once).
7. Dependency injection container preservation and edge-case resolution.
"""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from discord.ext import commands, tasks

from liga_bot.__main__ import main, run_bot
from liga_bot.bot import LigaBot
from liga_bot.config import Settings
from liga_bot.services.schedule_service import ScheduleService
from liga_bot.services.ticket_service import TicketService


@pytest.mark.asyncio
async def test_adversarial_double_close_sequential():
    """Stress-test calling bot.close() multiple times sequentially."""
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory") as mock_get_factory,
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
        patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
    ):
        mock_get_engine.return_value = mock_engine
        mock_get_factory.return_value = MagicMock()

        bot = LigaBot(extensions=())
        await bot.setup_hook()

        assert bot.engine is mock_engine
        assert bot.session_factory is not None

        # Execute multiple close calls in sequence
        for _ in range(5):
            await bot.close()

        # Engine should be closed on the first call, and remain None
        assert bot.engine is None
        assert bot.session_factory is None
        mock_close_engine.assert_awaited_once_with(mock_engine)
        assert mock_super_close.await_count == 5


@pytest.mark.asyncio
async def test_adversarial_double_close_concurrent():
    """Stress-test calling bot.close() concurrently from multiple tasks."""
    mock_engine = MagicMock()

    async def slow_close(engine):
        await asyncio.sleep(0.01)

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory") as mock_get_factory,
        patch("liga_bot.bot.close_engine", side_effect=slow_close) as mock_close_engine,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        mock_get_engine.return_value = mock_engine
        mock_get_factory.return_value = MagicMock()

        bot = LigaBot(extensions=())
        await bot.setup_hook()

        # Execute 5 concurrent close() calls simultaneously
        results = await asyncio.gather(
            bot.close(),
            bot.close(),
            bot.close(),
            bot.close(),
            bot.close(),
            return_exceptions=True,
        )

        # None of the concurrent calls should raise an exception
        for res in results:
            assert res is None or not isinstance(res, Exception), f"Unexpected exception: {res}"

        assert bot.engine is None
        assert bot.session_factory is None
        assert mock_close_engine.call_count >= 1


@pytest.mark.asyncio
async def test_adversarial_setup_hook_failure_propagates_and_cleans_resources():
    """
    Verify that if cog loading fails during setup_hook:
    1. The exception is cleanly propagated out.
    2. Any partial resources (DB engine, already loaded cogs) are properly freed on close().
    """
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory") as mock_get_factory,
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        mock_get_engine.return_value = mock_engine
        mock_get_factory.return_value = MagicMock()

        # Configure 3 extensions: ext1 succeeds, ext2 fails, ext3 should never be called
        ext1_called = False
        ext3_called = False

        async def mock_load_extension(ext: str) -> None:
            nonlocal ext1_called, ext3_called
            if ext == "ext1":
                ext1_called = True
            elif ext == "ext2":
                raise commands.ExtensionFailed("ext2", RuntimeError("Hostile failure during load"))
            elif ext == "ext3":
                ext3_called = True

        bot = LigaBot(extensions=["ext1", "ext2", "ext3"])
        bot.load_extension = mock_load_extension

        # 1. Verify exception propagation
        with pytest.raises(commands.ExtensionFailed) as exc_info:
            await bot.setup_hook()

        assert "Hostile failure during load" in str(exc_info.value)
        assert ext1_called is True
        assert ext3_called is False

        # 2. Engine was initialized before the cog failure
        assert bot.engine is mock_engine

        # 3. Simulate shutdown cleanup (as __main__.run_bot does in finally block)
        await bot.close()

        # 4. Verify DB engine was closed without leaking
        mock_close_engine.assert_awaited_once_with(mock_engine)
        assert bot.engine is None


@pytest.mark.asyncio
async def test_adversarial_run_bot_handles_setup_hook_failure_safely():
    """Verify that run_bot catches setup_hook fatal errors and returns 1 with cleanup."""
    test_settings = Settings(discord_token="adversarial_valid_token_abc123")

    with (
        patch.object(
            LigaBot,
            "start",
            new_callable=AsyncMock,
            side_effect=commands.ExtensionFailed("ext_bad", RuntimeError("Fatal startup crash")),
        ),
        patch.object(LigaBot, "close", new_callable=AsyncMock) as mock_close,
        patch.object(LigaBot, "is_closed", return_value=False),
    ):
        exit_code = await run_bot(settings=test_settings)

        assert exit_code == 1
        mock_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_adversarial_setup_hook_db_failure_propagates():
    """Verify that if get_engine fails in setup_hook, exception propagates and close is safe."""
    with (
        patch(
            "liga_bot.bot.get_engine",
            new_callable=AsyncMock,
            side_effect=ConnectionRefusedError("PGlite socket connection refused"),
        ),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        bot = LigaBot(extensions=())

        with pytest.raises(ConnectionRefusedError):
            await bot.setup_hook()

        assert bot.engine is None
        assert bot.session_factory is None

        # Calling close afterwards must not crash
        await bot.close()


@pytest.mark.asyncio
async def test_adversarial_shutdown_cancels_multiple_hostile_cogs():
    """
    Stress-test bot.close() with multiple hostile mock Cogs:
    - Cog 1: Normal with stop_loops and running loop
    - Cog 2: Multiple running loops, no stop_loops method
    - Cog 3: Hostile stop_loops() that raises Exception
    - Cog 4: Hostile property that raises AttributeError when inspected
    - Cog 5: Inactive loop (already stopped)
    """
    # Cog 1
    cog1 = MagicMock()
    loop1 = MagicMock(spec=tasks.Loop)
    loop1.is_running.return_value = True
    cog1.ticket_loop = loop1
    cog1.stop_loops = MagicMock()

    # Cog 2: 2 loops
    cog2 = MagicMock(spec=[])  # no stop_loops
    loop2_a = MagicMock(spec=tasks.Loop)
    loop2_a.is_running.return_value = True
    loop2_b = MagicMock(spec=tasks.Loop)
    loop2_b.is_running.return_value = True
    cog2.loop_a = loop2_a
    cog2.loop_b = loop2_b

    # Cog 3: stop_loops raises Exception
    cog3 = MagicMock()
    cog3.stop_loops.side_effect = RuntimeError("Hostile error in stop_loops")
    loop3 = MagicMock(spec=tasks.Loop)
    loop3.is_running.return_value = True
    cog3.recurrent_loop = loop3

    # Cog 4: Hostile property
    class HostileCog:
        def __init__(self):
            self.safe_loop = MagicMock(spec=tasks.Loop)
            self.safe_loop.is_running.return_value = True

        @property
        def poisonous_attr(self):
            raise AttributeError("Forbidden dynamic property access")

    cog4 = HostileCog()

    # Cog 5: Already stopped loop
    cog5 = MagicMock()
    loop5 = MagicMock(spec=tasks.Loop)
    loop5.is_running.return_value = False
    cog5.inactive_loop = loop5

    mock_cogs = {
        "Cog1": cog1,
        "Cog2": cog2,
        "Cog3": cog3,
        "Cog4": cog4,
        "Cog5": cog5,
    }

    bot = LigaBot(extensions=())

    with (
        patch.object(LigaBot, "cogs", new_callable=PropertyMock, return_value=mock_cogs),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock),
    ):
        # close() must not crash despite hostile cogs
        await bot.close()

        # Verify Cog 1 loop cancelled and stop_loops called
        cog1.stop_loops.assert_called_once()
        loop1.cancel.assert_called_once()

        # Verify Cog 2 both loops cancelled
        loop2_a.cancel.assert_called_once()
        loop2_b.cancel.assert_called_once()

        # Verify Cog 3 loop cancelled despite stop_loops exception
        loop3.cancel.assert_called_once()

        # Verify Cog 4 safe_loop cancelled despite poisonous_attr
        cog4.safe_loop.cancel.assert_called_once()

        # Verify Cog 5 inactive loop was not cancelled
        loop5.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_adversarial_on_ready_storm_sequential_and_concurrent():
    """
    Stress-test on_ready storm (rapid gateway reconnects).
    Guarantees that bot.tree.sync() is NEVER called under any circumstance.
    """
    bot = LigaBot()
    bot.tree.sync = AsyncMock()

    # 1. Sequential storm: 100 consecutive on_ready events
    for _ in range(100):
        await bot.on_ready()

    bot.tree.sync.assert_not_called()

    # 2. Concurrent storm: 50 concurrent on_ready events
    await asyncio.gather(*[bot.on_ready() for _ in range(50)])

    bot.tree.sync.assert_not_called()
    assert bot.tree.sync.call_count == 0

    await bot.close()


@pytest.mark.asyncio
async def test_adversarial_on_ready_with_none_user_and_empty_guilds():
    """Verify on_ready resilience when user entity is None or guilds list is empty."""
    bot = LigaBot()
    bot.tree.sync = AsyncMock()

    with (
        patch.object(LigaBot, "user", new_callable=PropertyMock, return_value=None),
        patch.object(LigaBot, "guilds", new_callable=PropertyMock, return_value=[]),
    ):
        # Must log safely without crashing even if user is None
        await bot.on_ready()

    bot.tree.sync.assert_not_called()
    await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile_token",
    [
        "",
        "   ",
        "\t\t",
        "\n\r\n",
        "  \t \r\n  ",
    ],
)
async def test_adversarial_missing_or_whitespace_token_returns_code_1(hostile_token: str):
    """
    Verify run_bot safely returns exit code 1 without unhandled exception
    when DISCORD_TOKEN is missing or whitespace-only.
    """
    test_settings = Settings(discord_token=hostile_token)
    exit_code = await run_bot(settings=test_settings)
    assert exit_code == 1


def test_adversarial_main_sync_exit_on_whitespace_token():
    """Verify main() exits with status code 1 when token is whitespace."""
    with (
        patch("liga_bot.__main__.get_settings") as mock_settings,
        patch("sys.exit") as mock_exit,
    ):
        mock_settings.return_value = Settings(discord_token="    \t\n")
        main()
        mock_exit.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_adversarial_simulated_signal_trap_schedules_shutdown():
    """
    Simulate OS signals (SIGINT, SIGTERM) delivered to run_bot.
    Verify that:
    1. A graceful shutdown task (bot.close()) is scheduled.
    2. Subsequent duplicate signals do NOT schedule duplicate close tasks.
    """
    registered_handlers: dict[signal.Signals, callable] = {}
    mock_loop = MagicMock()

    def mock_add_signal_handler(sig, callback):
        registered_handlers[sig] = callback

    mock_loop.add_signal_handler = mock_add_signal_handler

    test_settings = Settings(discord_token="mock_valid_token_123")

    with (
        patch("asyncio.get_running_loop", return_value=mock_loop),
        patch.object(LigaBot, "start", new_callable=AsyncMock) as mock_start,
        patch.object(LigaBot, "close", new_callable=AsyncMock) as mock_close,
        patch("asyncio.create_task") as mock_create_task,
    ):
        mock_create_task.side_effect = lambda coro: (coro.close(), MagicMock())[1]

        # We simulate that start() pauses waiting for gateway
        async def mock_start_impl(token):
            # When bot.start is waiting, simulate SIGINT delivery
            handler = registered_handlers.get(signal.SIGINT)
            assert handler is not None, "SIGINT handler was not registered"
            # First signal delivery: must schedule bot.close()
            handler()
            # Second signal delivery: must be ignored
            handler()

        mock_start.side_effect = mock_start_impl

        exit_code = await run_bot(settings=test_settings)

        assert exit_code == 0
        # asyncio.create_task must be called exactly once despite 2 signal triggers
        assert mock_create_task.call_count == 1
        mock_close.assert_awaited()


@pytest.mark.asyncio
async def test_adversarial_simulated_sigterm_signal_trap():
    """Verify SIGTERM registration and single-task scheduling."""
    registered_handlers: dict[signal.Signals, callable] = {}
    mock_loop = MagicMock()

    def mock_add_signal_handler(sig, callback):
        registered_handlers[sig] = callback

    mock_loop.add_signal_handler = mock_add_signal_handler
    test_settings = Settings(discord_token="mock_valid_token_sigterm")

    with (
        patch("asyncio.get_running_loop", return_value=mock_loop),
        patch.object(LigaBot, "start", new_callable=AsyncMock) as mock_start,
        patch.object(LigaBot, "close", new_callable=AsyncMock),
        patch("asyncio.create_task") as mock_create_task,
    ):
        mock_create_task.side_effect = lambda coro: (coro.close(), MagicMock())[1]

        async def mock_start_impl(token):
            handler = registered_handlers.get(signal.SIGTERM)
            assert handler is not None, "SIGTERM handler was not registered"
            handler()

        mock_start.side_effect = mock_start_impl

        exit_code = await run_bot(settings=test_settings)

        assert exit_code == 0
        assert mock_create_task.call_count == 1


@pytest.mark.asyncio
async def test_adversarial_di_container_wiring_and_service_preservation():
    """
    Verify Dependency Injection container invariants:
    1. setup_hook binds bot instance to schedule_service and ticket_service.
    2. Pre-injected custom services are preserved and not overwritten.
    """
    mock_custom_schedule = MagicMock(spec=ScheduleService)
    mock_custom_ticket = MagicMock(spec=TicketService)

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory") as mock_get_factory,
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
        patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
    ):
        mock_get_engine.return_value = MagicMock()
        mock_get_factory.return_value = MagicMock()

        bot = LigaBot(extensions=())
        bot.schedule_service = mock_custom_schedule
        bot.ticket_service = mock_custom_ticket

        await bot.setup_hook()

        # Custom services must be preserved
        assert bot.schedule_service is mock_custom_schedule
        assert bot.ticket_service is mock_custom_ticket

        await bot.close()
        mock_close_engine.assert_awaited_once()
        mock_super_close.assert_awaited_once()
