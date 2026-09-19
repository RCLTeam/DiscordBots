"""
Componentes de interfaz de usuario desacoplados de Discord para la gestión de plantillas.

Define la vista interactiva GestionarPosicionView y sus selectores y botones subordinados
para modificar la posición y capitanía de miembros de plantilla en LigaBot:
- TeamSelect: Desplegable con los clubes a los que pertenece el usuario.
- PositionSelect: Desplegable con las 9 posiciones de RosterRole.
- SaveButton: Botón de confirmación con validación deportiva y manejo de conflictos.
- CancelButton: Botón de cancelación de la operación interactiva.
- GestionarPosicionView: Vista contenedora con timeout, interaction_check y gestión de estados.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import discord

from liga_bot.models.enums import RosterRole
from liga_bot.models.roster import Team, TeamMembership
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    InvalidCaptainRoleError,
    PlayerNotTeamMemberError,
    RosterSyncError,
    RosterSyncService,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selectores Desplegables
# ---------------------------------------------------------------------------


class TeamSelect(discord.ui.Select[Any]):
    """Desplegable para seleccionar el equipo objetivo del jugador."""

    def __init__(
        self,
        user_teams: list[tuple[Team, TeamMembership]],
        selected_team_id: UUID | None = None,
    ) -> None:
        self._values: list[str] = []
        options: list[discord.SelectOption] = []

        if not user_teams:
            options.append(
                discord.SelectOption(
                    label="Sin equipos asignados",
                    value="none",
                    description="El usuario no pertenece a ningún equipo registrado",
                )
            )
            super().__init__(
                placeholder="Sin equipos asignados...",
                min_values=1,
                max_values=1,
                options=options,
                disabled=True,
                row=0,
            )
            return

        for team, membership in user_teams:
            is_selected = selected_team_id == team.id
            desc = f"Rol actual: {membership.role.value}"
            if membership.is_captain:
                desc += " (Capitán)"
            options.append(
                discord.SelectOption(
                    label=f"{team.name} [{team.tag}]",
                    value=str(team.id),
                    description=desc[:100],
                    emoji="🛡️",
                    default=is_selected,
                )
            )

        super().__init__(
            placeholder="Selecciona el equipo a gestionar...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    @property
    def view(self) -> GestionarPosicionView:
        return super().view  # type: ignore[return-value]

    @property
    def values(self) -> list[str]:
        """Lista de valores seleccionados por el usuario."""
        return getattr(self, "_values", [])

    @values.setter
    def values(self, val: list[str]) -> None:
        self._values = list(val)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Callback invocado al seleccionar una opción de equipo."""
        if not self.values or self.values[0] == "none":
            return

        self.view.selected_team_id = UUID(self.values[0])
        for opt in self.options:
            opt.default = opt.value == self.values[0]

        if not interaction.response.is_done():
            await interaction.response.defer()


class PositionSelect(discord.ui.Select[Any]):
    """Desplegable para seleccionar el nuevo rol o posición de plantilla."""

    ROLE_METADATA: dict[RosterRole, tuple[str, str]] = {
        RosterRole.TOP: ("⚔️", "Línea superior (Titular competitivo)"),
        RosterRole.JUNGLE: ("🌲", "Jungla (Titular competitivo)"),
        RosterRole.MID: ("🧙", "Línea central (Titular competitivo)"),
        RosterRole.ADC: ("🏹", "Tirador / Bot (Titular competitivo)"),
        RosterRole.SUPPORT: ("🛡️", "Apoyo / Soporte (Titular competitivo)"),
        RosterRole.SUBSTITUTE: ("🔄", "Suplente (Rol competitivo)"),
        RosterRole.COACH: ("📋", "Entrenador (No competitivo, multiequipo)"),
        RosterRole.STAFF: ("💼", "Cuerpo técnico / Staff (No competitivo)"),
        RosterRole.PARTNERS: ("🤝", "Colaborador / Partner (No competitivo)"),
    }

    def __init__(self, selected_role: RosterRole | None = None) -> None:
        self._values: list[str] = []
        options: list[discord.SelectOption] = []
        for role, (emoji, desc) in self.ROLE_METADATA.items():
            options.append(
                discord.SelectOption(
                    label=role.value.capitalize(),
                    value=role.value,
                    description=desc,
                    emoji=emoji,
                    default=(selected_role == role),
                )
            )

        super().__init__(
            placeholder="Selecciona la nueva posición o rol...",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    @property
    def view(self) -> GestionarPosicionView:
        return super().view  # type: ignore[return-value]

    @property
    def values(self) -> list[str]:
        """Lista de valores seleccionados por el usuario."""
        return getattr(self, "_values", [])

    @values.setter
    def values(self, val: list[str]) -> None:
        self._values = list(val)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Callback invocado al seleccionar una posición de plantilla."""
        if not self.values:
            return

        self.view.selected_role = RosterRole(self.values[0])
        for opt in self.options:
            opt.default = opt.value == self.values[0]

        if not self.view.selected_role.is_starter():
            self.view.is_captain = False

        if not interaction.response.is_done():
            await interaction.response.defer()


# ---------------------------------------------------------------------------
# Botones de Acción
# ---------------------------------------------------------------------------


class SaveButton(discord.ui.Button[Any]):
    """Botón para persistir el cambio de posición tras validación deportiva."""

    def __init__(self) -> None:
        super().__init__(
            label="Guardar Posición",
            style=discord.ButtonStyle.success,
            emoji="💾",
            custom_id="roster:gestionar_posicion:save",
            row=2,
        )

    @property
    def view(self) -> GestionarPosicionView:
        return super().view  # type: ignore[return-value]

    async def callback(self, interaction: discord.Interaction) -> None:
        """Valida selecciones y delega la actualización a RosterSyncService."""
        # 1. Validación previa de campos requeridos
        if self.view.selected_team_id is None or self.view.selected_role is None:
            missing: list[str] = []
            if self.view.selected_team_id is None:
                missing.append("equipo")
            if self.view.selected_role is None:
                missing.append("posición/rol")

            msg = (
                f"⚠️ Faltan campos por seleccionar: **{', '.join(missing)}**. "
                "Por favor, selecciona ambos desplegables antes de guardar."
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        # 2. Defer de respuesta
        if not interaction.response.is_done():
            await interaction.response.defer()

        # 3. Llamada al servicio con captura exhaustiva de errores
        try:
            updated_membership = await self.view.roster_sync_service.change_player_position(
                discord_user_id=str(self.view.member.id),
                team_id=self.view.selected_team_id,
                new_role=self.view.selected_role,
                actor_id=str(self.view.actor.id),
                is_captain=self.view.is_captain,
            )
        except CompetitivePositionConflictError as exc:
            conflict_embed = self.view.build_conflict_embed(exc)
            if interaction.response.is_done():
                await interaction.followup.send(embed=conflict_embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=conflict_embed, ephemeral=True)
            return
        except InvalidCaptainRoleError as exc:
            msg = (
                f"❌ **Error de Capitanía**: El rol `{exc.role}` "
                "no es una posición titular permitida.\n"
                "Solo las posiciones titulares ('top', 'jungle', 'mid', 'adc', 'support') "
                "pueden ser capitanes."
            )
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        except PlayerNotTeamMemberError as exc:
            msg = (
                f"❌ **Error de Membresía**: El usuario <@{exc.discord_user_id}> ya no pertenece "
                "a la plantilla del equipo objetivo."
            )
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        except (ValueError, RosterSyncError) as exc:
            msg = f"❌ **Error**: {exc}"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        except Exception as exc:
            logger.exception("Error imprevisto al cambiar posición de plantilla: %s", exc)
            msg = (
                "❌ Ocurrió un error interno inesperado al guardar la posición. "
                "Por favor, inténtalo de nuevo o contacta al administrador."
            )
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        # 4. Éxito: Deshabilitar componentes y enviar embed resumen
        for child in self.view.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        self.view.stop()

        success_embed = self.view.build_success_embed(updated_membership)
        if interaction.response.is_done():
            if hasattr(interaction, "edit_original_response"):
                await interaction.edit_original_response(embed=success_embed, view=self.view)
            else:
                await interaction.followup.send(embed=success_embed, ephemeral=True)
        else:
            await interaction.response.edit_message(embed=success_embed, view=self.view)


SavePositionButton = SaveButton


class CancelButton(discord.ui.Button[Any]):
    """Botón para cancelar la operación interactiva."""

    def __init__(self) -> None:
        super().__init__(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
            emoji="❌",
            custom_id="roster:gestionar_posicion:cancel",
            row=2,
        )

    @property
    def view(self) -> GestionarPosicionView:
        return super().view  # type: ignore[return-value]

    async def callback(self, interaction: discord.Interaction) -> None:
        """Deshabilita la vista y notifica la cancelación."""
        for child in self.view.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        self.view.stop()

        cancel_embed = discord.Embed(
            title="Operación Cancelada",
            description="La gestión de posición ha sido cancelada sin modificaciones.",
            color=discord.Color.light_grey(),
        )
        if interaction.response.is_done():
            if hasattr(interaction, "edit_original_response"):
                await interaction.edit_original_response(embed=cancel_embed, view=self.view)
            else:
                await interaction.followup.send(embed=cancel_embed, ephemeral=True)
        else:
            await interaction.response.edit_message(embed=cancel_embed, view=self.view)


# ---------------------------------------------------------------------------
# Vista Contenedora Principal
# ---------------------------------------------------------------------------


class GestionarPosicionView(discord.ui.View):
    """
    Vista interactiva de Discord para gestionar la posición y rol de plantilla de un jugador.
    """

    def __init__(
        self,
        member: discord.Member,
        user_teams: list[tuple[Team, TeamMembership]],
        roster_sync_service: RosterSyncService,
        actor: discord.Member | discord.User,
        timeout: float | None = 180.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.member: discord.Member = member
        self.user_teams: list[tuple[Team, TeamMembership]] = user_teams
        self.roster_sync_service: RosterSyncService = roster_sync_service
        self.actor: discord.Member | discord.User = actor
        self.message: discord.Message | discord.WebhookMessage | None = None

        # Auto-selección si solo pertenece a 1 equipo
        if len(user_teams) == 1:
            self.selected_team_id: UUID | None = user_teams[0][0].id
        else:
            self.selected_team_id = None

        self.selected_role: RosterRole | None = None
        self.is_captain: bool = False

        # Agregar componentes interactivos
        self.team_select = TeamSelect(
            user_teams=self.user_teams,
            selected_team_id=self.selected_team_id,
        )
        self.position_select = PositionSelect(selected_role=self.selected_role)
        self.save_button = SaveButton()
        self.cancel_button = CancelButton()

        if not self.user_teams:
            self.team_select.disabled = True
            self.save_button.disabled = True

        self.add_item(self.team_select)
        self.add_item(self.position_select)
        self.add_item(self.save_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Garantiza que únicamente el actor que instanció el panel pueda accionar los controles."""
        if interaction.user.id == self.actor.id:
            return True

        await interaction.response.send_message(
            "⛔ No tienes autorización para interactuar con este panel de gestión de posiciones.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        """Deshabilita todos los componentes interactivos tras expirar el timeout."""
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException) as exc:
                logger.debug(
                    "No se pudo actualizar el mensaje tras timeout de GestionarPosicionView: %s",
                    exc,
                )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        """Manejador global de errores en componentes de la vista."""
        logger.exception(
            "Error no controlado en el componente %s de GestionarPosicionView: %s",
            item.__class__.__name__,
            error,
        )
        msg = "❌ Ocurrió un error inesperado al procesar la interacción con el panel."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    def build_initial_embed(self) -> discord.Embed:
        """Construye el embed inicial informativo para el panel."""
        embed = discord.Embed(
            title="Gestión de Posición de Plantilla",
            description=(
                f"Selecciona el equipo y la nueva posición a asignar a {self.member.mention}.\n"
                "Pulsa **Guardar Posición** para aplicar los cambios en la base de datos."
            ),
            color=discord.Color.blue(),
        )
        avatar_url = getattr(getattr(self.member, "display_avatar", None), "url", None)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(
            name="👤 Jugador",
            value=f"{self.member.mention} (`{self.member.name}`)",
            inline=True,
        )
        embed.add_field(
            name="🛡️ Equipos Pertenecientes",
            value=str(len(self.user_teams)),
            inline=True,
        )
        embed.set_footer(text="RCL League • Gestión de Plantillas")
        return embed

    def build_success_embed(self, updated_membership: TeamMembership) -> discord.Embed:
        """Construye el recibo detallado de cambio de posición exitoso."""
        team = next((t for t, _ in self.user_teams if t.id == self.selected_team_id), None)
        team_name = f"{team.name} [{team.tag}]" if team else str(self.selected_team_id)

        prev_membership = next(
            (m for t, m in self.user_teams if t.id == self.selected_team_id), None
        )
        prev_role_str = (
            prev_membership.role.value
            if prev_membership and hasattr(prev_membership.role, "value")
            else "Desconocido"
        )

        embed = discord.Embed(
            title="✅ Posición Actualizada Exitosamente",
            description=(
                f"Se ha actualizado correctamente la asignación de {self.member.mention} "
                "en la plantilla oficial de la liga."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        avatar_url = getattr(getattr(self.member, "display_avatar", None), "url", None)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(
            name="👤 Jugador",
            value=f"{self.member.mention} (`{self.member.name}`)",
            inline=True,
        )
        embed.add_field(name="🛡️ Equipo", value=team_name, inline=True)
        embed.add_field(name="📋 Rol Anterior", value=f"`{prev_role_str}`", inline=True)

        new_role_val = (
            updated_membership.role.value
            if hasattr(updated_membership.role, "value")
            else str(updated_membership.role)
        )
        embed.add_field(
            name="🎯 Nuevo Rol",
            value=f"**`{new_role_val}`**",
            inline=True,
        )

        is_comp = False
        if hasattr(updated_membership.role, "is_competitive"):
            is_comp = updated_membership.role.is_competitive()
        elif isinstance(updated_membership.role, str):
            try:
                is_comp = RosterRole(updated_membership.role).is_competitive()
            except ValueError:
                pass

        embed.add_field(
            name="⚡ Tipo de Posición",
            value="Competitiva (Posición Única)" if is_comp else "No Competitiva (Multiequipo)",
            inline=True,
        )
        embed.add_field(
            name="👑 Capitanía",
            value="Sí (Capitán oficial)" if updated_membership.is_captain else "No",
            inline=True,
        )
        actor_mention = getattr(self.actor, "mention", str(self.actor))
        embed.add_field(name="✍️ Modificado por", value=actor_mention, inline=False)
        embed.set_footer(text="RCL League • Sincronización de Plantillas")
        return embed

    def build_conflict_embed(self, exc: CompetitivePositionConflictError) -> discord.Embed:
        """Construye el embed de advertencia ante conflicto con la invariante competitiva."""
        embed = discord.Embed(
            title="⚠️ Conflicto de Posición Competitiva",
            description=(
                f"No es posible asignar el rol solicitado a {self.member.mention} "
                "debido al reglamento de posiciones competitivas de la liga.\n\n"
                f"• **Jugador**: {self.member.mention}\n"
                f"• **Equipo Actual**: `{exc.existing_team_id}` (Rol: `{exc.existing_role}`)\n"
                f"• **Equipo Objetivo**: `{exc.new_team_id}`\n"
                f"  (Rol Intentado: `{exc.attempted_role}`)\n\n"
                "**Regla Deportiva (Posición Única)**:\n"
                "Un jugador solo puede ocupar un rol competitivo "
                "(`top`, `jungle`, `mid`, `adc`, `support`, `substitute`) "
                "en **como máximo 1 equipo** en toda la liga. Puede ostentar roles no competitivos "
                "(`coach`, `staff`, `partners`) en múltiples clubes simultáneamente.\n\n"
                "**Acción recomendada**:\n"
                "1. Selecciona un rol no competitivo (`coach`, `staff` o `partners`), o\n"
                "2. Modifica la posición en el otro club o tramita su baja."
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Acción rechazada por validación de invariante deportiva")
        return embed


__all__ = [
    "CancelButton",
    "GestionarPosicionView",
    "PositionSelect",
    "SaveButton",
    "SavePositionButton",
    "TeamSelect",
]
