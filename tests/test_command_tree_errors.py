"""
Pruebas del manejador global de errores de los slash commands (LigaCommandTree.on_error).

Los comandos se registran de verdad en el árbol de un LigaBot y se invocan con
`CommandTree._call`, el mismo método que usa discord.py al recibir la interacción,
para que la excepción recorra el camino real hasta `on_error`.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from liga_bot.bot import LigaBot
from liga_bot.command_tree import (
    MENSAJE_COMANDO_DESINCRONIZADO,
    MENSAJE_ERROR_INESPERADO,
    MENSAJE_SIN_PERMISOS,
    MENSAJE_SOLO_EN_SERVIDOR,
    LigaCommandTree,
)
from liga_bot.config import Settings


class _RespuestaFalsa:
    """Imita `discord.InteractionResponse`: registra si la interacción ya tiene respuesta."""

    def __init__(self) -> None:
        self._hecha = False
        self.send_message = AsyncMock(side_effect=self._marcar_hecha)
        self.defer = AsyncMock(side_effect=self._marcar_hecha)

    async def _marcar_hecha(self, *args: Any, **kwargs: Any) -> None:
        if self._hecha:
            raise discord.InteractionResponded(None)  # type: ignore[arg-type]
        self._hecha = True

    def is_done(self) -> bool:
        return self._hecha


class _InteraccionFalsa:
    """Interacción de slash command con lo que leen `CommandTree._call` y `on_error`."""

    def __init__(self, nombre_comando: str) -> None:
        self.data = {"type": 1, "name": nombre_comando, "options": []}
        self.type = discord.InteractionType.application_command
        self.command_failed = False
        self.user = SimpleNamespace(id=111)
        self.guild_id = 222
        self.guild = None
        self._state = None
        self.response = _RespuestaFalsa()
        self.followup = SimpleNamespace(send=AsyncMock())

    @property
    def command(self) -> Any:
        return getattr(self, "_cs_command", None)


@pytest.fixture
async def bot():
    instancia = LigaBot(settings=Settings(bridge_enabled=False), extensions=())
    yield instancia
    await instancia.close()


async def _ejecutar(bot: LigaBot, comando: app_commands.Command) -> _InteraccionFalsa:
    bot.tree.add_command(comando)
    interaccion = _InteraccionFalsa(comando.name)
    await bot.tree._call(interaccion)  # type: ignore[arg-type]
    return interaccion


async def test_ligabot_usa_el_arbol_con_manejador_global(bot):
    assert isinstance(bot.tree, LigaCommandTree)


async def test_excepcion_antes_de_responder_envia_respuesta_efimera(bot):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        raise RuntimeError("fallo de base de datos")

    interaccion = await _ejecutar(bot, prueba)

    interaccion.response.send_message.assert_awaited_once_with(
        MENSAJE_ERROR_INESPERADO, ephemeral=True
    )
    interaccion.followup.send.assert_not_awaited()


async def test_excepcion_despues_de_defer_envia_followup_efimero(bot):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        raise RuntimeError("fallo tras defer")

    interaccion = await _ejecutar(bot, prueba)

    interaccion.followup.send.assert_awaited_once_with(MENSAJE_ERROR_INESPERADO, ephemeral=True)
    interaccion.response.send_message.assert_not_awaited()


async def test_excepcion_se_registra_en_error_con_traza_comando_y_usuario(bot, caplog):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        raise RuntimeError("fallo de base de datos")

    with caplog.at_level(logging.ERROR, logger="liga_bot.command_tree"):
        await _ejecutar(bot, prueba)

    registros = [r for r in caplog.records if r.name == "liga_bot.command_tree"]
    assert len(registros) == 1
    registro = registros[0]
    assert registro.levelno == logging.ERROR
    assert registro.exc_info is not None
    assert isinstance(registro.exc_info[1], RuntimeError)
    mensaje = registro.getMessage()
    assert "/prueba" in mensaje
    assert "111" in mensaje
    assert "222" in mensaje


async def test_mensaje_al_usuario_no_incluye_el_detalle_de_la_excepcion(bot):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        raise RuntimeError("postgresql://usuario:secreto@host/bd")

    interaccion = await _ejecutar(bot, prueba)

    (mensaje,), _ = interaccion.response.send_message.await_args
    assert "secreto" not in mensaje


async def test_comando_que_gestiona_su_error_no_cambia_su_respuesta(bot):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        try:
            raise RuntimeError("fallo controlado")
        except RuntimeError:
            await interaction.response.send_message(
                "❌ Mensaje propio del comando.", ephemeral=True
            )

    interaccion = await _ejecutar(bot, prueba)

    interaccion.response.send_message.assert_awaited_once_with(
        "❌ Mensaje propio del comando.", ephemeral=True
    )
    interaccion.followup.send.assert_not_awaited()


async def test_comando_con_manejador_propio_no_recibe_respuesta_del_global(bot):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        raise RuntimeError("fallo")

    manejador_propio = AsyncMock()
    prueba.error(manejador_propio)

    interaccion = await _ejecutar(bot, prueba)

    manejador_propio.assert_awaited_once()
    interaccion.response.send_message.assert_not_awaited()
    interaccion.followup.send.assert_not_awaited()


async def test_check_fallido_sin_respuesta_previa_avisa_de_permisos(bot):
    def denegar(interaction: discord.Interaction) -> bool:
        return False

    @app_commands.command(name="prueba", description="Prueba")
    @app_commands.check(denegar)
    async def prueba(interaction: discord.Interaction) -> None:
        raise AssertionError("el check debería impedir la ejecución")

    interaccion = await _ejecutar(bot, prueba)

    interaccion.response.send_message.assert_awaited_once_with(MENSAJE_SIN_PERMISOS, ephemeral=True)


async def test_check_que_ya_respondio_no_recibe_otra_respuesta(bot):
    async def denegar_respondiendo(interaction: discord.Interaction) -> bool:
        await interaction.response.send_message("❌ Solo staff.", ephemeral=True)
        return False

    @app_commands.command(name="prueba", description="Prueba")
    @app_commands.check(denegar_respondiendo)
    async def prueba(interaction: discord.Interaction) -> None:
        raise AssertionError("el check debería impedir la ejecución")

    interaccion = await _ejecutar(bot, prueba)

    interaccion.response.send_message.assert_awaited_once_with("❌ Solo staff.", ephemeral=True)
    interaccion.followup.send.assert_not_awaited()


async def test_comando_solo_en_servidor_usado_por_md(bot):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        raise app_commands.NoPrivateMessage()

    interaccion = await _ejecutar(bot, prueba)

    interaccion.response.send_message.assert_awaited_once_with(
        MENSAJE_SOLO_EN_SERVIDOR, ephemeral=True
    )


async def test_bot_sin_permisos_indica_los_permisos_que_faltan(bot):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        raise app_commands.BotMissingPermissions(["manage_roles"])

    interaccion = await _ejecutar(bot, prueba)

    (mensaje,), kwargs = interaccion.response.send_message.await_args
    assert kwargs == {"ephemeral": True}
    assert "manage_roles" in mensaje.replace("\\", "")


async def test_check_fallido_se_registra_sin_nivel_error(bot, caplog):
    def denegar(interaction: discord.Interaction) -> bool:
        return False

    @app_commands.command(name="prueba", description="Prueba")
    @app_commands.check(denegar)
    async def prueba(interaction: discord.Interaction) -> None:
        return None

    with caplog.at_level(logging.DEBUG, logger="liga_bot.command_tree"):
        await _ejecutar(bot, prueba)

    registros = [r for r in caplog.records if r.name == "liga_bot.command_tree"]
    assert registros
    assert all(r.levelno < logging.ERROR for r in registros)


async def test_valor_de_opcion_no_convertible_da_mensaje_especifico(bot):
    interaccion = _InteraccionFalsa("prueba")
    error = app_commands.TransformerError("abc", discord.AppCommandOptionType.integer, _Entero())

    await bot.tree.on_error(interaccion, error)  # type: ignore[arg-type]

    (mensaje,), kwargs = interaccion.response.send_message.await_args
    assert kwargs == {"ephemeral": True}
    assert "abc" in mensaje
    assert mensaje != MENSAJE_ERROR_INESPERADO


async def test_comando_desconocido_pide_sincronizar(bot):
    interaccion = _InteraccionFalsa("inexistente")

    await bot.tree.on_error(  # type: ignore[arg-type]
        interaccion, app_commands.CommandNotFound("inexistente", [])
    )

    interaccion.response.send_message.assert_awaited_once_with(
        MENSAJE_COMANDO_DESINCRONIZADO, ephemeral=True
    )


async def test_fallo_al_enviar_el_aviso_no_propaga_excepciones(bot, caplog):
    @app_commands.command(name="prueba", description="Prueba")
    async def prueba(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        raise RuntimeError("fallo tras defer")

    bot.tree.add_command(prueba)
    interaccion = _InteraccionFalsa("prueba")
    respuesta_http = SimpleNamespace(status=404, reason="Not Found")
    interaccion.followup.send.side_effect = discord.NotFound(respuesta_http, "Unknown interaction")

    with caplog.at_level(logging.WARNING, logger="liga_bot.command_tree"):
        await bot.tree._call(interaccion)  # type: ignore[arg-type]

    assert any(r.levelno == logging.WARNING for r in caplog.records)


class _Entero(app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: Any) -> int:
        return int(value)
