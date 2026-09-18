"""
Pruebas unitarias para el ciclo de vida de LigaBot y punto de entrada __main__.py.

Verifica:
- Configuración de intents privilegiados (members, message_content).
- Inyección de dependencias y carga asíncrona de Cogs en setup_hook().
- Liberación ordenada de recursos, cancelación de bucles y cierre de DB en close().
- Desacoplamiento estricto de sincronización de comandos en on_ready() contra rate limits.
- Manejo de señales y validación de tokens en __main__.py.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from discord.ext import commands, tasks

from liga_bot.__main__ import main, run_bot, setup_logging
from liga_bot.bot import DEFAULT_EXTENSIONS, LigaBot
from liga_bot.config import Settings
from liga_bot.services.schedule_service import ScheduleService
from liga_bot.services.ticket_service import TicketService


@pytest.mark.asyncio
async def test_bot_initialization_defaults():
    """Verifica que LigaBot inicializa los intents privilegiados y dependencias por defecto."""
    bot = LigaBot()

    assert bot.intents.members is True
    assert bot.intents.message_content is True
    assert bot.command_prefix == "!"
    assert bot.engine is None
    assert bot.session_factory is None
    assert bot.schedule_service is None
    assert bot.ticket_service is None
    assert bot.extensions_to_load == DEFAULT_EXTENSIONS

    await bot.close()


@pytest.mark.asyncio
async def test_bot_custom_extensions():
    """Verifica que se pueden personalizar las extensiones cargadas en LigaBot."""
    custom_exts = ("liga_bot.cogs.admin",)
    bot = LigaBot(extensions=custom_exts)

    assert bot.extensions_to_load == custom_exts

    await bot.close()


@pytest.mark.asyncio
async def test_bot_setup_hook_initialization():
    """Verifica que setup_hook inicializa DB, servicios de dominio y dependencias."""
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    mock_session_factory = MagicMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory") as mock_get_factory,
    ):
        mock_get_engine.return_value = mock_engine
        mock_get_factory.return_value = mock_session_factory

        bot = LigaBot(extensions=())
        bot.load_extension = AsyncMock()

        await bot.setup_hook()

        assert bot.engine is mock_engine
        assert bot.session_factory is mock_session_factory
        assert isinstance(bot.schedule_service, ScheduleService)
        assert isinstance(bot.ticket_service, TicketService)
        assert bot.schedule_service.bot is bot
        assert bot.ticket_service.bot is bot

        await bot.close()


@pytest.mark.asyncio
async def test_bot_setup_hook_loads_all_default_extensions():
    """Verifica que setup_hook itera y carga cada extensión por defecto."""
    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
    ):
        bot = LigaBot()
        bot.load_extension = AsyncMock()

        await bot.setup_hook()

        assert bot.load_extension.await_count == len(DEFAULT_EXTENSIONS)
        loaded = [call.args[0] for call in bot.load_extension.await_args_list]
        assert set(loaded) == set(DEFAULT_EXTENSIONS)

        await bot.close()


@pytest.mark.asyncio
async def test_bot_setup_hook_extension_failure_raises():
    """Verifica que un fallo al cargar una extensión propaga la excepción e interrumpe el setup."""
    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
    ):
        bot = LigaBot()
        bot.load_extension = AsyncMock(
            side_effect=commands.ExtensionFailed("admin", Exception("Simulated error"))
        )

        with pytest.raises(commands.ExtensionFailed):
            await bot.setup_hook()

        await bot.close()


@pytest.mark.asyncio
async def test_bot_close_cancels_background_loops_and_disposes_engine():
    """Verifica que close() cancela bucles en cogs y cierra el motor de BD."""
    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
    ):
        bot = LigaBot(extensions=())
        bot.load_extension = AsyncMock()
        await bot.setup_hook()

        # Crear un cog simulado con un tasks.Loop activo y stop_loops
        mock_cog = MagicMock()
        mock_loop = MagicMock(spec=tasks.Loop)
        mock_loop.is_running.return_value = True
        mock_cog.ticket_loop = mock_loop
        mock_cog.stop_loops = MagicMock()

        with (
            patch.object(
                LigaBot, "cogs", new_callable=PropertyMock, return_value={"MockCog": mock_cog}
            ),
            patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
        ):
            await bot.close()

            mock_cog.stop_loops.assert_called_once()
            mock_loop.cancel.assert_called_once()
            mock_close_engine.assert_awaited_once()
            assert bot.engine is None
            assert bot.session_factory is None
            mock_super_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_close_is_idempotent():
    """Verifica que llamar a close() múltiples veces es seguro e idempotente."""
    bot = LigaBot(extensions=())
    with patch.object(commands.Bot, "close", new_callable=AsyncMock):
        await bot.close()
        await bot.close()

    assert bot.engine is None
    assert bot.session_factory is None


@pytest.mark.asyncio
async def test_bot_on_ready_does_not_sync_tree():
    """
    Verifica que on_ready() no ejecuta tree.sync() para prevenir
    bloqueos por límite de tasa (HTTP 429) en reconexiones del Gateway.
    """
    bot = LigaBot()
    bot.tree.sync = AsyncMock()

    await bot.on_ready()

    bot.tree.sync.assert_not_called()
    await bot.close()


@pytest.mark.asyncio
async def test_main_missing_token_returns_code_1():
    """Verifica que run_bot devuelve código 1 si DISCORD_TOKEN está vacío."""
    test_settings = Settings(discord_token="")
    exit_code = await run_bot(settings=test_settings)

    assert exit_code == 1


@pytest.mark.asyncio
async def test_main_successful_start_and_shutdown():
    """Verifica el flujo normal de inicio y parada de run_bot."""
    test_settings = Settings(discord_token="fake_test_token_12345")

    with (
        patch.object(LigaBot, "start", new_callable=AsyncMock) as mock_start,
        patch.object(LigaBot, "close", new_callable=AsyncMock) as mock_close,
    ):
        exit_code = await run_bot(settings=test_settings)

        assert exit_code == 0
        mock_start.assert_awaited_once_with("fake_test_token_12345")
        mock_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_handles_keyboard_interrupt():
    """Verifica que run_bot captura KeyboardInterrupt limpiamente y retorna 0."""
    test_settings = Settings(discord_token="fake_test_token_12345")

    with (
        patch.object(LigaBot, "start", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        patch.object(LigaBot, "close", new_callable=AsyncMock) as mock_close,
    ):
        exit_code = await run_bot(settings=test_settings)

        assert exit_code == 0
        mock_close.assert_awaited_once()


def test_main_sync_entrypoint():
    """Verifica que la función sincrónica main() ejecuta el loop y sale con el código devuelto."""
    with (
        patch("liga_bot.__main__.run_bot", new_callable=AsyncMock, return_value=0) as mock_run,
        patch("sys.exit") as mock_exit,
    ):
        main()
        mock_run.assert_awaited_once()
        mock_exit.assert_called_once_with(0)


def test_setup_logging_levels():
    """Verifica la configuración de niveles de logging."""
    settings_info = Settings(log_level="INFO")
    setup_logging(settings_info)

    settings_debug = Settings(log_level="DEBUG")
    setup_logging(settings_debug)
