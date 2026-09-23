"""Servidor WebSocket asíncrono para integración de comandos y sugerencias."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from liga_bot.services.bridge_protocol import extract_request_id
from liga_bot.services.rate_limiter import SlidingWindowRateLimiter
from liga_bot.services.suggestion_service import SuggestionDeliveryError

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

    from liga_bot.config import Settings
    from liga_bot.services.suggestion_service import SuggestionService

logger = logging.getLogger("liga_bot.services.websocket_bridge")


class WebsocketBridgeService:
    """Gestiona el servidor HTTP/WebSocket interno para integración con servicios externos."""

    def __init__(
        self,
        bot: discord.Client | commands.Bot,
        settings: Settings,
        suggestion_service: SuggestionService | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        *,
        auth_timeout_seconds: float = 10.0,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.suggestion_service = suggestion_service
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            limit=settings.bridge_rate_limit_per_minute,
            window_seconds=60.0,
        )
        self.auth_timeout_seconds = max(0.01, float(auth_timeout_seconds))

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._active_sockets: set[web.WebSocketResponse] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._is_running: bool = False

    @property
    def is_running(self) -> bool:
        """Indica si el servidor WebSocket está activo y escuchando."""
        return self._is_running and self._site is not None

    @property
    def port(self) -> int:
        """Devuelve el puerto efectivo en el que escucha el servidor (soporta puerto 0 efímero)."""
        if self._site is not None and getattr(self._site, "_server", None) is not None:
            sockets = getattr(self._site._server, "sockets", None)
            if sockets:
                return sockets[0].getsockname()[1]
        return self.settings.bridge_port

    async def start(self) -> None:
        """Inicia el servidor web asíncrono en el bucle de eventos del bot."""
        if not self.settings.bridge_enabled:
            logger.info("WebSocket Bridge deshabilitado por configuración (bridge_enabled=False).")
            return

        if self.is_running:
            logger.warning("WebSocket Bridge ya se encuentra en ejecución.")
            return

        self._app = web.Application()
        self._app.router.add_get("/ws/bridge", self._handle_ws)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(
            self._runner,
            host=self.settings.bridge_host,
            port=self.settings.bridge_port,
        )

        try:
            await self._site.start()
            self._is_running = True
            logger.info(
                "WebSocket Bridge escuchando en ws://%s:%s/ws/bridge",
                self.settings.bridge_host,
                self.port,
            )
        except Exception:
            self._is_running = False
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None
            raise

    async def stop(self) -> None:
        """Detiene el servidor y cierra todas las conexiones y tareas activas ordenadamente."""
        self._is_running = False

        # 1. Cerrar todos los sockets activos con código 1000
        for ws in list(self._active_sockets):
            if not ws.closed:
                try:
                    await ws.close(code=1000, message=b"Server shutting down")
                except Exception as exc:
                    logger.debug("Error cerrando WebSocket durante stop: %s", exc)
        self._active_sockets.clear()

        # 2. Drenar tareas de entrega en segundo plano pendientes
        if self._background_tasks:
            done, pending = await asyncio.wait(self._background_tasks, timeout=2.0)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._background_tasks.clear()

        # 3. Detener y limpiar el runner de aiohttp
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:
                logger.warning("Error durante limpieza de AppRunner: %s", exc)
            self._runner = None
            self._site = None
            self._app = None

        logger.info("WebSocket Bridge detenido correctamente.")

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Endpoint de verificación de estado y conexiones activas."""
        is_ready = False
        is_ready_attr = getattr(self.bot, "is_ready", None)
        if callable(is_ready_attr):
            ready_val = is_ready_attr()
            if not asyncio.iscoroutine(ready_val):
                is_ready = bool(ready_val)

        return web.json_response(
            {
                "status": "ok",
                "bot_ready": is_ready,
                "active_connections": len(self._active_sockets),
                "connections": len(self._active_sockets),
            }
        )

    def _extract_token(self, payload: dict[str, Any]) -> str | None:
        """Extrae el token de autenticación admitiendo las 3 variantes del protocolo."""
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None

        content = data.get("content")
        if isinstance(content, dict):
            token = content.get("token") or content.get("supertoken")
            if isinstance(token, str) and token.strip():
                return token.strip()
        elif isinstance(content, str) and content.strip():
            return content.strip()

        token = data.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()

        return None

    def _validate_token(self, token: str | None) -> bool:
        """Valida el token mediante secrets.compare_digest evitando bypass por supertoken vacío."""
        expected = self.settings.discord_bot_supertoken
        if not expected or not expected.strip():
            logger.warning("Intento de login rechazado: discord_bot_supertoken no configurado.")
            return False
        if not token or not isinstance(token, str):
            return False
        return secrets.compare_digest(token, expected)

    async def _auth_timeout(
        self,
        ws: web.WebSocketResponse,
        is_authenticated_check: Any,
    ) -> None:
        """Cierra el socket si no se recibe autenticación válida en el plazo configurado."""
        try:
            await asyncio.sleep(self.auth_timeout_seconds)
            if not is_authenticated_check() and not ws.closed:
                logger.info("Cerrando WebSocket por timeout de autenticación (código 4001).")
                await ws.close(code=4001, message=b"Authentication timeout")
        except asyncio.CancelledError:
            pass

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Manejador principal del ciclo de vida de WebSocket."""
        ws = web.WebSocketResponse(heartbeat=20.0, autoping=True)
        await ws.prepare(request)
        self._active_sockets.add(ws)

        is_authenticated = False
        login_timer = asyncio.create_task(self._auth_timeout(ws, lambda: is_authenticated))

        try:
            async for msg in ws:
                if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    continue

                try:
                    payload = json.loads(msg.data)
                except Exception:
                    # R1: JSON malformado -> descarte silencioso absoluto
                    continue

                req_id = extract_request_id(payload)
                if not req_id:
                    # R1: Ausencia o invalidez de UUID -> descarte silencioso absoluto
                    continue

                cmd_type = str(payload.get("type", "")).strip().upper()

                if not is_authenticated:
                    if cmd_type in ("LOG IN", "LOGIN"):
                        token = self._extract_token(payload)
                        if self._validate_token(token):
                            is_authenticated = True
                            login_timer.cancel()
                            await ws.send_json(
                                {
                                    "type": "LOGIN_SUCCESS",
                                    "data": {"id": req_id, "status": "ok"},
                                }
                            )
                        else:
                            # Token inválido: silencio absoluto sin respuesta de error
                            pass
                    # Pre-login: cualquier otro comando es ignorado en silencio
                    continue

                # Sesión autenticada: despacho multiplexado
                if cmd_type == "SUGGESTION_CREATED":
                    await self._process_suggestion(ws, req_id, payload)
                elif cmd_type in ("LOG IN", "LOGIN"):
                    # Idempotente para clientes ya autenticados
                    await ws.send_json(
                        {
                            "type": "LOGIN_SUCCESS",
                            "data": {"id": req_id, "status": "ok"},
                        }
                    )
        finally:
            if not login_timer.done():
                login_timer.cancel()
            self._active_sockets.discard(ws)

        return ws

    async def _process_suggestion(
        self,
        ws: web.WebSocketResponse,
        req_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Valida cuota con SlidingWindowRateLimiter y despacha entrega en 2 fases."""
        allowed, retry_after = await self.rate_limiter.acquire("global")
        if not allowed:
            if not ws.closed:
                try:
                    await ws.send_json(
                        {
                            "type": "ERROR",
                            "data": {
                                "id": req_id,
                                "code": "RATE_LIMITED",
                                "retry_after_seconds": round(retry_after, 1),
                            },
                        }
                    )
                except (ConnectionResetError, RuntimeError):
                    pass
            return

        # Fase 1: Confirmación inmediata de cola
        if not ws.closed:
            try:
                await ws.send_json(
                    {
                        "type": "SUGGESTION_QUEUED",
                        "data": {
                            "id": req_id,
                            "status": "queued",
                        },
                    }
                )
            except (ConnectionResetError, RuntimeError):
                return

        # Fase 2: Tarea asíncrona desacoplada con retención en _background_tasks
        task = asyncio.create_task(self._deliver_suggestion(ws, req_id, payload))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _deliver_suggestion(
        self,
        ws: web.WebSocketResponse,
        req_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Ejecuta la entrega en Discord y notifica el resultado sobre el WebSocket."""
        data = payload.get("data")
        content = data.get("content") if isinstance(data, dict) else {}
        content_dict = (
            content if isinstance(content, dict) else (data if isinstance(data, dict) else {})
        )

        author_id = content_dict.get("author_id", "0")
        author_username = (
            content_dict.get("author_username") or content_dict.get("username") or "Anónimo"
        )
        suggestion_text = content_dict.get("suggestion") or content_dict.get("content") or ""
        avatar_url = content_dict.get("avatar_url")
        created_at = content_dict.get("created_at")

        if self.suggestion_service is None:
            logger.error("SuggestionService no está configurado en WebsocketBridgeService.")
            if not ws.closed:
                try:
                    await ws.send_json(
                        {
                            "type": "SUGGESTION_FAILED",
                            "data": {
                                "id": req_id,
                                "code": "DISCORD_ERROR",
                                "message": "SuggestionService no está disponible.",
                            },
                        }
                    )
                except (ConnectionResetError, RuntimeError):
                    pass
            return

        try:
            msg_id, ch_id = await self.suggestion_service.post_suggestion(
                author_id=author_id,
                author_username=str(author_username),
                suggestion=str(suggestion_text),
                avatar_url=avatar_url,
                created_at=created_at,
            )
            if not ws.closed:
                try:
                    await ws.send_json(
                        {
                            "type": "SUGGESTION_CONFIRMED",
                            "data": {
                                "id": req_id,
                                "message_id": int(msg_id),
                                "channel_id": int(ch_id),
                            },
                        }
                    )
                except (ConnectionResetError, RuntimeError):
                    pass
        except SuggestionDeliveryError as exc:
            if not ws.closed:
                try:
                    await ws.send_json(
                        {
                            "type": "SUGGESTION_FAILED",
                            "data": {
                                "id": req_id,
                                "code": "DISCORD_ERROR",
                                "message": str(exc),
                            },
                        }
                    )
                except (ConnectionResetError, RuntimeError):
                    pass
        except Exception as exc:
            logger.error("Error inesperado entregando sugerencia: %s", exc, exc_info=True)
            if not ws.closed:
                try:
                    await ws.send_json(
                        {
                            "type": "SUGGESTION_FAILED",
                            "data": {
                                "id": req_id,
                                "code": "DISCORD_ERROR",
                                "message": str(exc) or "Error interno al procesar la sugerencia.",
                            },
                        }
                    )
                except (ConnectionResetError, RuntimeError):
                    pass
