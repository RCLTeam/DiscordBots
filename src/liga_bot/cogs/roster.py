"""
Módulo de presentación de Discord para la sincronización de plantillas de equipos
y gestión interactiva de posiciones de plantilla con RCL-Next.

Define RosterCog con:
- Listener on_member_update para sincronización automática de altas y bajas
  de membresía en BD ante adición o remoción de roles de equipo en Discord.
- Slash command /gestionar-posicion para desplegar la interfaz interactiva
  GestionarPosicionView y modificar rol de plantilla y capitanía.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from liga_bot.cogs.permissions import is_staff, resolve_member
from liga_bot.config import Settings, get_settings
from liga_bot.models.enums import RosterRole
from liga_bot.services.roster_sync_service import RosterSyncError
from liga_bot.ui.roster import GestionarPosicionView
from liga_bot.utils.formatting import apply_team_tag, strip_team_tag

if TYPE_CHECKING:
    from liga_bot.bot import LigaBot
    from liga_bot.services.roster_sync_service import RosterSyncService

logger = logging.getLogger(__name__)


class RosterCog(commands.Cog, name="Roster"):
    """
    Cog responsable de la sincronización de roles de equipo y la gestión
    de posiciones de plantilla.
    """

    def __init__(
        self,
        bot: LigaBot | commands.Bot,
        settings: Settings | None = None,
    ) -> None:
        self.bot: LigaBot | commands.Bot = bot
        self._settings = settings

    @property
    def settings(self) -> Settings:
        """Resuelve de forma resiliente la configuración de la aplicación."""
        if self._settings is not None:
            return self._settings
        bot_settings = getattr(self.bot, "settings", None)
        return bot_settings if bot_settings is not None else get_settings()

    @property
    def roster_sync_service(self) -> RosterSyncService | None:
        """Resuelve el servicio de sincronización de plantillas desde el bot."""
        return getattr(self.bot, "roster_sync_service", None)

    # ---------------------------------------------------------------------------
    # Event Listener: Sincronización Automática de Roles
    # ---------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """
        Detecta cambios en los roles de un miembro de Discord y delega la sincronización
        en RosterSyncService de forma secuencial y resiliente ante fallos parciales:
        - Roles añadidos: invoca handle_role_added (crea membresía si es rol de club).
        - Roles eliminados: invoca handle_role_removed (da de baja la membresía si correspondía).
        - Aislamiento de excepciones: un fallo en un rol no interrumpe el resto.
        """
        before_roles = getattr(before, "roles", [])
        after_roles = getattr(after, "roles", [])

        added_roles = [r for r in after_roles if r not in before_roles]
        removed_roles = [r for r in before_roles if r not in after_roles]

        if not added_roles and not removed_roles:
            return

        service = self.roster_sync_service
        if service is None:
            logger.warning(
                "RosterSyncService no disponible en el bot; omitiendo sync para %s (%s).",
                getattr(after, "display_name", str(after)),
                getattr(after, "id", "desconocido"),
            )
            return

        # 1. Procesar roles añadidos secuencialmente
        for role in added_roles:
            try:
                membership = await service.handle_role_added(member=after, role=role)
                if membership is not None:
                    logger.info(
                        "Membresía sincronizada (alta): usuario '%s' (%s), rol '%s' (%s).",
                        after.display_name,
                        after.id,
                        role.name,
                        role.id,
                    )
            except Exception as exc:
                logger.error(
                    "Error al procesar alta de rol '%s' (%s) para usuario '%s' (%s): %s",
                    getattr(role, "name", "desconocido"),
                    getattr(role, "id", "desconocido"),
                    after.display_name,
                    after.id,
                    exc,
                    exc_info=True,
                )

        # 2. Procesar roles eliminados secuencialmente
        for role in removed_roles:
            try:
                removed = await service.handle_role_removed(member=after, role=role)
                if removed:
                    logger.info(
                        "Membresía sincronizada (baja): usuario '%s' (%s), rol '%s' (%s).",
                        after.display_name,
                        after.id,
                        role.name,
                        role.id,
                    )
            except Exception as exc:
                logger.error(
                    "Error al procesar retirada de rol '%s' (%s) para usuario '%s' (%s): %s",
                    getattr(role, "name", "desconocido"),
                    getattr(role, "id", "desconocido"),
                    after.display_name,
                    after.id,
                    exc,
                    exc_info=True,
                )

    # ---------------------------------------------------------------------------
    # Slash Command: /gestionar-posicion
    # ---------------------------------------------------------------------------

    @app_commands.command(
        name="gestionar-posicion",
        description="Gestiona la posición y rol de plantilla de un jugador en sus equipos",
    )
    @app_commands.describe(
        member="Miembro del servidor cuya posición de plantilla se desea gestionar",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gestionar_posicion(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        """Abre el panel interactivo para gestionar la posición y rol de plantilla."""
        # 1. Validación de contexto de servidor
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        # 2. Verificación de autorización de Staff / Administrador
        is_authorized = await is_staff(interaction.user, self.settings)
        if not is_authorized:
            await interaction.response.send_message(
                "❌ Solo el personal de staff tiene autorización para gestionar posiciones.",
                ephemeral=True,
            )
            return

        # 3. Verificación previa de disponibilidad del servicio
        service = self.roster_sync_service
        if service is None:
            await interaction.response.send_message(
                "❌ El servicio de sincronización de plantillas no está disponible.",
                ephemeral=True,
            )
            return

        # 4. Respuesta diferida efímera
        await interaction.response.defer(ephemeral=True)

        target_member = await resolve_member(member) or member

        # 5. Consulta de equipos pertenecientes
        try:
            user_teams = await service.get_user_teams(str(target_member.id))
        except Exception as exc:
            logger.error(
                "Error al consultar equipos para usuario %s (%s): %s",
                getattr(target_member, "display_name", str(target_member)),
                target_member.id,
                exc,
                exc_info=True,
            )
            await interaction.followup.send(
                "❌ Ocurrió un error inesperado al consultar los equipos del usuario en la base.",
                ephemeral=True,
            )
            return

        # 6. Caso sin equipos: enviar advertencia informativa sin vista interactiva
        if not user_teams:
            desc = (
                f"El usuario {target_member.mention} (`{target_member.display_name}`) "
                "no pertenece a la plantilla de ningún equipo registrado en la liga.\n\n"
                "Para gestionar su posición competitiva o rol en plantilla, primero "
                "debe tener asignado al menos un rol de equipo oficial en Discord."
            )
            empty_embed = discord.Embed(
                title="🛡️ Sin Equipos Registrados",
                description=desc,
                color=discord.Color.orange(),
            )
            avatar_url = getattr(getattr(target_member, "display_avatar", None), "url", None)
            if avatar_url:
                empty_embed.set_thumbnail(url=avatar_url)
            empty_embed.set_footer(text="RCL League • Sincronización de Plantillas")
            await interaction.followup.send(embed=empty_embed, ephemeral=True)
            return

        # 7. Caso con equipos: instanciar vista interactiva y enviar panel
        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=interaction.user,
        )
        msg = await interaction.followup.send(
            embed=view.build_initial_embed(),
            view=view,
            ephemeral=True,
        )
        view.message = msg

    # ---------------------------------------------------------------------------
    # Slash Command: /traspasa-equipo
    # ---------------------------------------------------------------------------

    @app_commands.command(
        name="traspasa-equipo",
        description="Traspasa a un jugador al equipo indicado con su nueva posición (Solo Staff)",
    )
    @app_commands.describe(
        usuario="Jugador que se traspasa",
        equipo="Rol de Discord del equipo de destino",
        posicion="Posición que ocupará en la plantilla",
        nombre_lol="Nombre de invocador para el apodo (opcional: por defecto el actual)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def traspasa_equipo(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        equipo: discord.Role,
        posicion: RosterRole,
        nombre_lol: str | None = None,
    ) -> None:
        """Traspasa a un jugador a otro equipo actualizando plantilla y roles de Discord."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        is_authorized = await is_staff(interaction.user, self.settings)
        if not is_authorized:
            await interaction.response.send_message(
                "❌ Solo el personal de staff puede traspasar jugadores.",
                ephemeral=True,
            )
            return

        service = self.roster_sync_service
        if service is None:
            await interaction.response.send_message(
                "❌ El servicio de sincronización de plantillas no está disponible.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        target_member = await resolve_member(usuario) or usuario

        # 1. Base de datos primero: así el listener on_member_update encuentra la
        #    membresía ya creada con su posición y no la recrea con el rol por defecto.
        try:
            _, team, previous_team = await service.transfer_player(
                member=target_member,
                team_role=equipo,
                new_position=posicion,
                actor_id=interaction.user.id,
            )
        except RosterSyncError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            logger.error(
                "Error al traspasar a %s al equipo %s: %s",
                getattr(target_member, "display_name", str(target_member)),
                equipo.name,
                exc,
                exc_info=True,
            )
            await interaction.followup.send(
                "❌ Ocurrió un error inesperado al registrar el traspaso.",
                ephemeral=True,
            )
            return

        # 2. Sincronizar los roles de Discord con la plantilla resultante
        avisos: list[str] = []
        if previous_team is not None:
            old_role = interaction.guild.get_role(previous_team.discord_role_id)
            if old_role is not None and old_role in target_member.roles:
                try:
                    await target_member.remove_roles(old_role)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("No se pudo retirar el rol '%s': %s", old_role.name, exc)
                    avisos.append(f"no se pudo retirar el rol {old_role.mention}")

        if equipo not in target_member.roles:
            try:
                await target_member.add_roles(equipo)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("No se pudo asignar el rol '%s': %s", equipo.name, exc)
                avisos.append(f"no se pudo asignar el rol {equipo.mention}")

        # 3. Apodo canónico según prioridad deportiva competitiva
        base_nick = (nombre_lol or "").strip() or target_member.display_name
        try:
            nuevo_nick = await service.resolve_canonical_nick(
                discord_user_id=target_member.id,
                base_name=base_nick,
            )
        except Exception as exc:
            logger.warning(
                "No se pudo resolver el apodo canónico para %s: %s",
                target_member.display_name,
                exc,
            )
            try:
                known_tags = await service.list_team_tags()
            except Exception:
                known_tags = [team.tag]
            nuevo_nick = apply_team_tag(base_nick, team.tag, known_tags)[:32].rstrip()
        try:
            await target_member.edit(nick=nuevo_nick)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("No se pudo renombrar a '%s': %s", nuevo_nick, exc)
            avisos.append(f"no se pudo renombrar a `{nuevo_nick}`")

        procedencia = f" desde **{previous_team.name}**" if previous_team is not None else ""
        mensaje = (
            f"✅ {target_member.mention} traspasado{procedencia} a {equipo.mention} "
            f"como **{posicion.value}** (`{nuevo_nick}`)."
        )
        if avisos:
            mensaje += "\n⚠️ Plantilla actualizada, pero " + ", ".join(avisos) + "."
        await interaction.followup.send(mensaje, ephemeral=True)

    @app_commands.command(
        name="liberar-jugador",
        description="Saca a un jugador de la plantilla del equipo indicado (Solo Staff)",
    )
    @app_commands.describe(
        equipo="Rol de Discord del equipo del que se libera al jugador",
        usuario="Jugador al que se libera",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def liberar_jugador(
        self,
        interaction: discord.Interaction,
        equipo: discord.Role,
        usuario: discord.Member,
    ) -> None:
        """
        Da de baja al jugador de la plantilla de ese equipo y le retira su rol en Discord.

        La ficha del jugador no se borra. El rol de Libre solo se asigna si tras la baja
        no le queda ninguna otra plantilla: quien siga, por ejemplo, de coach en otro club
        conserva ese rol y no pasa a agente libre.
        """
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord.",
                ephemeral=True,
            )
            return

        is_authorized = await is_staff(interaction.user, self.settings)
        if not is_authorized:
            await interaction.response.send_message(
                "❌ Solo el personal de staff puede liberar jugadores.",
                ephemeral=True,
            )
            return

        service = self.roster_sync_service
        if service is None:
            await interaction.response.send_message(
                "❌ El servicio de sincronización de plantillas no está disponible.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        target_member = await resolve_member(usuario) or usuario

        # 1. Base de datos primero: la baja deja registro de movimiento y auditoría.
        try:
            dado_de_baja = await service.handle_role_removed(
                member=target_member,
                role=equipo,
                actor_id=interaction.user.id,
            )
        except RosterSyncError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            logger.error(
                "Error al liberar a %s del equipo %s: %s",
                getattr(target_member, "display_name", str(target_member)),
                equipo.name,
                exc,
                exc_info=True,
            )
            await interaction.followup.send(
                "❌ Ocurrió un error inesperado al registrar la baja.",
                ephemeral=True,
            )
            return

        if not dado_de_baja:
            await interaction.followup.send(
                f"⚠️ {target_member.mention} no figura en la plantilla de {equipo.mention}, "
                "o ese rol no corresponde a ningún equipo registrado.",
                ephemeral=True,
            )
            return

        avisos: list[str] = []

        # 2. Retirar el rol del equipo en Discord
        if equipo in target_member.roles:
            try:
                await target_member.remove_roles(equipo)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("No se pudo retirar el rol '%s': %s", equipo.name, exc)
                avisos.append(f"no se pudo retirar el rol {equipo.mention}")

        # 3. Solo queda libre quien no conserve ninguna otra plantilla
        restantes = await service.get_user_teams(target_member.id)
        try:
            known_tags = await service.list_team_tags()
        except Exception as exc:
            logger.warning("No se pudieron cargar los tags de equipo: %s", exc)
            known_tags = []

        if restantes:
            # Le queda plantilla: el apodo pasa a llevar el tag del club que prevalece,
            # el primero por orden alfabético cuando hay más de uno.
            equipo_restante = min(restantes, key=lambda par: par[0].name.lower())[0]
            nuevo_nick = apply_team_tag(
                target_member.display_name, equipo_restante.tag, known_tags
            )[:32]
            if nuevo_nick != target_member.display_name:
                try:
                    await target_member.edit(nick=nuevo_nick)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("No se pudo renombrar a '%s': %s", nuevo_nick, exc)
                    avisos.append(f"no se pudo renombrar a `{nuevo_nick}`")

            equipos = ", ".join(f"**{team.name}** ({m.role.value})" for team, m in restantes)
            mensaje = (
                f"✅ {target_member.mention} queda fuera de la plantilla de {equipo.mention}. "
                f"Sigue en {equipos}, así que no pasa a agente libre (`{nuevo_nick}`)."
            )
            if avisos:
                mensaje += "\n⚠️ Plantilla actualizada, pero " + ", ".join(avisos) + "."
            await interaction.followup.send(mensaje, ephemeral=True)
            return

        free_role = discord.utils.get(interaction.guild.roles, name=self.settings.free_role_name)
        if free_role is None:
            avisos.append(f"el rol '{self.settings.free_role_name}' no existe en el servidor")
        elif free_role not in target_member.roles:
            try:
                await target_member.add_roles(free_role)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("No se pudo asignar el rol de agente libre: %s", exc)
                avisos.append(f"no se pudo asignar el rol {free_role.mention}")

        # 4. Apodo sin el tag del equipo
        nuevo_nick = strip_team_tag(target_member.display_name, known_tags)[:32]
        if nuevo_nick != target_member.display_name:
            try:
                await target_member.edit(nick=nuevo_nick)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("No se pudo renombrar a '%s': %s", nuevo_nick, exc)
                avisos.append(f"no se pudo renombrar a `{nuevo_nick}`")

        mensaje = (
            f"✅ {target_member.mention} liberado de {equipo.mention}. "
            f"No le queda ninguna otra plantilla, así que pasa a "
            f"**{self.settings.free_role_name}** (`{nuevo_nick}`)."
        )
        if avisos:
            mensaje += "\n⚠️ Plantilla actualizada, pero " + ", ".join(avisos) + "."
        await interaction.followup.send(mensaje, ephemeral=True)


async def setup(bot: LigaBot | commands.Bot) -> None:
    """Función de carga estándar de extensión de discord.py con guard de idempotencia."""
    if "Roster" not in bot.cogs:
        await bot.add_cog(RosterCog(bot))  # type: ignore[arg-type]


__all__ = [
    "RosterCog",
    "setup",
]
