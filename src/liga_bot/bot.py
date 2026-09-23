"""
Módulo del cliente principal de Discord para LigaBot.

Define la subclase LigaBot(commands.Bot) con:
- Intents privilegiados obligatorios (members, message_content).
- Inyección de dependencias (settings, engine, session_factory, schedule_service, ticket_service).
- Carga asíncrona de extensiones en setup_hook().
- Parada ordenada y liberación de recursos en close().
- Desacoplamiento de sincronización de comandos en on_ready() para prevenir rate limits.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

import discord
from discord.ext import commands, tasks
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings, get_settings
from liga_bot.database import close_engine, get_engine, get_session_factory
from liga_bot.services.role_service import RoleService
from liga_bot.services.roster_sync_service import RosterSyncService
from liga_bot.services.schedule_service import ScheduleService
from liga_bot.services.suggestion_service import SuggestionService
from liga_bot.services.ticket_service import TicketService
from liga_bot.services.websocket_bridge_service import WebsocketBridgeService

logger = logging.getLogger("liga_bot.bot")

DEFAULT_EXTENSIONS: Final[tuple[str, ...]] = (
    "liga_bot.cogs.admin",
    "liga_bot.cogs.roles",
    "liga_bot.cogs.roster",
    "liga_bot.cogs.schedule",
    "liga_bot.cogs.teams",
    "liga_bot.cogs.tickets",
)


class LigaBot(commands.Bot):
    """
    Cliente Discord modular desacoplado para LigaBot.

    Centraliza el ciclo de vida del bot, la inyección de dependencias
    y la carga asíncrona de extensiones.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        command_prefix: str = "!",
        intents: discord.Intents | None = None,
        extensions: Sequence[str] | None = None,
        *,
        suggestion_service: SuggestionService | None = None,
        websocket_bridge_service: WebsocketBridgeService | None = None,
        **kwargs: Any,
    ) -> None:
        resolved_settings = settings or get_settings()

        if intents is None:
            intents = discord.Intents.default()
        # Intents privilegiados obligatorios para la operativa de la liga
        intents.members = True
        intents.message_content = True

        super().__init__(
            command_prefix=command_prefix,
            intents=intents,
            **kwargs,
        )

        self.settings: Settings = resolved_settings
        self.extensions_to_load: tuple[str, ...] = (
            tuple(extensions) if extensions is not None else DEFAULT_EXTENSIONS
        )

        # Inyección de dependencias (inicializadas en setup_hook)
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.schedule_service: ScheduleService | None = None
        self.ticket_service: TicketService | None = None
        self.role_service: RoleService | None = None
        self.roster_sync_service: RosterSyncService | None = None
        self.suggestion_service: SuggestionService | None = suggestion_service
        self.websocket_bridge_service: WebsocketBridgeService | None = websocket_bridge_service

    async def setup_hook(self) -> None:
        """
        Hook asíncrono de inicialización previo al inicio del bot.

        1. Inicializa el motor de base de datos dual (PostgreSQL o PGlite).
        2. Inicializa la factoría de sesiones asíncronas.
        3. Instancia los servicios de dominio (ScheduleService, TicketService,
           RoleService, RosterSyncService, SuggestionService, WebsocketBridgeService).
        4. Carga de forma asíncrona todas las extensiones / Cogs configuradas.
        5. Inicia el servidor WebSocket Bridge si está habilitado por configuración.
        """
        logger.info("Ejecutando setup_hook de LigaBot...")

        # 1. Base de datos
        if self.engine is None:
            logger.info("Inicializando motor de base de datos (%s)...", self.settings.database_url)
            self.engine = await get_engine(self.settings)

        if self.session_factory is None:
            self.session_factory = get_session_factory(self.engine)

        # 2. Servicios de dominio
        if self.schedule_service is None:
            self.schedule_service = ScheduleService(
                session_factory=self.session_factory,
                settings=self.settings,
                bot=self,
            )

        if self.ticket_service is None:
            self.ticket_service = TicketService(
                session_factory=self.session_factory,
                settings=self.settings,
                bot=self,
            )

        if self.role_service is None:
            self.role_service = RoleService(
                session_factory=self.session_factory,
                settings=self.settings,
                bot=self,
            )

        if self.roster_sync_service is None:
            self.roster_sync_service = RosterSyncService(
                session_factory=self.session_factory,
                settings=self.settings,
                bot=self,
            )

        if self.suggestion_service is None:
            self.suggestion_service = SuggestionService(
                bot=self,
                settings=self.settings,
            )

        if self.websocket_bridge_service is None:
            self.websocket_bridge_service = WebsocketBridgeService(
                bot=self,
                settings=self.settings,
                suggestion_service=self.suggestion_service,
            )

        # 3. Carga de Cogs / Extensiones
        for extension in self.extensions_to_load:
            try:
                await self.load_extension(extension)
                logger.info("Extensión cargada exitosamente: %s", extension)
            except Exception as exc:
                logger.error("Error cargando extensión '%s': %s", extension, exc, exc_info=True)
                raise

        # 4. Servidor WebSocket Bridge
        if self.settings.bridge_enabled and self.websocket_bridge_service is not None:
            logger.info(
                "Iniciando WebSocket Bridge en ws://%s:%s/ws/bridge...",
                self.settings.bridge_host,
                self.settings.bridge_port,
            )
            await self.websocket_bridge_service.start()
        else:
            logger.info("WebSocket Bridge deshabilitado por configuración (bridge_enabled=False).")

        logger.info("setup_hook completado con éxito.")

    async def close(self) -> None:
        """
        Cierre ordenado y liberación segura de recursos de LigaBot.

        1. Detiene y desconecta de forma atómica el servidor WebSocket Bridge si está activo.
        2. Cancela cualquier tarea en segundo plano (tasks.Loop) en los Cogs.
        3. Cierra y libera el motor de base de datos y conexiones activas.
        4. Invoca super().close() para cerrar la sesión HTTP y websocket de Discord.
        """
        logger.info("Iniciando secuencia de cierre ordenado de LigaBot...")

        # 1. Detener WebSocket Bridge si está activo
        if self.websocket_bridge_service is not None:
            logger.info("Deteniendo WebSocket Bridge...")
            bridge = self.websocket_bridge_service
            self.websocket_bridge_service = None
            try:
                await bridge.stop()
            except Exception as exc:
                logger.warning("Error deteniendo WebSocket Bridge: %s", exc)

        # 2. Cancelar bucles en segundo plano en los Cogs
        for cog_name, cog in list(self.cogs.items()):
            if hasattr(cog, "stop_loops") and callable(cog.stop_loops):
                try:
                    cog.stop_loops()
                except Exception as exc:
                    logger.warning("Error llamando stop_loops() en Cog '%s': %s", cog_name, exc)

            for attr_name in dir(cog):
                try:
                    attr = getattr(cog, attr_name, None)
                    if isinstance(attr, tasks.Loop) and attr.is_running():
                        logger.info("Cancelando bucle '%s' en Cog '%s'...", attr_name, cog_name)
                        attr.cancel()
                except Exception as exc:
                    logger.warning(
                        "Error inspeccionando bucles en %s.%s: %s", cog_name, attr_name, exc
                    )

        # 3. Cerrar motor de base de datos
        if self.engine is not None:
            logger.info("Cerrando motor de base de datos...")
            await close_engine(self.engine)
            self.engine = None
            self.session_factory = None

        # 4. Cerrar cliente de Discord
        logger.info("Cerrando conexión de Discord...")
        await super().close()
        logger.info("LigaBot cerrado completamente.")

    async def on_ready(self) -> None:
        """
        Evento invocado cuando el bot se conecta y la caché está lista.

        Nota arquitectónica: La sincronización de comandos (tree.sync()) está
        deliberadamente desacoplada de on_ready() para prevenir límites de tasa
        (HTTP 429) en reconexiones del Gateway. Use /sync para sincronizaciones explícitas.
        """
        user_str = str(self.user) if self.user else "Desconocido"
        user_id = self.user.id if self.user else 0
        logger.info(
            "LigaBot conectado exitosamente como %s (ID: %s). Servidores conectados: %d",
            user_str,
            user_id,
            len(self.guilds),
        )
