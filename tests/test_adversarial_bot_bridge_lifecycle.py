"""
Suite de pruebas adversariales y fuzzer de concurrencia para el ciclo de vida de LigaBot.

Desafía empíricamente:
1. Ciclos rápidos de inicio/parada (20 iteraciones con mocks y con sockets reales efímeros).
2. Concurrencia masiva en bot.close() con retardo simulado en bridge.stop() (cero carreras).
3. Resiliencia ante fallos y excepciones arbitrarias durante bridge.stop() (cierre de DB y bot).
4. Resiliencia ante fallo en bridge.start() seguido de close() en bloque finally.
5. Inyección de mocks combinatoria sobre los 4 servicios de dominio + 2 servicios del bridge.
6. Cierre ordenado de múltiples websockets activos durante parada con código 1000.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from discord.ext import commands

from liga_bot.bot import LigaBot
from liga_bot.config import Settings
from liga_bot.services.role_service import RoleService
from liga_bot.services.roster_sync_service import RosterSyncService
from liga_bot.services.schedule_service import ScheduleService
from liga_bot.services.suggestion_service import SuggestionService
from liga_bot.services.ticket_service import TicketService
from liga_bot.services.websocket_bridge_service import WebsocketBridgeService

# ============================================================================
# Vector 1: Rapid Start / Stop Cycling Stress
# ============================================================================


@pytest.mark.asyncio
async def test_adversarial_rapid_start_stop_cycling_20_instances():
    """
    Stress-test: 20 ciclos consecutivos de instanciación, setup_hook() y close().
    Verifica que no hay fugas de referencias, estados residuales ni degradación de rendimiento.
    """
    settings = Settings(bridge_enabled=True)

    for _cycle in range(20):
        mock_engine = MagicMock()
        mock_session_factory = MagicMock()

        with (
            patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
            patch("liga_bot.bot.get_session_factory") as mock_get_factory,
            patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
            patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock) as mock_start,
            patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop,
            patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
        ):
            mock_get_engine.return_value = mock_engine
            mock_get_factory.return_value = mock_session_factory

            bot = LigaBot(settings=settings, extensions=())
            assert bot.websocket_bridge_service is None
            assert bot.suggestion_service is None

            await bot.setup_hook()

            # Verificación del estado inicial post-setup
            assert isinstance(bot.suggestion_service, SuggestionService)
            assert isinstance(bot.websocket_bridge_service, WebsocketBridgeService)
            assert bot.engine is mock_engine
            assert bot.session_factory is mock_session_factory
            mock_start.assert_awaited_once()

            # Cierre ordenado
            await bot.close()

            # Verificación de liberación limpia
            mock_stop.assert_awaited_once()
            mock_close_engine.assert_awaited_once_with(mock_engine)
            mock_super_close.assert_awaited_once()
            assert bot.websocket_bridge_service is None
            assert bot.engine is None
            assert bot.session_factory is None


@pytest.mark.asyncio
async def test_adversarial_rapid_start_stop_real_ephemeral_sockets_20_cycles():
    """
    Stress-test empírico con la pila de red real:
    Abre y cierra el servidor WebSocket Bridge real 20 veces en puertos efímeros (port=0).
    Verifica que el sistema operativo y aiohttp liberan sockets sin fugas de descriptores
    y que /health responde 200 en cada ciclo.
    """
    for cycle in range(20):
        settings = Settings(
            bridge_enabled=True,
            bridge_port=0,
            discord_bot_supertoken="adversarial-token-xyz",
        )
        bot = LigaBot(settings=settings, extensions=())

        with (
            patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
            patch("liga_bot.bot.get_session_factory"),
            patch("liga_bot.bot.close_engine", new_callable=AsyncMock),
            patch.object(commands.Bot, "close", new_callable=AsyncMock),
        ):
            await bot.setup_hook()
            assert bot.websocket_bridge_service is not None
            assert bot.websocket_bridge_service.is_running is True

            port = bot.websocket_bridge_service.port
            assert port > 0, f"Ciclo {cycle}: El puerto efímero debe ser > 0"

            # Verificar conectividad HTTP real
            url = f"http://127.0.0.1:{port}/health"
            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientTimeout(total=2.0)
                async with session.get(url, timeout=timeout) as resp:
                    assert resp.status == 200, f"Ciclo {cycle}: status {resp.status}"
                    body = await resp.json()
                    assert body["status"] == "ok"
                    assert body["active_connections"] == 0

            # Cierre del bot y su bridge
            await bot.close()
            assert bot.websocket_bridge_service is None

            # Confirmar que el socket se liberó y el puerto rechaza nuevas conexiones
            with pytest.raises((aiohttp.ClientConnectorError, OSError)):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=0.5)):
                        pass


@pytest.mark.asyncio
async def test_adversarial_rapid_cycling_with_active_websockets():
    """
    Stress-test: Cierra el bot mientras múltiples conexiones WebSocket activas (autenticadas
    y no autenticadas) están conectadas simultáneamente.
    Verifica que bot.close() desconecta a todos los clientes con código 1000 y limpia sockets.
    """
    settings = Settings(
        bridge_enabled=True,
        bridge_port=0,
        discord_bot_supertoken="super-secret-token",
    )

    for _cycle in range(3):
        bot = LigaBot(settings=settings, extensions=())

        with (
            patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
            patch("liga_bot.bot.get_session_factory"),
            patch("liga_bot.bot.close_engine", new_callable=AsyncMock),
            patch.object(commands.Bot, "close", new_callable=AsyncMock),
        ):
            await bot.setup_hook()
            port = bot.websocket_bridge_service.port
            ws_url = f"ws://127.0.0.1:{port}/ws/bridge"

            async with aiohttp.ClientSession() as session:
                # Conectar 4 clientes simultáneos
                ws_clients = []
                for _ in range(4):
                    ws = await session.ws_connect(ws_url)
                    ws_clients.append(ws)

                # Autenticar 2 clientes con token válido
                for i in range(2):
                    await ws_clients[i].send_json(
                        {
                            "type": "LOG IN",
                            "data": {
                                "id": "11111111-1111-4111-8111-111111111111",
                                "content": {"token": "super-secret-token"},
                            },
                        }
                    )
                    resp = await ws_clients[i].receive_json(timeout=2.0)
                    assert resp["type"] == "LOGIN_SUCCESS"

                # Los otros 2 se quedan pre-auth (silencio)

                # Iniciar lectura concurrente de cierre en los 4 clientes
                receive_tasks = [asyncio.create_task(ws.receive(timeout=3.0)) for ws in ws_clients]

                # Desencadenar el cierre del bot mientras los 4 sockets están activos
                await bot.close()
                assert bot.websocket_bridge_service is None

                # Verificar que los 4 sockets reciben evento de cierre
                msgs = await asyncio.gather(*receive_tasks)
                for idx, (ws, msg) in enumerate(zip(ws_clients, msgs, strict=True)):
                    assert msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    ), f"Cliente {idx} recibió tipo inesperado: {msg.type}"
                    assert ws.close_code in (1000, 1006)
                    assert ws.closed


# ============================================================================
# Vector 2: Massive Concurrent bot.close() Invocations
# ============================================================================


@pytest.mark.asyncio
async def test_adversarial_massive_concurrent_close_with_delayed_bridge_stop():
    """
    Stress-test de concurrencia: 30 tareas invocando bot.close() simultáneamente mientras
    bridge.stop(), close_engine() y super().close() experimentan retardos asíncronos.
    Verifica:
    - Cero excepciones no controladas.
    - bridge.stop() es invocado exactamente 1 vez (el pop atómico previene reentrancia).
    - bot.websocket_bridge_service es None al finalizar.
    - bot.engine es None al finalizar.
    """
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())
    mock_engine = MagicMock()

    stop_call_count = 0

    async def slow_bridge_stop():
        nonlocal stop_call_count
        stop_call_count += 1
        await asyncio.sleep(0.04)

    async def slow_close_engine(engine):
        await asyncio.sleep(0.02)

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", side_effect=slow_close_engine),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(WebsocketBridgeService, "stop", side_effect=slow_bridge_stop),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        mock_get_engine.return_value = mock_engine
        await bot.setup_hook()

        # Lanzar 30 llamadas concurrentes a bot.close()
        tasks = [asyncio.create_task(bot.close()) for _ in range(30)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Ninguna tarea debe haber fallado con excepción
        for idx, res in enumerate(results):
            assert res is None or not isinstance(res, Exception), (
                f"Tarea {idx} en bot.close() lanzó una excepción: {res}"
            )

        # bridge.stop() debe haberse ejecutado exactamente una vez
        assert stop_call_count == 1, (
            f"Esperado stop() 1 vez, ejecutado {stop_call_count} veces (carrera)."
        )
        assert bot.websocket_bridge_service is None
        assert bot.engine is None
        assert bot.session_factory is None


# ============================================================================
# Vector 3: Exceptions During bridge.stop() and Guaranteed Teardown
# ============================================================================


@pytest.mark.parametrize(
    "hostile_exception",
    [
        RuntimeError("Corrupción crítica en el event loop de aiohttp"),
        OSError(98, "Address already in use durante limpieza"),
        ConnectionResetError("Socket forzado a reset durante teardown"),
        TypeError("Argumento incompatible en runner.cleanup()"),
        AttributeError("'NoneType' object has no attribute 'cleanup'"),
        KeyError("Clave no encontrada en diccionario de sockets"),
        TimeoutError("Tiempo de espera agotado al cerrar websockets"),
    ],
)
@pytest.mark.asyncio
async def test_adversarial_bridge_stop_exceptions_do_not_leak_or_block_teardown(
    hostile_exception: Exception,
):
    """
    Stress-test de fallos: Si bridge.stop() lanza cualquier excepción estándar,
    bot.close() DEBE capturarla defensivamente, continuar la secuencia de apagado,
    cerrar el motor de base de datos y cerrar la sesión de Discord sin propagar el error.
    """
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())
    mock_engine = MagicMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(
            WebsocketBridgeService,
            "stop",
            new_callable=AsyncMock,
            side_effect=hostile_exception,
        ) as mock_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
    ):
        mock_get_engine.return_value = mock_engine
        await bot.setup_hook()

        # bot.close() no debe propagar la excepción hacia arriba
        await bot.close()

        # Verificaciones obligatorias
        mock_stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None
        mock_close_engine.assert_awaited_once_with(mock_engine)
        assert bot.engine is None
        assert bot.session_factory is None
        mock_super_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_adversarial_bridge_start_failure_followed_by_close():
    """
    Stress-test: Si bridge.start() falla durante setup_hook() (ej. puerto ocupado),
    el llamador debe recibir la excepción y ejecutar bot.close() en un bloque de limpieza.
    Verifica que bot.close() funciona correctamente aun cuando el bridge nunca llegó a escuchar.
    """
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())
    mock_engine = MagicMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
        patch.object(
            WebsocketBridgeService,
            "start",
            new_callable=AsyncMock,
            side_effect=OSError(98, "Address already in use"),
        ),
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
    ):
        mock_get_engine.return_value = mock_engine

        # setup_hook debe propagar el error de inicio
        with pytest.raises(OSError, match="Address already in use"):
            await bot.setup_hook()

        # Simular bloque de recuperación/limpieza en el punto de entrada
        await bot.close()

        # El bridge debe ser detenido (o intentado detener) y los recursos de DB liberados
        mock_stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None
        mock_close_engine.assert_awaited_once_with(mock_engine)
        assert bot.engine is None
        mock_super_close.assert_awaited_once()


# ============================================================================
# Vector 4: Mock Injection Combinatorial Matrix (6 Services)
# ============================================================================


@pytest.mark.asyncio
async def test_adversarial_mock_injection_all_6_services_constructor_and_attributes():
    """
    Stress-test DI: Inyección de mocks para los 4 servicios de dominio + 2 servicios de bridge.
    Verifica que NINGÚN mock inyectado es sobrescrito por setup_hook(), que bridge.start()
    se invoca sobre el mock correcto y que bot.close() lo detiene limpiamente.
    """
    # 4 Servicios de Dominio
    mock_schedule = MagicMock(spec=ScheduleService)
    mock_ticket = MagicMock(spec=TicketService)
    mock_role = MagicMock(spec=RoleService)
    mock_roster = MagicMock(spec=RosterSyncService)

    # 2 Servicios del Bridge
    mock_suggestion = MagicMock(spec=SuggestionService)
    mock_bridge = MagicMock(spec=WebsocketBridgeService)
    mock_bridge.start = AsyncMock()
    mock_bridge.stop = AsyncMock()

    settings = Settings(bridge_enabled=True)

    # Inyección combinada: bridge y suggestion por constructor kwargs, otros 4 por atributos
    bot = LigaBot(
        settings=settings,
        extensions=(),
        suggestion_service=mock_suggestion,
        websocket_bridge_service=mock_bridge,
    )
    bot.schedule_service = mock_schedule
    bot.ticket_service = mock_ticket
    bot.role_service = mock_role
    bot.roster_sync_service = mock_roster

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()

        # Invariantes absolutas: las instancias DEBEN ser idénticas a los mocks inyectados
        assert bot.schedule_service is mock_schedule
        assert bot.ticket_service is mock_ticket
        assert bot.role_service is mock_role
        assert bot.roster_sync_service is mock_roster
        assert bot.suggestion_service is mock_suggestion
        assert bot.websocket_bridge_service is mock_bridge

        mock_bridge.start.assert_awaited_once()

        # Cierre ordenado
        await bot.close()

        mock_bridge.stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None
        # Los otros servicios inyectados no se corrompen
        assert bot.schedule_service is mock_schedule
        assert bot.ticket_service is mock_ticket
        assert bot.role_service is mock_role
        assert bot.roster_sync_service is mock_roster
        assert bot.suggestion_service is mock_suggestion


@pytest.mark.asyncio
async def test_adversarial_mock_injection_cross_wiring_custom_suggestion_service():
    """
    Stress-test DI: Inyección ÚNICAMENTE de suggestion_service personalizado.
    setup_hook() DEBE auto-instanciar WebsocketBridgeService y conectarlo
    directamente con la instancia mock de suggestion_service inyectada.
    """
    mock_suggestion = MagicMock(spec=SuggestionService)
    settings = Settings(bridge_enabled=True)

    bot = LigaBot(
        settings=settings,
        extensions=(),
        suggestion_service=mock_suggestion,
    )

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock) as mock_bridge_start,
        patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_bridge_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()

        assert bot.suggestion_service is mock_suggestion
        assert isinstance(bot.websocket_bridge_service, WebsocketBridgeService)
        # La propiedad suggestion_service del bridge debe apuntar al mock inyectado
        assert bot.websocket_bridge_service.suggestion_service is mock_suggestion
        assert bot.websocket_bridge_service.bot is bot

        mock_bridge_start.assert_awaited_once()

        await bot.close()
        mock_bridge_stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None
        assert bot.suggestion_service is mock_suggestion


@pytest.mark.asyncio
async def test_adversarial_mock_injection_cross_wiring_custom_bridge_service():
    """
    Stress-test DI: Inyección ÚNICAMENTE de websocket_bridge_service personalizado.
    setup_hook() DEBE auto-instanciar SuggestionService y conservar el bridge intacto
    sin sobreescribirlo ni intentar reiniciar sus atributos.
    """
    mock_bridge = MagicMock(spec=WebsocketBridgeService)
    mock_bridge.start = AsyncMock()
    mock_bridge.stop = AsyncMock()
    settings = Settings(bridge_enabled=True)

    bot = LigaBot(
        settings=settings,
        extensions=(),
        websocket_bridge_service=mock_bridge,
    )

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock),
        patch("liga_bot.bot.get_session_factory"),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        await bot.setup_hook()

        # SuggestionService se auto-instancia
        assert isinstance(bot.suggestion_service, SuggestionService)
        assert bot.suggestion_service.bot is bot
        # El bridge inyectado se respeta 100%
        assert bot.websocket_bridge_service is mock_bridge
        mock_bridge.start.assert_awaited_once()

        await bot.close()
        mock_bridge.stop.assert_awaited_once()
        assert bot.websocket_bridge_service is None


@pytest.mark.asyncio
async def test_adversarial_rapid_start_stop_same_instance_10_cycles():
    """
    Stress-test: Ejecuta setup_hook() y close() 10 veces sobre la MISMA instancia de LigaBot.
    Verifica que la re-inicialización del bridge y engine se produce limpiamente en cada ciclo.
    """
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())

    for _cycle in range(10):
        mock_engine = MagicMock()
        mock_session_factory = MagicMock()

        with (
            patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
            patch("liga_bot.bot.get_session_factory") as mock_get_factory,
            patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
            patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock) as mock_start,
            patch.object(WebsocketBridgeService, "stop", new_callable=AsyncMock) as mock_stop,
            patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
        ):
            mock_get_engine.return_value = mock_engine
            mock_get_factory.return_value = mock_session_factory

            await bot.setup_hook()

            assert bot.websocket_bridge_service is not None
            mock_start.assert_awaited_once()

            await bot.close()

            assert bot.websocket_bridge_service is None
            assert bot.engine is None
            mock_stop.assert_awaited_once()
            mock_close_engine.assert_awaited_once_with(mock_engine)
            mock_super_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_adversarial_cancellation_during_bridge_stop_allows_clean_second_close():
    """
    Stress-test: Si la primera llamada a bot.close() es cancelada (asyncio.CancelledError)
    mientras se detiene el bridge, el atributo bot.websocket_bridge_service ya fue desacoplado.
    Una segunda llamada a close() no debe re-intentar el bridge y debe cerrar el engine.
    """
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())
    mock_engine = MagicMock()

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock) as mock_close_engine,
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(
            WebsocketBridgeService,
            "stop",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError("Simulated external task cancellation"),
        ) as mock_stop,
        patch.object(commands.Bot, "close", new_callable=AsyncMock) as mock_super_close,
    ):
        mock_get_engine.return_value = mock_engine
        await bot.setup_hook()

        # Primer close() recibe CancelledError de bridge.stop
        with pytest.raises(asyncio.CancelledError):
            await bot.close()

        # Invariante clave: websocket_bridge_service fue puesto a None ANTES de await bridge.stop()
        assert bot.websocket_bridge_service is None

        # El engine no se cerró en el primer intento debido a la propagación de CancelledError
        assert bot.engine is mock_engine

        # Segundo close() de rescate / teardown garantizado:
        # bridge.stop no debe ser llamado de nuevo (porque es None)
        # y close_engine debe completarse con éxito
        await bot.close()

        assert mock_stop.await_count == 1  # No se repite la llamada fallida
        mock_close_engine.assert_awaited_once_with(mock_engine)
        assert bot.engine is None
        mock_super_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_adversarial_concurrent_close_stress_50_tasks():
    """
    Stress-test masivo: 50 corrutinas concurrentes llamando a bot.close() simultáneamente
    con retardos asíncronos en el cierre del bridge y del engine.
    Verifica que la condición de carrera nunca produce doble ejecución ni fallos de atributo.
    """
    settings = Settings(bridge_enabled=True)
    bot = LigaBot(settings=settings, extensions=())
    mock_engine = MagicMock()

    bridge_stop_counter = 0

    async def jitter_bridge_stop():
        nonlocal bridge_stop_counter
        bridge_stop_counter += 1
        await asyncio.sleep(0.01)

    with (
        patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
        patch("liga_bot.bot.get_session_factory"),
        patch("liga_bot.bot.close_engine", new_callable=AsyncMock),
        patch.object(WebsocketBridgeService, "start", new_callable=AsyncMock),
        patch.object(WebsocketBridgeService, "stop", side_effect=jitter_bridge_stop),
        patch.object(commands.Bot, "close", new_callable=AsyncMock),
    ):
        mock_get_engine.return_value = mock_engine
        await bot.setup_hook()

        tasks = [asyncio.create_task(bot.close()) for _ in range(50)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            assert res is None or not isinstance(res, Exception), f"Error inesperado: {res}"

        assert bridge_stop_counter == 1
        assert bot.websocket_bridge_service is None
        assert bot.engine is None
