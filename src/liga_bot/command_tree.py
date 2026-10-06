"""
Árbol de slash commands de LigaBot con manejador global de errores.

`LigaCommandTree.on_error` recibe toda excepción que un slash command o menú
contextual no capture. Registra el fallo y avisa con un mensaje efímero a quien
ejecutó el comando, tanto si el comando aún no había respondido como si ya había
hecho `defer()`. Los comandos que capturan sus propios errores no llegan aquí, y
los que tienen manejador propio (`@comando.error` o `cog_app_command_error`)
conservan su comportamiento. La tabla de mensajes está en
`docs/architecture/runtime.md`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

MENSAJE_ERROR_INESPERADO: Final[str] = (
    "❌ Se ha producido un error inesperado al ejecutar el comando. "
    "Inténtalo de nuevo más tarde y, si se repite, avisa al staff."
)
MENSAJE_SIN_PERMISOS: Final[str] = "❌ No tienes permisos para usar este comando."
MENSAJE_SOLO_EN_SERVIDOR: Final[str] = "❌ Este comando solo puede usarse dentro de un servidor."
MENSAJE_COMANDO_DESINCRONIZADO: Final[str] = (
    "❌ Este comando no coincide con la versión actual del bot. "
    "El staff debe sincronizar los comandos con /sync."
)

# Longitud máxima del valor de una opción que se repite en el aviso al usuario.
_MAX_VALOR_MOSTRADO: Final[int] = 100


def _nombre_comando(interaction: discord.Interaction) -> str:
    return getattr(interaction.command, "qualified_name", None) or "<desconocido>"


def _id_usuario(interaction: discord.Interaction) -> Any:
    return getattr(getattr(interaction, "user", None), "id", None)


def _mensaje_check_fallido(error: app_commands.CheckFailure) -> str:
    if isinstance(error, app_commands.NoPrivateMessage):
        return MENSAJE_SOLO_EN_SERVIDOR
    if isinstance(error, app_commands.BotMissingPermissions):
        permisos = discord.utils.escape_markdown(", ".join(error.missing_permissions))
        return f"❌ Al bot le faltan permisos para ejecutar este comando: {permisos}."
    if isinstance(error, app_commands.CommandOnCooldown):
        return (
            "⏳ Has usado este comando hace muy poco. "
            f"Vuelve a intentarlo en {error.retry_after:.0f} s."
        )
    return MENSAJE_SIN_PERMISOS


def _mensaje_valor_no_valido(error: app_commands.TransformerError) -> str:
    valor = str(error.value)
    if len(valor) > _MAX_VALOR_MOSTRADO:
        valor = valor[:_MAX_VALOR_MOSTRADO] + "…"
    return (
        f"❌ El valor «{discord.utils.escape_markdown(valor)}» no es válido "
        "para una de las opciones del comando."
    )


class LigaCommandTree(app_commands.CommandTree):
    """`CommandTree` cuyo `on_error` registra el fallo y responde en efímero."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        command = interaction.command
        if command is not None and command._has_any_error_handlers():
            # El comando o su Cog ya gestionan el error: mismo criterio que discord.py.
            return

        nombre = _nombre_comando(interaction)
        usuario = _id_usuario(interaction)
        servidor = interaction.guild_id

        if isinstance(error, app_commands.CheckFailure):
            logger.info(
                "Comando /%s rechazado para el usuario %s (servidor %s): %s",
                nombre,
                usuario,
                servidor,
                error,
            )
            if interaction.response.is_done():
                # El check ya respondió al usuario: no se envía un segundo aviso.
                return
            await self._avisar(interaction, _mensaje_check_fallido(error), nombre)
            return

        if isinstance(error, app_commands.TransformerError):
            logger.info(
                "Comando /%s del usuario %s (servidor %s) con una opción no válida: %s",
                nombre,
                usuario,
                servidor,
                error,
            )
            await self._avisar(interaction, _mensaje_valor_no_valido(error), nombre)
            return

        if isinstance(error, (app_commands.CommandNotFound, app_commands.CommandSignatureMismatch)):
            logger.warning(
                "Comando /%s del usuario %s (servidor %s) no coincide con el árbol del bot: %s",
                nombre,
                usuario,
                servidor,
                error,
            )
            await self._avisar(interaction, MENSAJE_COMANDO_DESINCRONIZADO, nombre)
            return

        original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        logger.error(
            "Error no controlado en el comando /%s (usuario %s, servidor %s)",
            nombre,
            usuario,
            servidor,
            exc_info=original,
        )
        await self._avisar(interaction, MENSAJE_ERROR_INESPERADO, nombre)

    @staticmethod
    async def _avisar(interaction: discord.Interaction, mensaje: str, nombre: str) -> None:
        """Envía el aviso efímero; usa `followup` si ya hubo respuesta o `defer()`."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(mensaje, ephemeral=True)
            else:
                await interaction.response.send_message(mensaje, ephemeral=True)
        except (discord.HTTPException, discord.InteractionResponded) as exc:
            # La interacción puede haber caducado o haberse respondido entre medias.
            logger.warning("No se pudo avisar del error del comando /%s: %s", nombre, exc)
