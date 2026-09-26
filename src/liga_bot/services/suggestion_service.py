"""Servicio de dominio para publicación de sugerencias en canales de Discord."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from discord.ext import commands

    from liga_bot.config import Settings

logger = logging.getLogger("liga_bot.services.suggestion")


class SuggestionDeliveryError(Exception):
    """Excepción lanzada cuando ocurre un error al entregar una sugerencia a Discord."""


class SuggestionService:
    """Gestiona la construcción y publicación de sugerencias en Discord."""

    def __init__(self, bot: discord.Client | commands.Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    async def post_suggestion(
        self,
        *,
        author_id: str | int,
        author_username: str,
        suggestion: str | None = None,
        content: str | None = None,
        avatar_url: str | None = None,
        created_at: str | datetime | None = None,
    ) -> tuple[int, int]:
        """Publica una sugerencia formateada en el canal configurado y añade reacciones.

        Soporta 'suggestion' o 'content' para compatibilidad total de invocación.
        Devuelve (message_id, channel_id).
        Lanza SuggestionDeliveryError si falla la resolución o el envío.
        """
        channel_id = self.settings.suggestions_channel_id
        if not channel_id or channel_id <= 0:
            raise SuggestionDeliveryError("suggestions_channel_id no está configurado.")

        # Validación del contenido
        text = (suggestion if suggestion is not None else content) or ""
        text = text.strip()
        if not text:
            raise SuggestionDeliveryError("El texto de la sugerencia no puede estar vacío.")

        # Sanitización de autor
        clean_author_id = str(author_id).strip()
        if not clean_author_id:
            raise SuggestionDeliveryError("El autor de la sugerencia no puede estar vacío.")
        clean_username = str(author_username).strip() or "Anónimo"

        # 1. Resolución de canal (caché -> fetch_channel)
        channel: Any = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as exc:
                raise SuggestionDeliveryError(
                    f"No se encontró el canal de sugerencias ({channel_id}): {exc}"
                ) from exc

        if not hasattr(channel, "send") or not callable(getattr(channel, "send", None)):
            ch_type = type(channel).__name__
            raise SuggestionDeliveryError(
                f"El canal ({channel_id}) no admite envío de mensajes (tipo {ch_type})."
            )

        # 2. Timestamp
        ts = datetime.now(timezone.utc)
        if created_at is not None:
            if isinstance(created_at, datetime):
                ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
            elif isinstance(created_at, str) and created_at.strip():
                iso_str = created_at.strip()
                if iso_str.endswith(("Z", "z")):
                    iso_str = iso_str[:-1] + "+00:00"
                try:
                    parsed = datetime.fromisoformat(iso_str)
                    ts = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    ts = datetime.now(timezone.utc)

        # 3. Construcción del Embed
        embed = discord.Embed(
            title="💡 Nueva Sugerencia",
            description=text,
            color=0x5865F2,
            timestamp=ts,
        )

        if avatar_url and (avatar_url.startswith("http://") or avatar_url.startswith("https://")):
            embed.set_author(name=clean_username, icon_url=avatar_url)
        else:
            embed.set_author(name=clean_username)

        embed.add_field(
            name="Autor",
            value=f"<@{clean_author_id}> ({clean_username})",
            inline=True,
        )
        embed.set_footer(text="RCL • Sistema de Sugerencias")

        # 4. Envío del mensaje
        try:
            message = await channel.send(embed=embed)
        except Exception as exc:
            raise SuggestionDeliveryError(
                f"Error al enviar mensaje al canal {channel_id}: {exc}"
            ) from exc

        # 5. Añadir reacciones de votación comunitaria
        for reaction in ("👍", "👎"):
            try:
                await message.add_reaction(reaction)
            except Exception as exc:
                logger.warning(
                    "No se pudo añadir reacción '%s' a la sugerencia %s: %s",
                    reaction,
                    message.id,
                    exc,
                )

        return message.id, channel.id
