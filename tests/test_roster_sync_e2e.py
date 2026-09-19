"""
Suite de pruebas de integración End-to-End (E2E) para el subsistema de sincronización
de plantillas y gestión de posiciones con RCL-Next (Milestone 6).

Valida de extremo a extremo contra PostgreSQL real (PGlite en memoria):
1. Escenario 1: Ciclo de vida completo de Discord (8 pasos):
   - Sembrado de 2 equipos en PGlite con discord_role_id asociados.
   - Paso 1: Asignación de rol de Team A -> on_member_update -> alta en BD (STAFF),
     movimiento (JOINED) y auditoría. Roles no retirados en Discord.
   - Paso 2: Asignación de rol de Team B -> on_member_update -> alta en BD (STAFF).
     Usuario pertenece a ambos equipos simultáneamente en BD y Discord.
   - Paso 3: Staff ejecuta /gestionar-posicion -> get_user_teams retorna ambos equipos
     -> GestionarPosicionView instanciada con selectores habilitados.
   - Paso 4: Staff selecciona Team A y asigna rol 'MID' -> persistencia en BD,
     movimiento 'role_changed' y audit_logs con snapshots before/after.
   - Paso 5: Staff selecciona Team B e intenta asignar 'TOP' -> CompetitivePositionConflictError
     capturado, embed de conflicto enviado y BD intacta.
   - Paso 6: Staff selecciona Team B y asigna 'COACH' -> éxito con rol no competitivo.
     Usuario en ambos equipos (MID en Team A, COACH en Team B).
   - Paso 7: Staff promociona a capitán en Team A -> éxito, movimiento 'promoted_to_captain'.
   - Paso 8: Retirada de rol de Team A en Discord -> on_member_update -> baja en BD de Team A,
     movimiento 'left' y auditoría. Membresía de Team B intacta.
2. Escenario 2: Operaciones concurrentes y rápidas:
   - 2A: Adición y remoción secuencial rápida de rol antes del siguiente tick.
     Cero membresías residuales en BD, exactamente 2 movimientos (JOINED, LEFT),
     2 registros de auditoría, sin retirada de roles en Discord.
   - 2B: Transferencia de rol competitivo y desbloqueo dinámico entre equipos.
   - 2C: Resiliencia ante carreras de operaciones concurrentes con asyncio.gather.
3. Escenario 3: Verificación completa de pista de auditoría y JSONB:
   - Integridad de claves foráneas y tipos UUID.
   - Correspondencia biyectiva 1:1 entre movimientos y registros de auditoría.
   - Fidelidad de esquemas JSONB before y after en transiciones de estado.
4. Escenarios auxiliares E2E:
   - Rechazo de comando /gestionar-posicion para usuarios no autorizados (0 mutaciones en BD).
   - Manejo de comando para miembros sin equipos registrados (embed informativo sin vista).
   - Actualización de roles ajenos a clubes ignorada (no-op en BD).
   - Descenso de capitanía y registro de acción demoted_from_captain.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.bot import LigaBot
from liga_bot.cogs.roster import RosterCog
from liga_bot.config import Settings
from liga_bot.models.enums import Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import (
    AuditLog,
    DiscordUser,
    RosterMovement,
    Team,
)
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
)
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    RosterSyncService,
)
from liga_bot.ui.roster import GestionarPosicionView

# ---------------------------------------------------------------------------
# Mocks Auxiliares de Discord
# ---------------------------------------------------------------------------


def make_mock_role(role_id: int, name: str) -> MagicMock:
    """Crea un mock estricto de discord.Role con atributos esperados."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_mock_member(
    user_id: int,
    name: str = "TestPlayer",
    roles: list[discord.Role] | None = None,
    is_admin: bool = False,
    can_manage_guild: bool = False,
) -> MagicMock:
    """Crea un mock reactivo de discord.Member con seguimiento de roles y permisos."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.global_name = f"{name} Global"
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])

    perms = MagicMock(spec=discord.Permissions)
    perms.administrator = is_admin
    perms.manage_guild = can_manage_guild or is_admin
    member.guild_permissions = perms

    avatar = MagicMock()
    avatar.key = f"key_{user_id}"
    avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/avatar.png"
    member.avatar = avatar
    member.display_avatar = avatar

    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    return member


def make_mock_guild(
    guild_id: int = 1547725310508667010,
    name: str = "RCL Official League",
) -> MagicMock:
    """Crea un mock de discord.Guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    guild.roles = []
    guild.get_role = MagicMock(return_value=None)
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    return guild


def make_mock_interaction(
    user: discord.Member | None = None,
    guild: discord.Guild | None | object = ...,
) -> MagicMock:
    """Crea un mock reactivo de discord.Interaction con seguimiento de response.is_done."""
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user or make_mock_member(user_id=999000, name="staff_actor")
    inter.guild = make_mock_guild() if guild is ... else guild
    inter.channel = MagicMock(spec=discord.TextChannel)

    is_done_flag = False

    def is_done_func() -> bool:
        return is_done_flag

    async def mock_defer(*args: Any, **kwargs: Any) -> None:
        nonlocal is_done_flag
        is_done_flag = True

    async def mock_send_message(*args: Any, **kwargs: Any) -> None:
        nonlocal is_done_flag
        is_done_flag = True

    async def mock_edit_message(*args: Any, **kwargs: Any) -> None:
        nonlocal is_done_flag
        is_done_flag = True

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(side_effect=is_done_func)
    response.defer = AsyncMock(side_effect=mock_defer)
    response.send_message = AsyncMock(side_effect=mock_send_message)
    response.edit_message = AsyncMock(side_effect=mock_edit_message)
    inter.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    followup.edit_message = AsyncMock()
    inter.followup = followup
    inter.edit_original_response = AsyncMock()

    return inter


# ---------------------------------------------------------------------------
# Fixtures de Persistencia PGlite y Servicios
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_settings() -> Settings:
    """Configuración de pruebas con roles de guild y staff definidos."""
    return Settings(
        guild_id=1547725310508667010,
        staff_role_id=1547729760384319518,
        ceo_role_id=1547729760384319519,
    )


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Generador de sesiones asíncronas sobre la base de datos PGlite migrada."""
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def clean_roster_tables(migrated_db: AsyncEngine) -> AsyncGenerator[None, None]:
    """
    Limpia las tablas compartidas antes y después de cada prueba
    y asegura un lock del event loop actual para PGlite.
    """
    from liga_bot.database import _engine_locks

    _engine_locks[migrated_db] = asyncio.Lock()
    async with migrated_db.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                "players, teams, discord_users CASCADE;"
            )
        )
        await conn.commit()
    yield
    _engine_locks[migrated_db] = asyncio.Lock()
    async with migrated_db.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                "players, teams, discord_users CASCADE;"
            )
        )
        await conn.commit()


@pytest.fixture
def roster_sync_service(
    session_factory: async_sessionmaker[AsyncSession],
    clean_settings: Settings,
) -> RosterSyncService:
    """Instancia real de RosterSyncService conectada a PGlite."""
    return RosterSyncService(session_factory=session_factory, settings=clean_settings)


@pytest.fixture
def mock_bot(
    clean_settings: Settings,
    roster_sync_service: RosterSyncService,
) -> MagicMock:
    """Mock de LigaBot con inyección de configuración y servicio."""
    bot = MagicMock(spec=LigaBot)
    bot.settings = clean_settings
    bot.roster_sync_service = roster_sync_service
    bot.cogs = {}
    bot.add_cog = AsyncMock()
    return bot


@pytest.fixture
def roster_cog(
    mock_bot: MagicMock,
    clean_settings: Settings,
) -> RosterCog:
    """Instancia real de RosterCog acoplada al bot y configuración."""
    return RosterCog(bot=mock_bot, settings=clean_settings)


@pytest_asyncio.fixture
async def seed_two_teams(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Team, Team]:
    """Siembra dos equipos oficiales en la base de datos PGlite."""
    async with session_factory() as session:
        team_a = Team(
            name="Planar Shock Pingus",
            tag="PSP",
            slug="planar-shock-pingus",
            division=Division.PREMIER,
            discord_role_id=7001,
        )
        team_b = Team(
            name="Void Invaders",
            tag="VOID",
            slug="void-invaders",
            division=Division.PREMIER,
            discord_role_id=7002,
        )
        session.add_all([team_a, team_b])
        await session.commit()
        await session.refresh(team_a)
        await session.refresh(team_b)
        return team_a, team_b


# ===========================================================================
# 1. Escenario 1: Ciclo de Vida Completo de Discord (8 Pasos)
# ===========================================================================


class TestRosterSyncFullLifecycleE2E:
    """Pruebas E2E del flujo completo de 8 pasos del ciclo de vida en Discord."""

    @pytest.mark.asyncio
    async def test_full_discord_lifecycle_e2e(
        self,
        roster_cog: RosterCog,
        roster_sync_service: RosterSyncService,
        seed_two_teams: tuple[Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
    ) -> None:
        """
        Escenario 1: Flujo completo de 8 pasos del ciclo de vida en Discord:
        1. Incorporación a Team A -> alta automática con rol STAFF.
        2. Incorporación a Team B -> alta automática con rol STAFF (multiclub).
        3. Staff ejecuta /gestionar-posicion -> despliegue interactivo con 2 clubes.
        4. Selección de Team A y asignación a MID -> persistencia y auditoría.
        5. Selección de Team B e intento de asignar TOP -> conflicto competitivo y rechazo.
        6. Selección de Team B y asignación a COACH -> éxito con rol no competitivo.
        7. Promoción a capitán en Team A -> acción promoted_to_captain.
        8. Retirada de rol de Team A en Discord -> baja en Team A, Team B intacto.
        """
        team_a, team_b = seed_two_teams
        guild = make_mock_guild(clean_settings.guild_id)

        role_team_a = make_mock_role(7001, "Planar Shock Pingus")
        role_team_b = make_mock_role(7002, "Void Invaders")
        staff_role = make_mock_role(clean_settings.staff_role_id, "Staff")

        player_id = 123456789012
        player_id_str = str(player_id)
        staff_id = 999000111222
        staff_id_str = str(staff_id)

        staff_member = make_mock_member(
            user_id=staff_id,
            name="StaffAdmin",
            roles=[staff_role],
        )

        # -----------------------------------------------------------------------
        # PASO 1: Asignación de rol de Team A -> on_member_update
        # -----------------------------------------------------------------------
        before_p1 = make_mock_member(user_id=player_id, name="FakerPlayer", roles=[])
        after_p1 = make_mock_member(user_id=player_id, name="FakerPlayer", roles=[role_team_a])

        await roster_cog.on_member_update(before_p1, after_p1)

        # Verificación en Discord: No se retiran roles
        after_p1.remove_roles.assert_not_called()
        assert role_team_a in after_p1.roles

        # Verificación en Base de Datos PGlite
        async with session_factory() as session:
            user = await session.get(DiscordUser, player_id_str)
            assert user is not None
            assert user.username == "FakerPlayer"

            m_repo = TeamMembershipRepository(session)
            m_a = await m_repo.get(team_a.id, player_id_str)
            assert m_a is not None
            assert m_a.role == RosterRole.STAFF
            assert m_a.is_captain is False

            mov_repo = RosterMovementRepository(session)
            movs_a = await mov_repo.list_by_team(team_a.id)
            assert len(movs_a) == 1
            assert movs_a[0].action == RosterMovementAction.JOINED
            assert movs_a[0].role == RosterRole.STAFF
            assert movs_a[0].discord_user_id == player_id_str

            audit_repo = AuditLogRepository(session)
            audits_a = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits_a) == 1
            assert audits_a[0].action == "roster.member_joined"
            assert audits_a[0].before is None
            assert audits_a[0].after is not None
            assert audits_a[0].after["role"] == "staff"
            assert audits_a[0].after["is_captain"] is False

        # -----------------------------------------------------------------------
        # PASO 2: Asignación de rol de Team B -> on_member_update (Multiclub)
        # -----------------------------------------------------------------------
        before_p2 = make_mock_member(user_id=player_id, name="FakerPlayer", roles=[role_team_a])
        after_p2 = make_mock_member(
            user_id=player_id,
            name="FakerPlayer",
            roles=[role_team_a, role_team_b],
        )

        await roster_cog.on_member_update(before_p2, after_p2)

        after_p2.remove_roles.assert_not_called()

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            user_memberships = await m_repo.list_by_user(player_id_str)
            assert len(user_memberships) == 2
            teams_in_db = {m.team_id for m in user_memberships}
            assert teams_in_db == {team_a.id, team_b.id}

            m_b = await m_repo.get(team_b.id, player_id_str)
            assert m_b is not None
            assert m_b.role == RosterRole.STAFF
            assert m_b.is_captain is False

            mov_repo = RosterMovementRepository(session)
            movs_user = await mov_repo.list_by_user(player_id_str)
            assert len(movs_user) == 2
            assert all(m.action == RosterMovementAction.JOINED for m in movs_user)

        # -----------------------------------------------------------------------
        # PASO 3: Staff ejecuta /gestionar-posicion
        # -----------------------------------------------------------------------
        inter_cmd = make_mock_interaction(user=staff_member, guild=guild)
        mock_panel_msg = MagicMock(spec=discord.Message)
        inter_cmd.followup.send = AsyncMock(return_value=mock_panel_msg)

        await roster_cog.gestionar_posicion.callback(roster_cog, inter_cmd, after_p2)

        inter_cmd.response.defer.assert_awaited_once_with(ephemeral=True)
        inter_cmd.followup.send.assert_awaited_once()

        kwargs_p3 = inter_cmd.followup.send.await_args.kwargs
        assert kwargs_p3.get("ephemeral") is True
        view_p3: GestionarPosicionView = kwargs_p3["view"]
        assert isinstance(view_p3, GestionarPosicionView)
        assert len(view_p3.user_teams) == 2
        assert view_p3.selected_team_id is None
        assert view_p3.team_select.disabled is False
        assert len(view_p3.team_select.options) == 2
        assert view_p3.position_select.disabled is False
        assert len(view_p3.position_select.options) == 9

        # -----------------------------------------------------------------------
        # PASO 4: Staff selecciona Team A y asigna rol 'MID' -> Guardar
        # -----------------------------------------------------------------------
        view_p3.team_select.values = [str(team_a.id)]
        inter_sel_team = make_mock_interaction(user=staff_member, guild=guild)
        await view_p3.team_select.callback(inter_sel_team)
        assert view_p3.selected_team_id == team_a.id

        view_p3.position_select.values = [RosterRole.MID.value]
        inter_sel_pos = make_mock_interaction(user=staff_member, guild=guild)
        await view_p3.position_select.callback(inter_sel_pos)
        assert view_p3.selected_role == RosterRole.MID

        inter_save_p4 = make_mock_interaction(user=staff_member, guild=guild)
        await view_p3.save_button.callback(inter_save_p4)

        assert view_p3.is_finished() is True

        # Verificar persistencia en PGlite
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            m_a_updated = await m_repo.get(team_a.id, player_id_str)
            assert m_a_updated is not None
            assert m_a_updated.role == RosterRole.MID
            assert m_a_updated.is_captain is False

            # Team B sigue intacto como STAFF
            m_b_check = await m_repo.get(team_b.id, player_id_str)
            assert m_b_check is not None
            assert m_b_check.role == RosterRole.STAFF

            mov_repo = RosterMovementRepository(session)
            movs_user = await mov_repo.list_by_user(player_id_str)
            role_changed_movs = [
                m for m in movs_user if m.action == RosterMovementAction.ROLE_CHANGED
            ]
            assert len(role_changed_movs) == 1
            assert role_changed_movs[0].team_id == team_a.id
            assert role_changed_movs[0].role == RosterRole.MID
            assert role_changed_movs[0].actor_id == staff_id_str

            audit_repo = AuditLogRepository(session)
            audits_a = await audit_repo.list_by_entity("team_membership", team_a.id)
            role_audits = [a for a in audits_a if a.action == "roster.role_changed"]
            assert len(role_audits) == 1
            assert role_audits[0].before["role"] == "staff"
            assert role_audits[0].after["role"] == "mid"
            assert role_audits[0].actor_discord_user_id == staff_id_str

        # -----------------------------------------------------------------------
        # PASO 5: Staff selecciona Team B e intenta asignar 'TOP' -> Conflicto
        # -----------------------------------------------------------------------
        inter_cmd_p5 = make_mock_interaction(user=staff_member, guild=guild)
        await roster_cog.gestionar_posicion.callback(roster_cog, inter_cmd_p5, after_p2)
        view_p5: GestionarPosicionView = inter_cmd_p5.followup.send.await_args.kwargs["view"]

        view_p5.team_select.values = [str(team_b.id)]
        await view_p5.team_select.callback(make_mock_interaction(user=staff_member, guild=guild))
        assert view_p5.selected_team_id == team_b.id

        view_p5.position_select.values = [RosterRole.TOP.value]
        await view_p5.position_select.callback(
            make_mock_interaction(user=staff_member, guild=guild)
        )
        assert view_p5.selected_role == RosterRole.TOP

        inter_save_p5 = make_mock_interaction(user=staff_member, guild=guild)
        await view_p5.save_button.callback(inter_save_p5)

        # Verificación de captura de error y envío de embed de conflicto
        inter_save_p5.followup.send.assert_awaited_once()
        conflict_embed = inter_save_p5.followup.send.await_args.kwargs["embed"]
        assert isinstance(conflict_embed, discord.Embed)
        assert "conflicto" in str(conflict_embed.title).lower()
        assert view_p5.is_finished() is False

        # Verificación en BD: Base de datos INTACTA
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            m_b_intact = await m_repo.get(team_b.id, player_id_str)
            assert m_b_intact is not None
            assert m_b_intact.role == RosterRole.STAFF

            m_a_intact = await m_repo.get(team_a.id, player_id_str)
            assert m_a_intact is not None
            assert m_a_intact.role == RosterRole.MID

        # -----------------------------------------------------------------------
        # PASO 6: Staff selecciona Team B y asigna 'COACH' -> Éxito
        # -----------------------------------------------------------------------
        view_p5.position_select.values = [RosterRole.COACH.value]
        await view_p5.position_select.callback(
            make_mock_interaction(user=staff_member, guild=guild)
        )
        assert view_p5.selected_role == RosterRole.COACH

        inter_save_p6 = make_mock_interaction(user=staff_member, guild=guild)
        await view_p5.save_button.callback(inter_save_p6)

        assert view_p5.is_finished() is True

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            m_b_updated = await m_repo.get(team_b.id, player_id_str)
            assert m_b_updated is not None
            assert m_b_updated.role == RosterRole.COACH

            # Ambas membresías coexisten (MID en Team A y COACH en Team B)
            m_a_check = await m_repo.get(team_a.id, player_id_str)
            assert m_a_check is not None
            assert m_a_check.role == RosterRole.MID

        # -----------------------------------------------------------------------
        # PASO 7: Staff promociona a capitán en Team A -> promoted_to_captain
        # -----------------------------------------------------------------------
        inter_cmd_p7 = make_mock_interaction(user=staff_member, guild=guild)
        await roster_cog.gestionar_posicion.callback(roster_cog, inter_cmd_p7, after_p2)
        view_p7: GestionarPosicionView = inter_cmd_p7.followup.send.await_args.kwargs["view"]

        view_p7.team_select.values = [str(team_a.id)]
        await view_p7.team_select.callback(make_mock_interaction(user=staff_member, guild=guild))
        view_p7.position_select.values = [RosterRole.MID.value]
        await view_p7.position_select.callback(
            make_mock_interaction(user=staff_member, guild=guild)
        )

        view_p7.is_captain = True

        inter_save_p7 = make_mock_interaction(user=staff_member, guild=guild)
        await view_p7.save_button.callback(inter_save_p7)

        assert view_p7.is_finished() is True

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            m_a_cap = await m_repo.get(team_a.id, player_id_str)
            assert m_a_cap is not None
            assert m_a_cap.is_captain is True
            assert m_a_cap.role == RosterRole.MID

            mov_repo = RosterMovementRepository(session)
            movs_user = await mov_repo.list_by_user(player_id_str)
            promoted_movs = [
                m for m in movs_user if m.action == RosterMovementAction.PROMOTED_TO_CAPTAIN
            ]
            assert len(promoted_movs) == 1
            assert promoted_movs[0].team_id == team_a.id
            assert promoted_movs[0].role == RosterRole.MID
            assert promoted_movs[0].actor_id == staff_id_str

            audit_repo = AuditLogRepository(session)
            audits_a = await audit_repo.list_by_entity("team_membership", team_a.id)
            cap_audits = [
                a for a in audits_a if a.after is not None and a.after.get("is_captain") is True
            ]
            assert len(cap_audits) >= 1

        # -----------------------------------------------------------------------
        # PASO 8: Retirada de rol de Team A en Discord -> on_member_update
        # -----------------------------------------------------------------------
        before_p8 = make_mock_member(
            user_id=player_id,
            name="FakerPlayer",
            roles=[role_team_a, role_team_b],
        )
        after_p8 = make_mock_member(
            user_id=player_id,
            name="FakerPlayer",
            roles=[role_team_b],
        )

        await roster_cog.on_member_update(before_p8, after_p8)

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            # Membresía de Team A eliminada de la base de datos
            m_a_deleted = await m_repo.get(team_a.id, player_id_str)
            assert m_a_deleted is None

            # Membresía de Team B permanece intacta
            m_b_remains = await m_repo.get(team_b.id, player_id_str)
            assert m_b_remains is not None
            assert m_b_remains.role == RosterRole.COACH
            assert m_b_remains.is_captain is False

            # Movimiento de salida registrado
            mov_repo = RosterMovementRepository(session)
            movs_a_final = await mov_repo.list_by_team(team_a.id)
            left_movs = [m for m in movs_a_final if m.action == RosterMovementAction.LEFT]
            assert len(left_movs) == 1
            assert left_movs[0].role == RosterRole.MID

            # Auditoría de baja registrada con snapshot before completo
            audit_repo = AuditLogRepository(session)
            audits_a_final = await audit_repo.list_by_entity("team_membership", team_a.id)
            left_audits = [a for a in audits_a_final if a.action == "roster.member_left"]
            assert len(left_audits) == 1
            assert left_audits[0].before is not None
            assert left_audits[0].before["role"] == "mid"
            assert left_audits[0].before["is_captain"] is True
            assert left_audits[0].after is None


# ===========================================================================
# 2. Escenario 2: Operaciones Concurrentes y Rápidas
# ===========================================================================


class TestRosterSyncRapidAndConcurrentOperationsE2E:
    """Verificación E2E de operaciones rápidas, transferencias y concurrencia."""

    @pytest.mark.asyncio
    async def test_rapid_sequential_role_add_and_immediate_remove_e2e(
        self,
        roster_cog: RosterCog,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        2A: Adición rápida de rol y remoción inmediata antes del siguiente tick.
        Verifica cero membresías residuales en BD, exactamente 2 movimientos
        (JOINED, LEFT), 2 registros de auditoría y no retirada de roles en Discord.
        """
        team_alpha, _ = seed_two_teams
        user_id = 123456001
        user_id_str = str(user_id)
        role_alpha = make_mock_role(7001, "Team Alpha")

        member_v0 = make_mock_member(user_id=user_id, roles=[])
        member_v1 = make_mock_member(user_id=user_id, roles=[role_alpha])
        member_v2 = make_mock_member(user_id=user_id, roles=[])

        # Sucesión rápida: Añadir rol y retirar inmediatamente
        await roster_cog.on_member_update(member_v0, member_v1)
        await roster_cog.on_member_update(member_v1, member_v2)

        # Discord: No se llamaron métodos de remoción de roles del bot
        member_v1.remove_roles.assert_not_called()
        member_v2.remove_roles.assert_not_called()

        async with session_factory() as s:
            membership_repo = TeamMembershipRepository(s)
            movement_repo = RosterMovementRepository(s)
            audit_repo = AuditLogRepository(s)

            # 1. Cero membresías activas en BD
            active = await membership_repo.get(team_alpha.id, user_id_str)
            assert active is None

            # 2. RosterMovements registró exactamente JOINED y LEFT
            movements = await movement_repo.list_by_user(user_id_str)
            assert len(movements) == 2
            # list_by_user devuelve orden descendente (más reciente primero)
            assert movements[0].action == RosterMovementAction.LEFT
            assert movements[0].role == RosterRole.STAFF
            assert movements[0].team_id == team_alpha.id

            assert movements[1].action == RosterMovementAction.JOINED
            assert movements[1].role == RosterRole.STAFF
            assert movements[1].team_id == team_alpha.id

            # 3. AuditLog registró exactamente member_joined y member_left
            logs = await audit_repo.list_by_entity("team_membership", team_alpha.id)
            assert len(logs) == 2
            assert logs[0].action == "roster.member_left"
            assert logs[0].after is None
            assert logs[0].before is not None
            assert logs[0].before["discord_user_id"] == user_id_str
            assert logs[0].before["role"] == "staff"

            assert logs[1].action == "roster.member_joined"
            assert logs[1].before is None
            assert logs[1].after is not None
            assert logs[1].after["discord_user_id"] == user_id_str
            assert logs[1].after["role"] == "staff"

        # 4. Idempotencia: Evento duplicado de retirada no genera duplicados
        repeated_removal = await roster_sync_service.handle_role_removed(
            member=member_v2,
            role=role_alpha,
        )
        assert repeated_removal is False

        async with session_factory() as s:
            movement_repo = RosterMovementRepository(s)
            movements_after = await movement_repo.list_by_user(user_id_str)
            assert len(movements_after) == 2

    @pytest.mark.asyncio
    async def test_competitive_role_transfer_and_unblocking_across_teams_e2e(
        self,
        roster_cog: RosterCog,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        2B: Transferencia de rol competitivo y desbloqueo entre equipos:
        Jugador en Team A como MID bloqueado de TOP en Team B.
        Al salir de Team A, queda libre y puede asumir TOP en Team B.
        Al reincorporarse a Team A como STAFF tiene éxito, pero asignar ADC se bloquea.
        """
        team_alpha, team_beta = seed_two_teams
        user_id = 123456002
        user_id_str = str(user_id)
        staff_id = "999999001"

        role_alpha = make_mock_role(7001, "Team Alpha")
        role_beta = make_mock_role(7002, "Team Beta")

        # Paso 1: Usuario se une a Team Alpha y Team Beta
        m0 = make_mock_member(user_id=user_id, roles=[])
        m1 = make_mock_member(user_id=user_id, roles=[role_alpha])
        m2 = make_mock_member(user_id=user_id, roles=[role_alpha, role_beta])

        await roster_cog.on_member_update(m0, m1)
        await roster_cog.on_member_update(m1, m2)

        # Paso 2: Asignar MID en Team Alpha
        await roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_alpha.id,
            new_role=RosterRole.MID,
            actor_id=staff_id,
        )

        # Paso 3: Intentar asignar TOP en Team Beta -> DEBE LANZAR CompetitivePositionConflictError
        with pytest.raises(CompetitivePositionConflictError) as exc_info:
            await roster_sync_service.change_player_position(
                discord_user_id=user_id_str,
                team_id=team_beta.id,
                new_role=RosterRole.TOP,
                actor_id=staff_id,
            )
        assert exc_info.value.existing_team_id == team_alpha.id
        assert exc_info.value.new_team_id == team_beta.id
        assert exc_info.value.existing_role == "mid"
        assert exc_info.value.attempted_role == "top"
        assert exc_info.value.discord_user_id == user_id_str

        # Membresía de Team Beta sigue intacta como STAFF
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            mem_b = await m_repo.get(team_beta.id, user_id_str)
            assert mem_b is not None
            assert mem_b.role == RosterRole.STAFF

        # Paso 4: Miembro pierde rol de Team Alpha en Discord
        m3 = make_mock_member(user_id=user_id, roles=[role_beta])
        await roster_cog.on_member_update(m2, m3)

        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            comp = await m_repo.get_competitive_membership(user_id_str)
            assert comp is None  # Desbloqueado competitivamente

        # Paso 5: Ahora asignar TOP en Team Beta -> DEBE TENER ÉXITO
        updated = await roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_beta.id,
            new_role=RosterRole.TOP,
            actor_id=staff_id,
        )
        assert updated.role == RosterRole.TOP
        assert updated.team_id == team_beta.id

        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            comp = await m_repo.get_competitive_membership(user_id_str)
            assert comp is not None
            assert comp.team_id == team_beta.id
            assert comp.role == RosterRole.TOP

        # Paso 6: Reingreso a Team Alpha en Discord -> STAFF tiene éxito
        m4 = make_mock_member(user_id=user_id, roles=[role_beta, role_alpha])
        await roster_cog.on_member_update(m3, m4)

        # Pero asignar ADC en Team Alpha genera conflicto competitivo
        with pytest.raises(CompetitivePositionConflictError) as exc_reblock:
            await roster_sync_service.change_player_position(
                discord_user_id=user_id_str,
                team_id=team_alpha.id,
                new_role=RosterRole.ADC,
                actor_id=staff_id,
            )
        assert exc_reblock.value.existing_team_id == team_beta.id
        assert exc_reblock.value.attempted_role == "adc"

    @pytest.mark.asyncio
    async def test_concurrent_operations_race_resilience_e2e(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        2C: Resiliencia ante carreras de operaciones concurrentes con asyncio.gather:
        - Incorporaciones simultáneas a dos clubes (concurrent add).
        - Alta en un club y baja simultánea en otro club (concurrent add & remove).
        """
        team_alpha, team_beta = seed_two_teams
        role_alpha = make_mock_role(7001, "Team Alpha")
        role_beta = make_mock_role(7002, "Team Beta")
        staff_id = "999001"

        # 1. Incorporación concurrente a dos clubes para el mismo usuario
        user_c1 = 200001
        user_c1_str = str(user_c1)
        m_c1 = make_mock_member(user_id=user_c1, name="ConcurrentUser1", roles=[])

        # Pre-sembrar usuario y actor para evitar colisión de inserción PK
        async with session_factory() as s:
            s.add_all(
                [
                    DiscordUser(discord_id=user_c1_str, username="ConcurrentUser1"),
                    DiscordUser(discord_id=staff_id, username="StaffActor"),
                ]
            )
            await s.commit()

        res_adds = await asyncio.gather(
            roster_sync_service.handle_role_added(m_c1, role_alpha, actor_id=staff_id),
            roster_sync_service.handle_role_added(m_c1, role_beta, actor_id=staff_id),
            return_exceptions=False,
        )

        assert all(r is not None for r in res_adds)

        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            user_memberships = await m_repo.list_by_user(user_c1_str)
            assert len(user_memberships) == 2
            assert {m.team_id for m in user_memberships} == {team_alpha.id, team_beta.id}

        # 2. Alta y baja ejecutadas concurrentemente (transferencia concurrente add + remove)
        user_c2 = 200002
        user_c2_str = str(user_c2)
        m_c2 = make_mock_member(user_id=user_c2, name="ConcurrentUser2", roles=[role_beta])

        # Pre-sembrar usuario y membresía en Team Alpha
        async with session_factory() as s:
            s.add(DiscordUser(discord_id=user_c2_str, username="ConcurrentUser2"))
            await s.flush()
            m_repo = TeamMembershipRepository(s)
            await m_repo.create(team_alpha.id, user_c2_str, RosterRole.STAFF)
            await s.commit()

        # Ejecución concurrente: alta en Team Beta y baja en Team Alpha
        results = await asyncio.gather(
            roster_sync_service.handle_role_added(m_c2, role_beta, actor_id=staff_id),
            roster_sync_service.handle_role_removed(m_c2, role_alpha, actor_id=staff_id),
            return_exceptions=False,
        )

        assert len(results) == 2
        assert results[0] is not None  # Alta en Beta
        assert results[1] is True  # Baja en Alpha

        # Verificamos estado final determinista y consistente en PGlite
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            mem_alpha = await m_repo.get(team_alpha.id, user_c2_str)
            assert mem_alpha is None

            mem_beta = await m_repo.get(team_beta.id, user_c2_str)
            assert mem_beta is not None
            assert mem_beta.role == RosterRole.STAFF

            mov_repo = RosterMovementRepository(s)
            movs = await mov_repo.list_by_user(user_c2_str)
            assert len(movs) == 2
            actions = {m.action for m in movs}
            assert actions == {RosterMovementAction.JOINED, RosterMovementAction.LEFT}

            audit_repo = AuditLogRepository(s)
            audits_alpha = await audit_repo.list_by_entity("team_membership", team_alpha.id)
            assert any(a.action == "roster.member_left" for a in audits_alpha)

            audits_beta = await audit_repo.list_by_entity("team_membership", team_beta.id)
            assert any(a.action == "roster.member_joined" for a in audits_beta)


# ===========================================================================
# 3. Escenario 3: Verificación Completa de Pista de Auditoría y JSONB
# ===========================================================================


class TestRosterSyncAuditTrailVerificationE2E:
    """Verificación integral de integridad referencial, JSONB y biyección de auditoría."""

    @pytest.mark.asyncio
    async def test_complete_audit_trail_and_jsonb_verification_e2e(
        self,
        roster_cog: RosterCog,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Escenario 3: Ejecuta un ciclo multiusuario y verifica:
        1. Claves foráneas válidas y tipos UUID.
        2. Correspondencia biyectiva 1:1 entre movimientos y registros de auditoría.
        3. Fidelidad de snapshots JSONB before/after en todas las transiciones.
        """
        team_alpha, team_beta = seed_two_teams
        user_1 = 300001
        user_1_str = str(user_1)
        user_2 = 300002
        user_2_str = str(user_2)
        staff_id = "999999010"

        role_alpha = make_mock_role(7001, "Team Alpha")
        role_beta = make_mock_role(7002, "Team Beta")

        # Operaciones sobre Usuario 1:
        # 1. User 1 se une a Alpha
        m1_0 = make_mock_member(user_id=user_1, roles=[])
        m1_1 = make_mock_member(user_id=user_1, roles=[role_alpha])
        await roster_cog.on_member_update(m1_0, m1_1)

        # 2. User 1 se une a Beta
        m1_2 = make_mock_member(user_id=user_1, roles=[role_alpha, role_beta])
        await roster_cog.on_member_update(m1_1, m1_2)

        # 3. User 1 cambia posición a MID en Alpha
        await roster_sync_service.change_player_position(
            discord_user_id=user_1_str,
            team_id=team_alpha.id,
            new_role=RosterRole.MID,
            actor_id=staff_id,
        )

        # 4. User 1 es promovido a Capitán en Alpha
        await roster_sync_service.change_player_position(
            discord_user_id=user_1_str,
            team_id=team_alpha.id,
            new_role=RosterRole.MID,
            actor_id=staff_id,
            is_captain=True,
        )

        # 5. User 1 es descendido de Capitán en Alpha
        await roster_sync_service.change_player_position(
            discord_user_id=user_1_str,
            team_id=team_alpha.id,
            new_role=RosterRole.MID,
            actor_id=staff_id,
            is_captain=False,
        )

        # 6. User 1 cambia posición a COACH en Beta
        await roster_sync_service.change_player_position(
            discord_user_id=user_1_str,
            team_id=team_beta.id,
            new_role=RosterRole.COACH,
            actor_id=staff_id,
        )

        # 7. User 1 abandona Team Alpha en Discord
        m1_3 = make_mock_member(user_id=user_1, roles=[role_beta])
        await roster_cog.on_member_update(m1_2, m1_3)

        # Operaciones sobre Usuario 2:
        # 8. User 2 se une a Beta
        m2_0 = make_mock_member(user_id=user_2, roles=[])
        m2_1 = make_mock_member(user_id=user_2, roles=[role_beta])
        await roster_cog.on_member_update(m2_0, m2_1)

        # 9. User 2 abandona Beta
        m2_2 = make_mock_member(user_id=user_2, roles=[])
        await roster_cog.on_member_update(m2_1, m2_2)

        # ===================================================================
        # Verificación Exhaustiva en Base de Datos
        # ===================================================================
        async with session_factory() as s:
            # 1. Recuperar todos los movimientos cronológicamente ascendente
            stmt_mov = select(RosterMovement).order_by(
                RosterMovement.created_at.asc(), RosterMovement.id.asc()
            )
            result_mov = await s.execute(stmt_mov)
            movements = list(result_mov.scalars().all())

            # 2. Recuperar todos los registros de auditoría cronológicamente ascendente
            stmt_aud = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            result_aud = await s.execute(stmt_aud)
            audit_logs = list(result_aud.scalars().all())

            # Biyección: Exactamente 9 movimientos y 9 logs de auditoría
            assert len(movements) == 9
            assert len(audit_logs) == 9

            # Recuperar usuarios y equipos existentes para validación de FKs
            stmt_users = select(DiscordUser.discord_id)
            res_users = await s.execute(stmt_users)
            valid_user_ids = set(res_users.scalars().all())

            stmt_teams = select(Team.id)
            res_teams = await s.execute(stmt_teams)
            valid_team_ids = set(res_teams.scalars().all())

            # Especificación de operaciones esperadas
            expected_ops = [
                # (action, role, team_id, discord_user_id, actor_id, audit_action)
                (
                    RosterMovementAction.JOINED,
                    RosterRole.STAFF,
                    team_alpha.id,
                    user_1_str,
                    None,
                    "roster.member_joined",
                ),
                (
                    RosterMovementAction.JOINED,
                    RosterRole.STAFF,
                    team_beta.id,
                    user_1_str,
                    None,
                    "roster.member_joined",
                ),
                (
                    RosterMovementAction.ROLE_CHANGED,
                    RosterRole.MID,
                    team_alpha.id,
                    user_1_str,
                    staff_id,
                    "roster.role_changed",
                ),
                (
                    RosterMovementAction.PROMOTED_TO_CAPTAIN,
                    RosterRole.MID,
                    team_alpha.id,
                    user_1_str,
                    staff_id,
                    "roster.role_changed",
                ),
                (
                    RosterMovementAction.DEMOTED_FROM_CAPTAIN,
                    RosterRole.MID,
                    team_alpha.id,
                    user_1_str,
                    staff_id,
                    "roster.role_changed",
                ),
                (
                    RosterMovementAction.ROLE_CHANGED,
                    RosterRole.COACH,
                    team_beta.id,
                    user_1_str,
                    staff_id,
                    "roster.role_changed",
                ),
                (
                    RosterMovementAction.LEFT,
                    RosterRole.MID,
                    team_alpha.id,
                    user_1_str,
                    None,
                    "roster.member_left",
                ),
                (
                    RosterMovementAction.JOINED,
                    RosterRole.STAFF,
                    team_beta.id,
                    user_2_str,
                    None,
                    "roster.member_joined",
                ),
                (
                    RosterMovementAction.LEFT,
                    RosterRole.STAFF,
                    team_beta.id,
                    user_2_str,
                    None,
                    "roster.member_left",
                ),
            ]

            for mov, log, exp in zip(movements, audit_logs, expected_ops, strict=True):
                exp_act, exp_role, exp_team, exp_user, exp_actor, exp_audit_act = exp

                # 1. Verificación de RosterMovement
                assert isinstance(mov.id, UUID)
                assert mov.action == exp_act
                assert mov.role == exp_role
                assert mov.team_id == exp_team
                assert mov.discord_user_id == exp_user
                assert mov.actor_id == exp_actor
                assert mov.created_at is not None

                # Integridad de claves foráneas en movimiento
                assert mov.team_id in valid_team_ids
                assert mov.discord_user_id in valid_user_ids
                if mov.actor_id is not None:
                    assert mov.actor_id in valid_user_ids

                # 2. Verificación de AuditLog
                assert isinstance(log.id, UUID)
                assert log.entity_type == "team_membership"
                assert log.entity_id == exp_team
                assert log.entity_id in valid_team_ids
                assert log.action == exp_audit_act
                assert log.actor_discord_user_id == exp_actor
                if log.actor_discord_user_id is not None:
                    assert log.actor_discord_user_id in valid_user_ids
                assert log.created_at is not None

                # 3. Correspondencia Biyectiva 1:1 y Esquemas JSONB
                if exp_act == RosterMovementAction.JOINED:
                    assert log.before is None
                    assert isinstance(log.after, dict)
                    assert log.after["team_id"] == str(exp_team)
                    assert log.after["discord_user_id"] == exp_user
                    assert log.after["role"] == "staff"
                    assert log.after["is_captain"] is False

                elif exp_act == RosterMovementAction.LEFT:
                    assert isinstance(log.before, dict)
                    assert log.after is None
                    assert log.before["team_id"] == str(exp_team)
                    assert log.before["discord_user_id"] == exp_user
                    assert log.before["role"] == exp_role.value

                elif exp_act == RosterMovementAction.ROLE_CHANGED:
                    assert isinstance(log.before, dict)
                    assert isinstance(log.after, dict)
                    assert log.before["team_id"] == str(exp_team)
                    assert log.after["team_id"] == str(exp_team)
                    assert log.before["discord_user_id"] == exp_user
                    assert log.after["discord_user_id"] == exp_user
                    assert log.after["role"] == exp_role.value
                    assert log.before["role"] != log.after["role"]

                elif exp_act == RosterMovementAction.PROMOTED_TO_CAPTAIN:
                    assert isinstance(log.before, dict)
                    assert isinstance(log.after, dict)
                    assert log.before["is_captain"] is False
                    assert log.after["is_captain"] is True
                    assert log.after["role"] == exp_role.value

                elif exp_act == RosterMovementAction.DEMOTED_FROM_CAPTAIN:
                    assert isinstance(log.before, dict)
                    assert isinstance(log.after, dict)
                    assert log.before["is_captain"] is True
                    assert log.after["is_captain"] is False
                    assert log.after["role"] == exp_role.value


# ===========================================================================
# 4. Escenarios Auxiliares E2E
# ===========================================================================


class TestRosterSyncAuxiliaryE2E:
    """Verificación E2E de controles perimetrales, permisos y descalificaciones."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_command_rejection_e2e(
        self,
        roster_cog: RosterCog,
        seed_two_teams: tuple[Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifica que un usuario sin rol de staff sea rechazado y produzca 0 mutaciones en BD."""
        unauth_user = make_mock_member(user_id=888999, roles=[], is_admin=False)
        target = make_mock_member(user_id=123456)
        inter = make_mock_interaction(user=unauth_user)

        await roster_cog.gestionar_posicion.callback(roster_cog, inter, target)

        inter.response.send_message.assert_awaited_once()
        kwargs = inter.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        msg = inter.response.send_message.await_args.args[0]
        assert "staff" in str(msg).lower() or "autorizaci\u00f3n" in str(msg).lower()

        # Cero mutaciones en base de datos
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            assert await m_repo.list_by_user("123456") == []
            mov_repo = RosterMovementRepository(session)
            assert await mov_repo.list_by_user("123456") == []

    @pytest.mark.asyncio
    async def test_user_without_teams_command_e2e(
        self,
        roster_cog: RosterCog,
        clean_settings: Settings,
    ) -> None:
        """Verifica que si el usuario no tiene equipos se envíe aviso sin vista interactiva."""
        staff = make_mock_member(
            user_id=999000,
            roles=[make_mock_role(clean_settings.staff_role_id, "Staff")],
        )
        target = make_mock_member(user_id=777888, name="SinEquipo")
        inter = make_mock_interaction(user=staff)

        await roster_cog.gestionar_posicion.callback(roster_cog, inter, target)

        inter.response.defer.assert_awaited_once_with(ephemeral=True)
        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        assert kwargs.get("view") is None or "view" not in kwargs
        embed = kwargs["embed"]
        assert "sin equipos" in str(embed.title).lower()

    @pytest.mark.asyncio
    async def test_non_team_role_update_ignored_e2e(
        self,
        roster_cog: RosterCog,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifica que roles de Discord no registrados como clubes no generen actividad en BD."""
        random_role = make_mock_role(999999, "ArbitraryDiscordRole")
        before = make_mock_member(user_id=555444, roles=[])
        after = make_mock_member(user_id=555444, roles=[random_role])

        await roster_cog.on_member_update(before, after)

        async with session_factory() as session:
            user = await session.get(DiscordUser, "555444")
            assert user is None
            m_repo = TeamMembershipRepository(session)
            assert await m_repo.list_by_user("555444") == []
            mov_repo = RosterMovementRepository(session)
            assert await mov_repo.list_by_user("555444") == []

    @pytest.mark.asyncio
    async def test_captaincy_demotion_action_e2e(
        self,
        roster_sync_service: RosterSyncService,
        seed_two_teams: tuple[Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifica que descender capitanía registre demoted_from_captain en auditoría."""
        team_a, _ = seed_two_teams
        player_id = "111222333"
        staff_id = "999000"

        # Crear como capitán
        async with session_factory() as session:
            user = DiscordUser(discord_id=player_id, username="captain_player")
            staff = DiscordUser(discord_id=staff_id, username="staff_user")
            session.add_all([user, staff])
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_a.id, player_id, RosterRole.SUPPORT, is_captain=True)
            await session.commit()

        # Descenso de capitanía
        updated = await roster_sync_service.change_player_position(
            discord_user_id=player_id,
            team_id=team_a.id,
            new_role=RosterRole.SUPPORT,
            actor_id=staff_id,
            is_captain=False,
        )

        assert updated.is_captain is False
        assert updated.role == RosterRole.SUPPORT

        async with session_factory() as session:
            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_user(player_id)
            assert len(movs) == 1
            assert movs[0].action == RosterMovementAction.DEMOTED_FROM_CAPTAIN
            assert movs[0].role == RosterRole.SUPPORT
            assert movs[0].actor_id == staff_id

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == 1
            assert audits[0].action == "roster.role_changed"
            assert audits[0].before is not None
            assert audits[0].before["is_captain"] is True
            assert audits[0].after is not None
            assert audits[0].after["is_captain"] is False
