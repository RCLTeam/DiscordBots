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
from liga_bot.models.enums import RoleRequestStatus
from liga_bot.repositories.role_request_repo import RoleRequestRepository
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.ui.roles import (
    PanelPedirRolView,
    build_panel_rol_embed,
    build_welcome_dm_blocked_embed,
    build_welcome_dm_error_embed,
)
from liga_bot.utils.formatting import apply_team_tag

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


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
        - Fase 1: Registra en transacción atómica de BD la cuenta de juego (players),
          la membresía con su posición y el estado APPROVED; la transacción se confirma (COMMIT).
        - Si la transacción en BD falla o se revierte, nunca se invoca add_roles ni Discord.
        - Fase 2: Tras el commit exitoso en BD, asigna el rol del equipo si existe,
          remueve 'Sin Verificar' y actualiza el apodo del miembro ('<TAG> <NombreLoL>').
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

                # Resolver el equipo en base de datos: su discord_role_id manda sobre el
                # nombre, que puede no coincidir literalmente con el rol de Discord.
                team_repo = TeamRepository(session)
                team = await team_repo.get_by_name(req.equipo)

                role = None
                if team is not None:
                    role = guild.get_role(team.discord_role_id)
                if role is None:
                    role = discord.utils.get(guild.roles, name=req.equipo)

                if role is None:
                    logger.warning(
                        "El rol del equipo '%s' (discord_role_id=%s) no se encontró en el "
                        "servidor %s: revisa el ID sembrado con seed-teams.",
                        req.equipo,
                        team.discord_role_id if team is not None else "desconocido",
                        guild.name,
                    )

                # Persistencia atómica: cuenta de juego, plantilla y estado de la solicitud
                # comparten la sesión, así que un fallo en cualquiera revierte todo el bloque.
                roster_service = getattr(self.bot, "roster_sync_service", None)
                if roster_service is not None:
                    await roster_service.ensure_player(
                        member=member,
                        game_name=req.nombre_lol,
                        riot_tag=req.riot_tag,
                        session=session,
                    )
                    if req.posicion and role is not None:
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

                # Obtener tags conocidos antes del commit para formateo de apodo
                known_tags: list[str] = []
                team_tag = team.tag if team is not None else None
                if team is not None:
                    known_tags = [t.tag for t in await team_repo.list_all()]

                # Capturar variables de estado antes de salir de la sesión transaccional
                target_member = member
                target_role = role
                nombre_lol = req.nombre_lol
                equipo_nombre = req.equipo
                member_display = member.display_name

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
        # Si las llamadas de Discord fallan, se registran, pero la BD ya es consistente y segura.
        if target_role is not None:
            try:
                await target_member.add_roles(target_role)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "No se pudo asignar el rol '%s' a %s: %s",
                    equipo_nombre,
                    member_display,
                    exc,
                )

        # Remover rol 'Sin Verificar' si está presente
        if self.settings.sin_verificar_role_id > 0:
            sin_verificar = discord.utils.get(guild.roles, id=self.settings.sin_verificar_role_id)
            if sin_verificar is None:
                sin_verificar = guild.get_role(self.settings.sin_verificar_role_id)
            if sin_verificar is not None and sin_verificar in target_member.roles:
                try:
                    await target_member.remove_roles(sin_verificar)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(
                        "No se pudo remover rol sin verificar de %s: %s",
                        member_display,
                        exc,
                    )

        # Actualizar apodo: "<TAG> <NombreLoL>", sin el Riot Tag
        nick = nombre_lol
        if team_tag is not None:
            nick = apply_team_tag(nick, team_tag, known_tags)
        else:
            logger.warning(
                "El equipo '%s' no existe en base de datos: no se aplica tag al apodo.",
                equipo_nombre,
            )
        nick = nick[:32]
        try:
            await target_member.edit(nick=nick)
        except discord.Forbidden:
            logger.warning(
                "Sin permisos para cambiar el apodo de %s a '%s': el bot necesita "
                "'Gestionar apodos' y un rol por encima del miembro (los dueños del "
                "servidor nunca pueden ser renombrados).",
                member_display,
                nick,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "No se pudo actualizar el apodo de %s a '%s': %s",
                member_display,
                nick,
                exc,
            )

        return True, f"Rol {equipo_nombre} confirmado para {member_display}."

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
