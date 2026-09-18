"""
Servicio de dominio para el aprovisionamiento y programación de partidos en LigaBot.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from liga_bot.config import Settings, get_settings
from liga_bot.database import get_session_factory, transactional_session
from liga_bot.models.enums import Division, MatchStatus
from liga_bot.models.match import Match
from liga_bot.models.team import Team
from liga_bot.repositories.match_repo import MatchRepository
from liga_bot.repositories.team_repo import TeamRepository
from liga_bot.utils.formatting import (
    format_match_channel_name,
    format_mensaje_1,
    format_mensaje_2,
    normalize_slug,
)

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MatchError(Exception):
    """Excepción y modelo de datos para errores en la creación de partidos."""

    message: str
    row: int | None = None
    team1_name: str | None = None
    team2_name: str | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class MatchResult:
    """Resultado detallado de la creación de un partido."""

    success: bool
    jornada: int
    team1_name: str
    team2_name: str
    match: Match | None = None
    channel: discord.TextChannel | None = None
    channel_mention: str | None = None
    error: str | None = None
    is_duplicate: bool = False


@dataclass(slots=True)
class JornadaResult:
    """Resultado agregado del procesamiento de una jornada completa."""

    jornada: int
    total_rows: int
    matches: list[MatchResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for m in self.matches if m.success)

    @property
    def error_count(self) -> int:
        return len(self.errors)


class ScheduleService:
    """
    Servicio de orquestación de calendario, validación de enfrentamientos y
    aprovisionamiento de canales en Discord.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        bot: commands.Bot | discord.Client | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.settings = settings or get_settings()
        self.bot = bot

    async def _resolve_team(self, team_repo: TeamRepository, name_or_slug: str) -> Team | None:
        """Resuelve un equipo buscando por nombre insensible a mayúsculas o por slug."""
        cleaned = name_or_slug.strip()
        team = await team_repo.get_by_name(cleaned, case_sensitive=False)
        if team is not None:
            return team
        slug_guess = normalize_slug(cleaned)
        return await team_repo.get_by_slug(slug_guess)

    async def create_match(
        self,
        guild: discord.Guild,
        jornada: int,
        team1_name: str,
        team2_name: str,
        scheduled_at: datetime | None = None,
        fecha: str | None = None,
        hora: str | None = None,
    ) -> MatchResult:
        """
        Crea un partido validando equipos y división, aprovisiona la categoría y el canal
        en Discord con permisos estrictos, envía los mensajes y persiste en base de datos.
        """
        # 1. Validación en Base de Datos
        async with transactional_session(self.session_factory) as session:
            team_repo = TeamRepository(session)
            match_repo = MatchRepository(session)

            team1 = await self._resolve_team(team_repo, team1_name)
            if team1 is None:
                return MatchResult(
                    success=False,
                    jornada=jornada,
                    team1_name=team1_name,
                    team2_name=team2_name,
                    error=f"El equipo local '{team1_name}' no está registrado en la base de datos.",
                )

            team2 = await self._resolve_team(team_repo, team2_name)
            if team2 is None:
                return MatchResult(
                    success=False,
                    jornada=jornada,
                    team1_name=team1_name,
                    team2_name=team2_name,
                    error=(
                        f"El equipo visitante '{team2_name}' "
                        "no está registrado en la base de datos."
                    ),
                )

            if team1.id == team2.id:
                return MatchResult(
                    success=False,
                    jornada=jornada,
                    team1_name=team1_name,
                    team2_name=team2_name,
                    error=f"Un equipo no puede enfrentarse a sí mismo ('{team1.name}').",
                )

            if team1.division != team2.division:
                return MatchResult(
                    success=False,
                    jornada=jornada,
                    team1_name=team1_name,
                    team2_name=team2_name,
                    error=(
                        f"Conflicto de división: '{team1.name}' ({team1.division.value}) y "
                        f"'{team2.name}' ({team2.division.value}) pertenecen a "
                        "divisiones diferentes."
                    ),
                )

            existing_match = await match_repo.get_by_jornada_and_teams(
                jornada=jornada,
                team1_id=team1.id,
                team2_id=team2.id,
                exact_order=False,
            )
            if existing_match is not None:
                return MatchResult(
                    success=False,
                    jornada=jornada,
                    team1_name=team1.name,
                    team2_name=team2.name,
                    match=existing_match,
                    is_duplicate=True,
                    error=(
                        f"El partido entre '{team1.name}' y '{team2.name}' "
                        f"ya existe para la jornada {jornada}."
                    ),
                )

            division = team1.division
            team1_role_id = team1.discord_role_id
            team2_role_id = team2.discord_role_id
            team1_slug = team1.slug
            team2_slug = team2.slug
            team1_id = team1.id
            team2_id = team2.id
            t1_name = team1.name
            t2_name = team2.name

        # 2. Resolución de Roles de Discord
        role1 = guild.get_role(team1_role_id)
        if role1 is None:
            return MatchResult(
                success=False,
                jornada=jornada,
                team1_name=t1_name,
                team2_name=t2_name,
                error=(
                    f"El rol de Discord con ID {team1_role_id} para '{t1_name}' "
                    "no existe en el servidor."
                ),
            )

        role2 = guild.get_role(team2_role_id)
        if role2 is None:
            return MatchResult(
                success=False,
                jornada=jornada,
                team1_name=t1_name,
                team2_name=t2_name,
                error=(
                    f"El rol de Discord con ID {team2_role_id} para '{t2_name}' "
                    "no existe en el servidor."
                ),
            )

        # 3. Resolución de Categoría
        category_name = (
            f"PREMIER - JORNADA {jornada}"
            if division == Division.PREMIER
            else f"ASCENSO - JORNADA {jornada}"
        )
        alt_category_name = f"ASCEND - JORNADA {jornada}"

        category = discord.utils.find(
            lambda c: c.name.strip().upper() in (category_name.upper(), alt_category_name.upper()),
            guild.categories,
        )
        if category is None:
            try:
                category = await guild.create_category(category_name)
            except Exception as exc:
                return MatchResult(
                    success=False,
                    jornada=jornada,
                    team1_name=t1_name,
                    team2_name=t2_name,
                    error=f"Error creando la categoría '{category_name}': {exc}",
                )

        # 4. Configuración de Permisos
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role1: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            role2: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        staff_role = guild.get_role(self.settings.staff_role_id)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )

        admin_role = guild.get_role(self.settings.admin_role_id)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )

        # Asignación de rol de CEO por división
        if division == Division.PREMIER:
            ceo_role = guild.get_role(self.settings.ceo_premier_role_id)
        else:
            ceo_role = guild.get_role(self.settings.ceo_ascend_role_id)

        if ceo_role:
            overwrites[ceo_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )

        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True
            )

        # 5. Creación de Canal de Texto
        channel_name = format_match_channel_name(jornada, team1_slug, team2_slug)
        created_channel: discord.TextChannel | None = None

        try:
            created_channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
            )

            # 6. Posteo de Mensajes de Coordinación
            fecha_display = fecha or (
                scheduled_at.strftime("%d/%m/%Y") if scheduled_at else "Por definir"
            )
            hora_display = hora or (
                scheduled_at.strftime("%H:%M") if scheduled_at else "Por definir"
            )

            msg1_text = format_mensaje_1(
                jornada=jornada,
                fecha=fecha_display,
                hora=hora_display,
                equipo1=role1.mention,
                equipo2=role2.mention,
            )
            msg2_text = format_mensaje_2()

            await created_channel.send(
                content=f"{role1.mention} {role2.mention}",
                embed=discord.Embed(description=msg1_text, color=discord.Color.blurple()),
            )
            await created_channel.send(
                embed=discord.Embed(description=msg2_text, color=discord.Color.blurple())
            )

            # 7. Persistencia Atómica en Base de Datos
            async with transactional_session(self.session_factory) as session:
                match_repo = MatchRepository(session)
                match = await match_repo.create(
                    jornada=jornada,
                    division=division,
                    team1_id=team1_id,
                    team2_id=team2_id,
                    scheduled_at=scheduled_at,
                    discord_channel_id=created_channel.id,
                    status=MatchStatus.CANAL_CREADO,
                )

            return MatchResult(
                success=True,
                jornada=jornada,
                team1_name=t1_name,
                team2_name=t2_name,
                match=match,
                channel=created_channel,
                channel_mention=created_channel.mention,
            )

        except Exception as exc:
            # Garantía Anti-Huérfanos: eliminación inmediata del canal
            if created_channel is not None:
                try:
                    await created_channel.delete(reason="Rollback: error de aprovisionamiento")
                except Exception:
                    pass
            logger.error(
                "Error al aprovisionar partido para jornada %s: %s",
                jornada,
                exc,
                exc_info=True,
            )
            return MatchResult(
                success=False,
                jornada=jornada,
                team1_name=t1_name,
                team2_name=t2_name,
                error=f"Error durante el aprovisionamiento: {exc}",
            )

    async def create_single_match(
        self,
        jornada: int,
        team1_name: str,
        team2_name: str,
        scheduled_at: datetime | None,
        guild: discord.Guild,
    ) -> MatchResult:
        """Alias para compatibilidad con PROJECT.md."""
        return await self.create_match(
            guild=guild,
            jornada=jornada,
            team1_name=team1_name,
            team2_name=team2_name,
            scheduled_at=scheduled_at,
        )

    async def create_jornada_from_csv(
        self,
        guild: discord.Guild,
        jornada: int,
        csv_content: str,
    ) -> JornadaResult:
        """Procesa una jornada en lote a partir de texto CSV."""
        cleaned = csv_content.lstrip("\ufeff").strip()
        if not cleaned:
            return JornadaResult(
                jornada=jornada,
                total_rows=0,
                errors=["El contenido del archivo CSV está vacío."],
            )

        first_line = cleaned.splitlines()[0]
        delimiter = ";" if (";" in first_line and "," not in first_line) else ","
        reader = csv.DictReader(io.StringIO(cleaned), delimiter=delimiter)

        if not reader.fieldnames:
            return JornadaResult(
                jornada=jornada,
                total_rows=0,
                errors=["No se detectaron cabeceras válidas en el CSV."],
            )

        # Normalizar cabeceras a minúsculas
        field_map = {f.strip().lower(): f for f in reader.fieldnames if f}
        required_cols = {"equipo1", "equipo2", "fecha", "hora"}
        missing_cols = required_cols - set(field_map.keys())
        if missing_cols:
            missing_sorted = ", ".join(sorted(missing_cols))
            return JornadaResult(
                jornada=jornada,
                total_rows=0,
                errors=[f"Cabeceras incompletas. Faltan las columnas: {missing_sorted}"],
            )

        col_eq1 = field_map["equipo1"]
        col_eq2 = field_map["equipo2"]
        col_fecha = field_map["fecha"]
        col_hora = field_map["hora"]

        results: list[MatchResult] = []
        errors: list[str] = []
        total_rows = 0
        seen_pairs: set[frozenset[str]] = set()

        for idx, row in enumerate(reader, start=2):
            total_rows += 1
            raw1 = (row.get(col_eq1) or "").strip()
            raw2 = (row.get(col_eq2) or "").strip()
            fecha_str = (row.get(col_fecha) or "").strip()
            hora_str = (row.get(col_hora) or "").strip()

            if not raw1 or not raw2:
                errors.append(f"Fila {idx}: faltan nombres de equipos.")
                continue

            pair_key = frozenset({raw1.lower(), raw2.lower()})
            if pair_key in seen_pairs:
                errors.append(
                    f"Fila {idx}: enfrentamiento duplicado dentro del mismo CSV "
                    f"('{raw1}' vs '{raw2}')."
                )
                continue
            seen_pairs.add(pair_key)

            scheduled_dt: datetime | None = None
            if fecha_str and hora_str:
                try:
                    scheduled_dt = datetime.strptime(
                        f"{fecha_str} {hora_str}", "%d/%m/%Y %H:%M"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    scheduled_dt = None

            res = await self.create_match(
                guild=guild,
                jornada=jornada,
                team1_name=raw1,
                team2_name=raw2,
                scheduled_at=scheduled_dt,
                fecha=fecha_str or None,
                hora=hora_str or None,
            )
            results.append(res)
            if not res.success:
                errors.append(f"Fila {idx} ({raw1} vs {raw2}): {res.error}")

        return JornadaResult(
            jornada=jornada,
            total_rows=total_rows,
            matches=results,
            errors=errors,
        )

    async def process_schedule_csv(
        self,
        jornada: int,
        csv_content: str,
        guild: discord.Guild,
    ) -> list[MatchResult]:
        """Alias para compatibilidad con PROJECT.md."""
        j_res = await self.create_jornada_from_csv(
            guild=guild, jornada=jornada, csv_content=csv_content
        )
        return j_res.matches
