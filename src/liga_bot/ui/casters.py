"""
Discord UI components for caster assignments, match broadcast panels,
and persistent action buttons using DynamicItem.
"""

from __future__ import annotations

import inspect
import logging
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

import discord

from liga_bot.config import Settings, get_settings
from liga_bot.models.caster import CasterRole
from liga_bot.services.caster_service import CasterService, MatchCastersData

if TYPE_CHECKING:
    from liga_bot.models.match import Match

logger = logging.getLogger(__name__)

# Configuración de botones de acción
ACTION_CONFIG: dict[str, dict[str, Any]] = {
    "cast": {
        "label": "Castear",
        "emoji": "🎙️",
        "style": discord.ButtonStyle.primary,
    },
    "stream": {
        "label": "Retransmitir",
        "emoji": "📺",
        "style": discord.ButtonStyle.secondary,
    },
    "both": {
        "label": "Ambas mezcladas",
        "emoji": "🎙️📺",
        "style": discord.ButtonStyle.success,
    },
    "leave": {
        "label": "Desapuntarse",
        "emoji": "❌",
        "style": discord.ButtonStyle.danger,
    },
}

ACTION_TO_ROLE: dict[str, CasterRole] = {
    "cast": CasterRole.CASTER,
    "stream": CasterRole.STREAMER,
    "both": CasterRole.BOTH,
}


def build_match_caster_embed(match: Match, casters_data: MatchCastersData) -> discord.Embed:
    """
    Genera un discord.Embed con la cartelera del partido, división, horario,
    retransmisión activa, casters asignados y estado de cobertura.
    """
    # 1. División
    division_name = "División"
    if getattr(match, "season_division", None) is not None:
        sd = match.season_division
        if hasattr(sd, "division") and getattr(sd.division, "name", None):
            division_name = sd.division.name
        elif getattr(sd, "division_name", None):
            division_name = sd.division_name
    elif getattr(match, "division", None) is not None:
        div = match.division
        division_name = div.value if hasattr(div, "value") else str(div)

    # 2. Equipos y Jornada
    t1_name = getattr(match.team1, "name", "Equipo 1") if match.team1 else "Equipo 1"
    t2_name = getattr(match.team2, "name", "Equipo 2") if match.team2 else "Equipo 2"
    jornada = getattr(match, "jornada", 1)

    title = f"{division_name} · Jornada {jornada} · {t1_name} vs {t2_name}"

    # 3. Estado de Cobertura y Color
    has_streamer = casters_data.has_streamer
    has_casters = len(casters_data.casters) > 0

    if has_streamer and has_casters:
        status_text = "🟢 Cobertura lista (Streamer + Casters)"
        color = discord.Color.green()
    elif has_streamer and not has_casters:
        status_text = "🟡 Falta caster (solo retransmisión)"
        color = discord.Color.gold()
    elif not has_streamer and has_casters:
        status_text = "🟡 Falta retransmisión (solo audio)"
        color = discord.Color.gold()
    else:
        status_text = "⚪ Vacante (sin cubrir)"
        color = discord.Color.blurple()

    embed = discord.Embed(
        title=title,
        color=color,
    )

    # 4. Horario
    if getattr(match, "scheduled_at", None) is not None and match.scheduled_at is not None:
        ts = int(match.scheduled_at.timestamp())
        horario_val = f"<t:{ts}:F> (<t:{ts}:R>)"
    else:
        horario_val = "*Por determinar*"
    embed.add_field(name="Horario", value=horario_val, inline=False)

    # 5. Retransmisión
    if casters_data.has_streamer and casters_data.streamer is not None:
        streamer = casters_data.streamer
        if streamer.caster_role == CasterRole.STREAMER:
            streamer_val = f"<@{streamer.discord_user_id}> (Solo PC)"
        elif streamer.caster_role == CasterRole.BOTH:
            streamer_val = f"<@{streamer.discord_user_id}> (Caster + PC)"
        else:
            streamer_val = f"<@{streamer.discord_user_id}>"
    else:
        streamer_val = "*Vacante (disponible)*"
    embed.add_field(name="📺 Retransmisión", value=streamer_val, inline=True)

    # 6. Casters
    if casters_data.casters:
        casters_val = ", ".join(f"<@{c.discord_user_id}>" for c in casters_data.casters)
    else:
        casters_val = "*Sin casters asignados*"
    embed.add_field(name="🎙️ Casters", value=casters_val, inline=True)

    # 7. Estado
    embed.add_field(name="Estado", value=status_text, inline=False)

    # 8. Footer
    embed.set_footer(text="RCL · Rebel Crown Legacy")

    return embed


class CasterActionButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"^caster:(?P<action>cast|stream|both|leave):(?P<match_id>[0-9a-fA-F-]+)$",
):
    """
    Botón interactivo dinámico persistente ante reinicios del bot (DynamicItem)
    para la asignación y desasignación de casters y retransmisiones.
    """

    def __init__(
        self,
        action: str,
        match_id: UUID | str,
        disabled: bool = False,
    ) -> None:
        cfg = ACTION_CONFIG.get(action)
        if cfg is None:
            raise ValueError(f"Acción de casteo desconocida: {action!r}")

        parsed_match_id = match_id if isinstance(match_id, UUID) else UUID(str(match_id))
        custom_id = f"caster:{action}:{parsed_match_id}"

        button = discord.ui.Button(
            label=cfg["label"],
            style=cfg["style"],
            emoji=cfg["emoji"],
            custom_id=custom_id,
            disabled=disabled,
        )
        super().__init__(button)

        self.action: str = action
        self.match_id: UUID = parsed_match_id
        # Redirigir el callback del botón interno a este mismo objeto
        self.item.callback = self.callback

    @property
    def disabled(self) -> bool:
        return self.item.disabled

    @disabled.setter
    def disabled(self, value: bool) -> None:
        self.item.disabled = value

    @property
    def style(self) -> discord.ButtonStyle:
        return self.item.style

    @style.setter
    def style(self, value: discord.ButtonStyle) -> None:
        self.item.style = value

    @property
    def label(self) -> str | None:
        return self.item.label

    @label.setter
    def label(self, value: str | None) -> None:
        self.item.label = value

    @property
    def emoji(self) -> discord.PartialEmoji | str | None:
        return self.item.emoji

    @emoji.setter
    def emoji(self, value: discord.PartialEmoji | str | None) -> None:
        self.item.emoji = value

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> CasterActionButton:
        """Reconstruye el botón a partir del match regex de custom_id."""
        action = match["action"]
        match_id = UUID(match["match_id"])
        disabled = getattr(item, "disabled", False)
        return cls(action=action, match_id=match_id, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Gestiona el clic en el botón de asignación o desasignación de casters."""
        client = interaction.client
        settings: Settings = getattr(client, "settings", None) or get_settings()

        # 1. Verificación de rol configurado (si caster_role_id > 0)
        if settings.caster_role_id > 0:
            from liga_bot.cogs.permissions import is_staff, resolve_member

            member = await resolve_member(interaction)
            has_role = False
            if member is not None:
                user_roles = {r.id for r in getattr(member, "roles", [])}
                if settings.caster_role_id in user_roles:
                    has_role = True

            if not has_role:
                staff_check = is_staff(interaction, settings)
                is_authorized_staff = (
                    await staff_check if inspect.isawaitable(staff_check) else bool(staff_check)
                )
                if not is_authorized_staff:
                    await interaction.response.send_message(
                        "❌ No tienes el rol necesario para apuntarte como caster.",
                        ephemeral=True,
                    )
                    return

        # 2. Resolución de CasterService
        caster_service: CasterService | None = getattr(client, "caster_service", None)
        if caster_service is None:
            session_factory = getattr(client, "session_factory", None)
            caster_service = CasterService(
                session_factory=session_factory,
                settings=settings,
                bot=client,
            )

        # 3. Ejecución del servicio según la acción
        if self.action in ACTION_TO_ROLE:
            role = ACTION_TO_ROLE[self.action]
            result = await caster_service.assign_caster(self.match_id, interaction.user.id, role)
        elif self.action == "leave":
            result = await caster_service.remove_caster(self.match_id, interaction.user.id)
        else:
            await interaction.response.send_message(
                f"❌ Acción desconocida: {self.action}",
                ephemeral=True,
            )
            return

        # 4. Manejo de error del servicio
        if not result.success:
            error_msg = result.error or "Error al procesar la solicitud."
            await interaction.response.send_message(f"❌ {error_msg}", ephemeral=True)
            return

        # 5. Actualización in-place del mensaje de Discord
        match = await caster_service.get_match(self.match_id)
        if match is None:
            await interaction.response.send_message(
                "❌ No se encontró la información del partido.",
                ephemeral=True,
            )
            return

        updated_data = result.data or await caster_service.get_match_casters_data(self.match_id)
        updated_embed = build_match_caster_embed(match, updated_data)
        updated_view = MatchCasterView(match_id=self.match_id, casters_data=updated_data)

        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=updated_embed, view=updated_view)
        else:
            await interaction.edit_original_response(embed=updated_embed, view=updated_view)

        await interaction.followup.send(
            "✅ Tu asignación ha sido actualizada.",
            ephemeral=True,
        )


class MatchCasterView(discord.ui.View):
    """Vista persistente interactiva para la cartelera de casters y retransmisiones."""

    def __init__(
        self,
        match_id: UUID | str,
        casters_data: MatchCastersData | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.match_id: UUID = match_id if isinstance(match_id, UUID) else UUID(str(match_id))
        self.casters_data: MatchCastersData | None = casters_data

        has_streamer = casters_data.has_streamer if casters_data is not None else False

        self.btn_cast: CasterActionButton = CasterActionButton(
            action="cast",
            match_id=self.match_id,
            disabled=False,
        )
        self.btn_stream: CasterActionButton = CasterActionButton(
            action="stream",
            match_id=self.match_id,
            disabled=has_streamer,
        )
        self.btn_both: CasterActionButton = CasterActionButton(
            action="both",
            match_id=self.match_id,
            disabled=has_streamer,
        )
        self.btn_leave: CasterActionButton = CasterActionButton(
            action="leave",
            match_id=self.match_id,
            disabled=False,
        )

        self.add_item(self.btn_cast)
        self.add_item(self.btn_stream)
        self.add_item(self.btn_both)
        self.add_item(self.btn_leave)
