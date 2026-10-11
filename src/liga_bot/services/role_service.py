"""
Servicio de dominio para la verificación, asignación y auditoría de roles en LigaBot.

Orquesta:
- Asignación de rol 'Sin Verificar' y envío de mensaje directo de bienvenida en on_member_join.
- Asignación directa del rol de agente libre ('Libre') con actualización de apodo y auditoría.
- Creación de canales de tickets de rol con permisos de sobreescritura seguros.
- Aprobación y confirmación de solicitudes de rol por parte de staff/administración.
- Denegación de solicitudes de rol con registro transaccional en base de datos.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory, transactional_session
from liga_bot.models.enums import RoleRequestStatus, RosterRole
from liga_bot.repositories.role_request_repo import RoleRequestRepository
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.services.roster_sync_service import RosterSyncError
from liga_bot.ui.roles import (
    PanelPedirRolView,
    build_panel_rol_embed,
    build_welcome_dm_blocked_embed,
    build_welcome_dm_error_embed,
)
from liga_bot.utils.formatting import apply_team_tag

if TYPE_CHECKING:
    from discord.ext import commands

    from liga_bot.models.team import Team

logger = logging.getLogger(__name__)


class TeamRoleNotResolvedError(Exception):
    """El equipo pedido no está registrado o su rol de Discord no existe en el servidor."""


class RoleService:
    """
    Servicio de orquestación de lógica de negocio para la verificación de roles de jugadores
    y la gestión del ciclo de vida de las solicitudes asociadas.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        bot: commands.Bot | discord.Client | None = None,
    ) -> None:
        self.session_factory: async_sessionmaker[AsyncSession] = (
            session_factory or get_session_factory()
        )
        self.settings: Settings = settings or get_settings()
        self.bot: commands.Bot | discord.Client | None = bot

    async def handle_member_join(self, member: discord.Member) -> bool:
        """
        Gestiona la incorporación de un nuevo miembro al servidor de Discord:
        - Si settings.sin_verificar_role_id > 0, localiza el rol y lo asigna al miembro.
        - Envía un mensaje directo de bienvenida con instrucciones informativas de verificación.
        - Retorna True si el rol fue asignado o no está configurado, o False ante error.
        """
        if self.settings.sin_verificar_role_id > 0:
            role = discord.utils.get(member.guild.roles, id=self.settings.sin_verificar_role_id)
            if role is None:
                role = member.guild.get_role(self.settings.sin_verificar_role_id)

            if role is None:
                logger.warning(
                    "Rol sin verificar con ID %s no encontrado en el servidor %s.",
                    self.settings.sin_verificar_role_id,
                    member.guild.name,
                )
                return False

            try:
                await member.add_roles(role)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error(
                    "Error al asignar rol sin verificar (%s) al miembro %s: %s",
                    role.name,
                    member.display_name,
                    exc,
                )
                return False
            except Exception as exc:
                logger.exception(
                    "Excepción inesperada al asignar rol sin verificar a %s: %s",
                    member.display_name,
                    exc,
                )
                return False

        # Intentar enviar mensaje directo informativo de bienvenida
        await self.send_welcome_dm(member)

        return True

    async def send_welcome_dm(self, member: discord.Member) -> bool:
        """
        Intenta enviar el mensaje directo de bienvenida con el panel de solicitud de rol.

        Si la entrega falla:
        - Si es por DMs cerrados o bloqueados (discord.Forbidden o código 50007),
          envía una alerta dorada de advertencia a #moderators-only.
        - Si es por una excepción inesperada (HTTPException distinta, error de red, etc.),
          envía una alerta roja con el error en bloque de código a #moderators-only.

        El envío de alertas al canal de moderación cuenta con manejo defensivo para que
        ningún fallo bloquee o interrumpa la incorporación del usuario.

        Retorna True si el DM se envió exitosamente, o False si falló la entrega.
        """
        try:
            guild = getattr(member, "guild", None)
            guild_name = getattr(guild, "name", None) or "la liga"
            welcome_msg = (
                f"¡Bienvenido/a a **{guild_name}**!\n\n"
                "Para acceder a los canales de la liga y registrarte en un equipo, "
                "solicita tu rol con el botón de abajo, desde el panel del servidor "
                "o con el comando `/pedir-rol`."
            )
            await member.send(
                welcome_msg,
                embed=build_panel_rol_embed(),
                view=PanelPedirRolView(),
            )
            return True
        except Exception as exc:
            is_blocked_dm = (
                isinstance(exc, discord.Forbidden) or getattr(exc, "code", None) == 50007
            )
            if is_blocked_dm:
                logger.info(
                    "No se pudo enviar DM de bienvenida a %s (DMs cerrados o bloqueados): %s",
                    member.display_name,
                    exc,
                )
                alert_embed = build_welcome_dm_blocked_embed(member)
            else:
                logger.warning(
                    "Excepción inesperada al enviar mensaje directo de bienvenida a %s: %s",
                    member.display_name,
                    exc,
                )
                alert_embed = build_welcome_dm_error_embed(member, exc)

            await self._notify_moderators_channel(member, alert_embed)
            return False

    async def _notify_moderators_channel(
        self, member: discord.Member, embed: discord.Embed
    ) -> None:
        """
        Envía un embed de alerta al canal de moderadores configurado en settings.
        Defensivo: captura cualquier excepción y registra en logs para
        garantizar que nunca propague un error ni interrumpa el flujo del bot.
        """
        channel_id = self.settings.moderators_channel_id
        if not channel_id or channel_id <= 0:
            logger.debug("moderators_channel_id no configurado (> 0); se omite alerta.")
            return

        try:
            channel = None
            guild = getattr(member, "guild", None)
            if guild is not None:
                if hasattr(guild, "get_channel") and callable(guild.get_channel):
                    channel = guild.get_channel(channel_id)
                if (
                    channel is None
                    and hasattr(guild, "fetch_channel")
                    and callable(guild.fetch_channel)
                ):
                    try:
                        channel = await guild.fetch_channel(channel_id)
                    except Exception as fetch_exc:
                        logger.debug(
                            "No se pudo obtener canal %s vía guild.fetch_channel: %s",
                            channel_id,
                            fetch_exc,
                        )

            if channel is None and self.bot is not None:
                if hasattr(self.bot, "get_channel") and callable(self.bot.get_channel):
                    channel = self.bot.get_channel(channel_id)
                if (
                    channel is None
                    and hasattr(self.bot, "fetch_channel")
                    and callable(self.bot.fetch_channel)
                ):
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except Exception as bot_fetch_exc:
                        logger.debug(
                            "No se pudo obtener canal %s vía bot.fetch_channel: %s",
                            channel_id,
                            bot_fetch_exc,
                        )

            if channel is not None and hasattr(channel, "send") and callable(channel.send):
                await channel.send(embed=embed)
                logger.info(
                    "Alerta de DM de bienvenida enviada a moderadores (%s) para %s.",
                    channel_id,
                    member.display_name,
                )
            else:
                logger.warning(
                    "Canal de moderadores (%s) no encontrado o no válido para enviar alerta.",
                    channel_id,
                )
        except Exception as alert_exc:
            logger.error(
                "Fallo defensivo al enviar alerta de moderación al canal %s: %s",
                channel_id,
                alert_exc,
            )

    async def list_team_names(self) -> list[str]:
        """Nombres de los equipos registrados en base de datos (división, nombre)."""
        async with transactional_session(self.session_factory) as session:
            teams = await TeamRepository(session).list_all()
            return [team.name for team in teams]

    async def _ensure_player(
        self,
        member: discord.Member,
        nombre_lol: str,
        riot_tag: str,
        session: AsyncSession | None = None,
    ) -> bool:
        """
        Registra la cuenta de juego del miembro en la tabla players.

        Se invoca en cuanto se conoce el nombre de invocador (solicitud de ticket o
        asignación directa de Libre), no al confirmar: así el jugador queda registrado
        aunque nunca llegue a entrar en un equipo.
        Retorna False si el servicio de plantillas no está disponible.
        """
        roster_service = getattr(self.bot, "roster_sync_service", None)
        if roster_service is None:
            logger.warning(
                "RosterSyncService no disponible: no se registra la cuenta de juego de %s.",
                member.display_name,
            )
            return False
        await roster_service.ensure_player(
            member=member,
            game_name=nombre_lol,
            riot_tag=riot_tag,
            session=session,
        )
        return True

    async def assign_free_role(
        self,
        member: discord.Member,
        nombre_lol: str,
        riot_tag: str,
    ) -> tuple[bool, str]:
        """
        Asigna directamente el rol de agente libre al miembro:
        - Busca el rol con nombre settings.free_role_name (por defecto 'Libre') en el servidor.
        - Remueve el rol 'Sin Verificar' si el miembro lo posee.
        - Añade el rol de agente libre al miembro.
        - Actualiza el apodo del miembro con su nombre de invocador (máximo 32 caracteres).
        - Registra la solicitud con estado APPROVED en BD para trazabilidad y auditoría.
        - Retorna (True, 'Rol Libre asignado correctamente.') o tupla con error descriptivo.
        """
        free_role = discord.utils.get(member.guild.roles, name=self.settings.free_role_name)
        if free_role is None:
            return False, f"El rol '{self.settings.free_role_name}' no existe en el servidor."

        # Remover rol 'Sin Verificar' si está presente
        if self.settings.sin_verificar_role_id > 0:
            sin_verificar = discord.utils.get(
                member.guild.roles, id=self.settings.sin_verificar_role_id
            )
            if sin_verificar is None:
                sin_verificar = member.guild.get_role(self.settings.sin_verificar_role_id)
            if sin_verificar is not None and sin_verificar in member.roles:
                try:
                    await member.remove_roles(sin_verificar)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(
                        "No se pudo remover el rol sin verificar de %s: %s",
                        member.display_name,
                        exc,
                    )

        # Añadir rol de agente libre
        try:
            await member.add_roles(free_role)
        except discord.Forbidden:
            return (
                False,
                f"Permisos insuficientes para asignar el rol '{self.settings.free_role_name}'.",
            )
        except discord.HTTPException as exc:
            return False, f"Error al asignar rol '{self.settings.free_role_name}': {exc}"

        # Actualizar apodo en Discord (hasta 32 caracteres)
        nick = nombre_lol[:32]
        try:
            await member.edit(nick=nick)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "No se pudo actualizar el apodo de %s a '%s': %s",
                member.display_name,
                nick,
                exc,
            )

        # Registrar solicitud aprobada en base de datos para trazabilidad
        try:
            async with transactional_session(self.session_factory) as session:
                await self._ensure_player(
                    member=member,
                    nombre_lol=nombre_lol,
                    riot_tag=riot_tag,
                    session=session,
                )
                repo = RoleRequestRepository(session)
                req = await repo.create_request(
                    user_id=member.id,
                    nombre_lol=nombre_lol,
                    riot_tag=riot_tag,
                    equipo=self.settings.free_role_name,
                    canal_id=None,
                )
                await repo.update_status(req.id, RoleRequestStatus.APPROVED)
        except Exception as exc:
            logger.error(
                "Error al registrar solicitud de rol Libre en base de datos para %s: %s",
                member.display_name,
                exc,
            )
            return False, "Error al registrar la asignación en la base de datos."

        return True, f"Rol {self.settings.free_role_name} asignado correctamente."

    async def create_role_request_ticket(
        self,
        guild: discord.Guild,
        member: discord.Member,
        nombre_lol: str,
        riot_tag: str,
        equipo: str,
        posicion: str | None = None,
    ) -> tuple[bool, str, discord.TextChannel | None]:
        """
        Crea un canal privado de ticket para solicitud de rol:
        - Verifica que el usuario no tenga ya una solicitud activa en estado PENDING.
        - Construye el diccionario de sobreescritura de permisos (PermissionOverwrite) restringido:
          - default_role: sin lectura ni envío.
          - member: lectura, envío y adjuntos.
          - staff_role_id: lectura y envío (si está configurado).
          - ceo_role_id: lectura y envío (si está configurado).
          - guild.me: lectura, envío y gestión de canales.
        - Ubica la categoría de tickets de rol si está configurada.
        - Crea el canal de texto con nombre 'rol-{member.name}' (máx 32 caracteres en minúsculas).
        - Persiste la solicitud RoleRequest en base de datos vinculada a canal_id.
        - Si la persistencia en BD falla, elimina el canal recién creado para evitar huérfanos.
        - Retorna (True, 'Canal de solicitud creado correctamente.', channel) o tupla con error.
        """
        # 1. Verificar si el usuario ya posee una solicitud pendiente activa
        async with transactional_session(self.session_factory) as session:
            repo = RoleRequestRepository(session)
            active = await repo.get_active_by_user(member.id)
            if active is not None:
                return False, "Ya tienes una solicitud de rol pendiente.", None

        # 1.b Registrar la cuenta de juego antes de tocar Discord: si falla, no queda
        #     ni canal ni solicitud huérfanos.
        try:
            await self._ensure_player(member=member, nombre_lol=nombre_lol, riot_tag=riot_tag)
        except Exception as exc:
            logger.error(
                "Error al registrar la cuenta de juego de %s: %s", member.display_name, exc
            )
            return False, "No se pudo registrar tu cuenta de juego en la base de datos.", None

        # 2. Construir sobreescritura de permisos
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                read_messages=False, send_messages=False
            ),
            member: discord.PermissionOverwrite(
                read_messages=True, send_messages=True, attach_files=True
            ),
        }

        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_channels=True
            )

        if self.settings.staff_role_id > 0:
            staff_role = discord.utils.get(guild.roles, id=self.settings.staff_role_id)
            if staff_role is None:
                staff_role = guild.get_role(self.settings.staff_role_id)
            if staff_role is not None:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True
                )

        if self.settings.ceo_role_id > 0:
            ceo_role = discord.utils.get(guild.roles, id=self.settings.ceo_role_id)
            if ceo_role is None:
                ceo_role = guild.get_role(self.settings.ceo_role_id)
            if ceo_role is not None:
                overwrites[ceo_role] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True
                )

        # 3. Categoría de canales de tickets de rol
        category: discord.CategoryChannel | None = None
        if self.settings.ticket_rol_category_id > 0:
            found_cat = guild.get_channel(self.settings.ticket_rol_category_id)
            if isinstance(found_cat, discord.CategoryChannel):
                category = found_cat

        # 4. Crear canal de texto en Discord
        channel_name = f"rol-{member.name}".lower()[:32]
        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                category=category,
            )
        except discord.Forbidden:
            return False, "Permisos insuficientes para crear el canal de solicitud.", None
        except discord.HTTPException as exc:
            return False, f"Error al crear el canal de solicitud: {exc}", None

        # 5. Persistir RoleRequest en base de datos con canal_id
        try:
            async with transactional_session(self.session_factory) as session:
                repo = RoleRequestRepository(session)
                await repo.create_request(
                    user_id=member.id,
                    nombre_lol=nombre_lol,
                    riot_tag=riot_tag,
                    equipo=equipo,
                    canal_id=channel.id,
                    posicion=posicion,
                )
        except Exception as exc:
            logger.error(
                "Error al registrar solicitud de rol en base de datos para canal %s: %s",
                channel.id,
                exc,
            )
            try:
                await channel.delete(reason="Error al registrar solicitud de rol en base de datos")
            except Exception as del_exc:
                logger.warning("No se pudo eliminar canal huérfano %s: %s", channel.id, del_exc)
            return False, "Error al registrar la solicitud en base de datos.", None

        return True, "Canal de solicitud creado correctamente.", channel

    async def discard_role_request_ticket(self, channel: discord.abc.GuildChannel) -> None:
        """
        Deshace un ticket cuyo mensaje con los botones no se pudo publicar:
        - Elimina la solicitud PENDING asociada al canal, para que el jugador pueda
          volver a pedir rol.
        - Elimina el canal del ticket.
        Defensivo: registra los fallos en el log sin propagarlos. La cuenta de juego
        registrada al abrir el ticket se conserva.
        """
        try:
            async with transactional_session(self.session_factory) as session:
                repo = RoleRequestRepository(session)
                req = await repo.get_by_channel_id(channel.id)
                if req is not None and req.estado == RoleRequestStatus.PENDING:
                    await repo.delete(req)
        except Exception as exc:
            logger.error(
                "No se pudo eliminar la solicitud pendiente del ticket %s: %s",
                channel.id,
                exc,
            )

        try:
            await channel.delete(reason="No se pudo publicar el mensaje del ticket de rol")
        except Exception as exc:
            logger.warning("No se pudo eliminar el canal huérfano %s: %s", channel.id, exc)

    async def _resolve_team_role(
        self,
        session: AsyncSession,
        guild: discord.Guild,
        equipo: str,
    ) -> tuple[Team, discord.Role]:
        """
        Resuelve el equipo registrado y su rol de Discord a partir del nombre pedido.

        Solo se acepta el rol cuyo ID es el discord_role_id del equipo registrado: un rol
        del servidor que se llame igual no basta. Lanza TeamRoleNotResolvedError con un
        mensaje para el staff si el equipo no está registrado o si su rol no existe.
        """
        team = await TeamRepository(session).get_by_name(equipo)
        if team is None:
            logger.warning(
                "El equipo '%s' no está registrado en base de datos (servidor %s).",
                equipo,
                guild.name,
            )
            raise TeamRoleNotResolvedError(
                f"El equipo '{equipo}' no está registrado en la base de datos. "
                "Regístralo (por ejemplo, con `liga-cli seed-teams`) y vuelve a intentarlo."
            )

        role = guild.get_role(team.discord_role_id)
        if role is None:
            logger.warning(
                "El rol del equipo '%s' (discord_role_id=%s) no se encontró en el "
                "servidor %s: revisa el ID sembrado con seed-teams.",
                team.name,
                team.discord_role_id,
                guild.name,
            )
            raise TeamRoleNotResolvedError(
                f"El rol del equipo '{team.name}' (discord_role_id {team.discord_role_id}) "
                "no existe en el servidor. Corrige el ID registrado y vuelve a intentarlo."
            )

        return team, role

    async def _apply_team_role_in_discord(
        self,
        guild: discord.Guild,
        member: discord.Member,
        role: discord.Role,
        nombre_lol: str,
        team_tag: str,
        known_tags: list[str],
        nick: str | None = None,
    ) -> list[str]:
        """
        Aplica en Discord el alta en un equipo, una vez confirmada la base de datos:
        - Asigna el rol del equipo.
        - Remueve el rol 'Sin Verificar' si el miembro lo tiene.
        - Pone el apodo canónico provisto o '<TAG> <NombreLoL>' (máximo 32 caracteres).
        Ningún fallo interrumpe los pasos siguientes. Retorna los avisos para el staff
        de lo que no se pudo aplicar.
        """
        warnings: list[str] = []
        member_display = member.display_name

        try:
            await member.add_roles(role)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "No se pudo asignar el rol '%s' a %s: %s",
                role.name,
                member_display,
                exc,
            )
            warnings.append(
                f"No se pudo asignar el rol '{role.name}' en Discord ({exc}); asígnalo a mano."
            )

        if self.settings.sin_verificar_role_id > 0:
            sin_verificar = discord.utils.get(guild.roles, id=self.settings.sin_verificar_role_id)
            if sin_verificar is None:
                sin_verificar = guild.get_role(self.settings.sin_verificar_role_id)
            if sin_verificar is not None and sin_verificar in member.roles:
                try:
                    await member.remove_roles(sin_verificar)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(
                        "No se pudo remover rol sin verificar de %s: %s",
                        member_display,
                        exc,
                    )

        target_nick = (
            nick if nick is not None else apply_team_tag(nombre_lol, team_tag, known_tags)[:32]
        )
        try:
            await member.edit(nick=target_nick)
        except discord.Forbidden:
            logger.warning(
                "Sin permisos para cambiar el apodo de %s a '%s': el bot necesita "
                "'Gestionar apodos' y un rol por encima del miembro (los dueños del "
                "servidor nunca pueden ser renombrados).",
                member_display,
                target_nick,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "No se pudo actualizar el apodo de %s a '%s': %s",
                member_display,
                target_nick,
                exc,
            )

        return warnings

    async def confirm_role_request(
        self,
        guild: discord.Guild,
        channel_id: int,
        staff_member: discord.Member,
    ) -> tuple[bool, str]:
        """
        Confirma y aprueba una solicitud de rol pendiente asociada a un canal de ticket:
        - Localiza la solicitud en estado PENDING correspondiente al channel_id.
        - Resuelve el miembro solicitante en el servidor (caché o API).
        - Resuelve el equipo registrado y su rol por discord_role_id. Si el equipo no está
          registrado o su rol no existe en el servidor, devuelve la causa al staff y la
          solicitud sigue PENDING (se puede volver a confirmar tras corregir el registro).
        - Fase 1: Registra en transacción atómica de BD la membresía con su posición y el
          estado APPROVED; la transacción se confirma (COMMIT). Un RosterSyncError revierte
          la transacción y su mensaje llega al staff.
        - Si la transacción en BD falla o se revierte, nunca se invoca add_roles ni Discord.
        - Fase 2: Tras el commit exitoso en BD, asigna el rol del equipo, remueve
          'Sin Verificar' y actualiza el apodo del miembro ('<TAG> <NombreLoL>'). Si Discord
          rechaza el rol, el mensaje de retorno lo avisa.
        - Retorna (True, f"Rol {req.equipo} confirmado para {member.display_name}.") o error.
        """
        try:
            async with transactional_session(self.session_factory) as session:
                repo = RoleRequestRepository(session)
                req = await repo.get_by_channel_id(channel_id)
                if req is None or req.estado != RoleRequestStatus.PENDING:
                    return False, "No hay solicitud pendiente asociada a este canal."

                if staff_member.id == req.user_id:
                    return False, "No puedes confirmar ni denegar tu propia solicitud de rol."

                # Resolver miembro solicitante en el servidor
                member = guild.get_member(req.user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(req.user_id)
                    except (discord.NotFound, discord.HTTPException):
                        member = None

                if member is None:
                    return False, "El usuario solicitante no se encuentra en el servidor."

                # Resolver el equipo registrado y su rol por discord_role_id. Si falla,
                # la solicitud sigue pendiente y el staff ve la causa.
                team, role = await self._resolve_team_role(session, guild, req.equipo)

                # Persistencia atómica: plantilla y estado de la solicitud comparten la
                # sesión, así que un fallo en cualquiera revierte todo el bloque. La cuenta
                # de juego ya quedó registrada al abrirse el ticket.
                roster_service = getattr(self.bot, "roster_sync_service", None)
                if roster_service is not None:
                    if req.posicion:
                        await roster_service.transfer_player(
                            member=member,
                            team_role=role,
                            new_position=req.posicion,
                            actor_id=staff_member.id,
                            session=session,
                        )
                else:
                    logger.warning(
                        "RosterSyncService no disponible: no se registra la plantilla de %s.",
                        member.display_name,
                    )

                await repo.update_status(
                    request_id=req.id,
                    estado=RoleRequestStatus.APPROVED,
                    staff_id=staff_member.id,
                )

                team_repo = TeamRepository(session)
                nick_to_apply = req.nombre_lol[:32]
                if roster_service is not None:
                    nick_to_apply = await roster_service.resolve_canonical_nick(
                        discord_user_id=member.id,
                        base_name=req.nombre_lol,
                        session=session,
                    )
                else:
                    fallback_known_tags: list[str] = []
                    team_tag_val = team.tag if team is not None else None
                    if team is not None:
                        fallback_known_tags = [t.tag for t in await team_repo.list_all()]
                    if team_tag_val is not None:
                        nick_to_apply = apply_team_tag(
                            req.nombre_lol, team_tag_val, fallback_known_tags
                        )[:32].rstrip()

                # Obtener tags conocidos antes del commit para formateo de apodo
                known_tags = [t.tag for t in await team_repo.list_all()]

                # Capturar variables de estado antes de salir de la sesión transaccional
                team_tag = team.tag
                nombre_lol = req.nombre_lol
                equipo_nombre = req.equipo
                member_display = member.display_name

        except (TeamRoleNotResolvedError, RosterSyncError) as exc:
            logger.warning(
                "No se confirma la solicitud del canal %s; transacción revertida: %s",
                channel_id,
                exc,
            )
            return False, f"{exc} La solicitud sigue pendiente."
        except Exception as exc:
            logger.error(
                "Error al confirmar la solicitud del canal %s; transacción revertida: %s",
                channel_id,
                exc,
                exc_info=True,
            )
            return (
                False,
                "No se pudo registrar la confirmación en base de datos. "
                "No se ha aplicado ningún cambio.",
            )

        # A partir de aquí, cambios en Discord: la base de datos YA se ha confirmado (COMMIT).
        warnings = await self._apply_team_role_in_discord(
            guild=guild,
            member=member,
            role=role,
            nombre_lol=nombre_lol,
            team_tag=team_tag,
            known_tags=known_tags,
            nick=nick_to_apply,
        )

        message = f"Rol {equipo_nombre} confirmado para {member_display}."
        if warnings:
            message = " ".join([message, *warnings])
        return True, message

    async def assign_team_role(
        self,
        guild: discord.Guild,
        member: discord.Member,
        staff_member: discord.Member,
        equipo: str,
        nombre_lol: str,
        riot_tag: str,
        posicion: str | None,
    ) -> tuple[bool, str]:
        """
        Asigna directamente a un jugador a un equipo registrado (comando /asignar-rol),
        dejando lo mismo que confirmar un ticket con ese equipo y posición:
        - Rechaza que el staff se asigne un rol a sí mismo.
        - Exige una posición de plantilla válida (RosterRole).
        - Resuelve el equipo registrado y su rol por discord_role_id; cualquier otro valor
          se rechaza sin tocar Discord.
        - Fase 1 (una transacción): registra la cuenta de juego, la membresía con la
          posición y la solicitud APPROVED con el staff_id. Si falla, no se toca Discord.
        - Fase 2: asigna el rol, remueve 'Sin Verificar' y pone el apodo '<TAG> <NombreLoL>'.
        - Retorna (True, mensaje) o (False, causa).
        """
        if staff_member.id == member.id:
            return False, "No puedes asignarte un rol a ti mismo."

        if not posicion:
            return False, "Indica la posición del jugador en el equipo."
        try:
            position = RosterRole(posicion)
        except ValueError:
            return False, f"La posición '{posicion}' no es válida."

        roster_service = getattr(self.bot, "roster_sync_service", None)
        if roster_service is None:
            logger.warning(
                "RosterSyncService no disponible: no se asigna el equipo '%s' a %s.",
                equipo,
                member.display_name,
            )
            return (
                False,
                "El servicio de plantillas no está disponible. No se ha aplicado ningún cambio.",
            )

        try:
            async with transactional_session(self.session_factory) as session:
                team, role = await self._resolve_team_role(session, guild, equipo)

                await self._ensure_player(
                    member=member,
                    nombre_lol=nombre_lol,
                    riot_tag=riot_tag,
                    session=session,
                )
                await roster_service.transfer_player(
                    member=member,
                    team_role=role,
                    new_position=position,
                    actor_id=staff_member.id,
                    session=session,
                )

                repo = RoleRequestRepository(session)
                req = await repo.create_request(
                    user_id=member.id,
                    nombre_lol=nombre_lol,
                    riot_tag=riot_tag,
                    equipo=team.name,
                    canal_id=None,
                    posicion=position.value,
                )
                await repo.update_status(
                    request_id=req.id,
                    estado=RoleRequestStatus.APPROVED,
                    staff_id=staff_member.id,
                )

                known_tags = [t.tag for t in await TeamRepository(session).list_all()]
                team_name = team.name
                team_tag = team.tag

        except (TeamRoleNotResolvedError, RosterSyncError) as exc:
            logger.warning(
                "No se asigna el equipo '%s' a %s; transacción revertida: %s",
                equipo,
                member.display_name,
                exc,
            )
            return False, str(exc)
        except Exception as exc:
            logger.error(
                "Error al registrar la asignación directa de '%s' a %s; transacción revertida: %s",
                equipo,
                member.display_name,
                exc,
                exc_info=True,
            )
            return (
                False,
                "No se pudo registrar la asignación en base de datos. "
                "No se ha aplicado ningún cambio.",
            )

        warnings = await self._apply_team_role_in_discord(
            guild=guild,
            member=member,
            role=role,
            nombre_lol=nombre_lol,
            team_tag=team_tag,
            known_tags=known_tags,
        )

        message = f"Rol {team_name} asignado a {member.display_name} ({position.value})."
        if warnings:
            message = " ".join([message, *warnings])
        return True, message

    async def deny_role_request(
        self,
        guild: discord.Guild,
        channel_id: int,
        staff_member: discord.Member,
    ) -> tuple[bool, str]:
        """
        Deniega una solicitud de rol asociada a un canal de ticket:
        - Localiza la solicitud en estado PENDING asociada al channel_id.
        - Actualiza el estado a DENIED registrando el staff_id en base de datos.
        - Retorna (True, 'Solicitud de rol denegada.') o tupla con mensaje de error.
        """
        logger.info(
            "Denegando solicitud de rol en canal %s del servidor %s por staff %s (ID %s).",
            channel_id,
            guild.name,
            staff_member.display_name,
            staff_member.id,
        )

        async with transactional_session(self.session_factory) as session:
            repo = RoleRequestRepository(session)
            req = await repo.get_by_channel_id(channel_id)
            if req is None or req.estado != RoleRequestStatus.PENDING:
                return False, "No hay solicitud pendiente asociada a este canal."

            if staff_member.id == req.user_id:
                return False, "No puedes confirmar ni denegar tu propia solicitud de rol."

            await repo.update_status(
                request_id=req.id,
                estado=RoleRequestStatus.DENIED,
                staff_id=staff_member.id,
            )

        return True, "Solicitud de rol denegada."


__all__ = [
    "RoleService",
]
