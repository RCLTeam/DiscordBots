"""
Punto de entrada ejecutable para LigaBot.

Permite arrancar el bot mediante:
    uv run python -m liga_bot
o:
    python -m liga_bot

Configura el sistema de logging, maneja señales del sistema operativo (SIGINT, SIGTERM)
para un apagado limpio y arranca el ciclo de vida del bot.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import signal
import sys

from liga_bot.bot import LigaBot
from liga_bot.config import Settings, get_settings

logger = logging.getLogger("liga_bot")


def setup_logging(settings: Settings) -> None:
    """Configura el logging del sistema según el nivel configurado."""
    numeric_level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reducir verbosidad de librerías de Discord si no estamos en DEBUG profundo
    if numeric_level > logging.DEBUG:
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.WARNING)


async def run_bot(settings: Settings | None = None) -> int:
    """
    Corrutina principal de ejecución del bot.

    Inicializa LigaBot, registra manejadores de señales y gestiona la sesión
    hasta su finalización o interrupción.
    """
    resolved_settings = settings or get_settings()
    setup_logging(resolved_settings)

    token = resolved_settings.discord_token
    if not token or not token.strip():
        logger.critical(
            "La variable DISCORD_TOKEN no está configurada. "
            "Por favor especifíquela en su archivo .env o en las variables de entorno."
        )
        return 1

    bot = LigaBot(settings=resolved_settings)
    loop = asyncio.get_running_loop()
    shutdown_initiated = False

    def handle_signal(sig: signal.Signals) -> None:
        nonlocal shutdown_initiated
        if shutdown_initiated:
            logger.warning("Señal %s recibida de nuevo. Forzando salida...", sig.name)
            return
        shutdown_initiated = True
        logger.info("Señal %s recibida. Iniciando parada ordenada...", sig.name)
        asyncio.create_task(bot.close())

    # Registrar señales SIGINT y SIGTERM en sistemas Unix
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, functools.partial(handle_signal, sig))
        except (NotImplementedError, RuntimeError):
            # No soportado en Windows ProactorEventLoop o hilos no principales
            pass

    try:
        logger.info("Iniciando conexión con Discord Gateway...")
        await bot.start(token)
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Ejecución interrumpida por el usuario o tarea cancelada.")
        return 0
    except Exception as exc:
        logger.critical("Error fatal durante la ejecución de LigaBot: %s", exc, exc_info=True)
        return 1
    finally:
        if not bot.is_closed():
            logger.info("Cerrando recursos pendientes del bot...")
            await bot.close()


def main() -> None:
    """Punto de entrada síncrono para 'python -m liga_bot'."""
    try:
        exit_code = asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Proceso interrumpido.")
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
