"""
Suite de pruebas de estrés adversarial y verificación empírica de operaciones rápidas
para el subsistema de sincronización de plantillas y gestión de posiciones con
RCL-Next (Milestone 6).

Somete el sistema a cargas y escenarios de estrés:
1. Ráfagas rápidas de adición y remoción en bucle (Flip-Flop) para verificar
   limpieza absoluta de membresías y coherencia 1:1 en movimientos y auditoría.
2. Ráfagas masivas multi-usuario y multi-club (8 miembros, 3 clubes, 30+ transiciones)
   con verificación de no-orfandad y preservación de la invariante competitiva.
3. Ráfagas concurrentes intensivas con asyncio.gather combinando altas y bajas simultáneas.
4. Desbandada masiva de club (10 miembros dados de baja consecutivamente).
5. Matriz exhaustiva de estrés sobre la invariante competitiva entre múltiples clubes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

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
    TeamMembership,
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
async def seed_three_teams(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Team, Team, Team]:
    """Siembra tres equipos oficiales en la base de datos PGlite."""
    async with session_factory() as session:
        team_alpha = Team(
            name="Planar Shock Pingus",
            tag="PSP",
            slug="planar-shock-pingus",
            division=Division.PREMIER,
            discord_role_id=8001,
        )
        team_beta = Team(
            name="Void Invaders",
            tag="VOID",
            slug="void-invaders",
            division=Division.PREMIER,
            discord_role_id=8002,
        )
        team_gamma = Team(
            name="Solar Flare Gaming",
            tag="SFG",
            slug="solar-flare-gaming",
            division=Division.ASCEND,
            discord_role_id=8003,
        )
        session.add_all([team_alpha, team_beta, team_gamma])
        await session.commit()
        await session.refresh(team_alpha)
        await session.refresh(team_beta)
        await session.refresh(team_gamma)
        return team_alpha, team_beta, team_gamma


# ===========================================================================
# Clases de Pruebas de Estrés Adversarial
# ===========================================================================


class TestRosterSyncRapidOperationsStress:
    """Batería de pruebas de estrés sobre operaciones rápidas y contención de estado."""

    @pytest.mark.asyncio
    async def test_stress_rapid_flip_flop_role_updates(
        self,
        roster_cog: RosterCog,
        session_factory: async_sessionmaker[AsyncSession],
        seed_three_teams: tuple[Team, Team, Team],
    ) -> None:
        """
        Estrés Flip-Flop: 10 ciclos consecutivos de adición y remoción inmediata
        de rol de equipo en Discord (20 eventos totales).
        Verifica:
        - 0 membresías activas al final.
        - Exactamente 20 movimientos (10 JOINED y 10 LEFT alternados).
        - Exactamente 20 logs de auditoría (10 member_joined y 10 member_left).
        - Cero corrupción referencial ni registros huérfanos.
        """
        team_alpha, _, _ = seed_three_teams
        user_id = 500001
        user_id_str = str(user_id)
        role_alpha = make_mock_role(8001, "Planar Shock Pingus")

        cycles = 10
        for _i in range(cycles):
            # 1. Asignar rol
            m_empty = make_mock_member(user_id=user_id, roles=[])
            m_with_role = make_mock_member(user_id=user_id, roles=[role_alpha])
            await roster_cog.on_member_update(m_empty, m_with_role)

            # 2. Retirar rol inmediatamente
            await roster_cog.on_member_update(m_with_role, m_empty)

        # Verificación forense en base de datos
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            active = await m_repo.get(team_alpha.id, user_id_str)
            assert active is None, "No deben existir membresías activas tras los ciclos flip-flop"

            mov_repo = RosterMovementRepository(session)
            movements = await mov_repo.list_by_user(user_id_str)
            assert len(movements) == cycles * 2, f"Se esperaban {cycles * 2} movimientos"

            # list_by_user devuelve orden descendente (el más reciente primero: LEFT, JOINED, ...)
            for idx, mov in enumerate(movements):
                expected_action = (
                    RosterMovementAction.LEFT if idx % 2 == 0 else RosterMovementAction.JOINED
                )
                assert mov.action == expected_action
                assert mov.team_id == team_alpha.id
                assert mov.role == RosterRole.STAFF

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_alpha.id)
            assert len(audits) == cycles * 2, f"Se esperaban {cycles * 2} registros de auditoría"

    @pytest.mark.asyncio
    async def test_stress_mass_members_rapid_burst(
        self,
        roster_cog: RosterCog,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_three_teams: tuple[Team, Team, Team],
    ) -> None:
        """
        Estrés Masivo Multi-Miembro y Multi-Club:
        - 8 miembros diferentes interactúan con 3 clubes en ráfagas rápidas.
        - Asignaciones concurrentes, transferencias, cambios a roles competitivos y capitanía.
        - Ráfaga de salidas parciales.
        - Verificación de consistencia total en team_memberships, roster_movements y audit_logs.
        """
        team_a, team_b, team_c = seed_three_teams
        role_a = make_mock_role(8001, "Team Alpha")
        role_b = make_mock_role(8002, "Team Beta")
        role_c = make_mock_role(8003, "Team Gamma")
        staff_id = "999999000"

        member_ids = [500100 + i for i in range(8)]
        member_id_strs = [str(mid) for mid in member_ids]

        # -------------------------------------------------------------------
        # RÁFAGA 1: Los 8 miembros reciben el rol de Team Alpha
        # -------------------------------------------------------------------
        for mid in member_ids:
            m_before = make_mock_member(user_id=mid, roles=[])
            m_after = make_mock_member(user_id=mid, roles=[role_a])
            await roster_cog.on_member_update(m_before, m_after)

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            memberships_a = await m_repo.list_by_team(team_a.id)
            assert len(memberships_a) == 8
            assert all(m.role == RosterRole.STAFF for m in memberships_a)

        # -------------------------------------------------------------------
        # RÁFAGA 2: Miembros 0, 1, 2, 3 también se unen a Team Beta (Multiclub)
        # -------------------------------------------------------------------
        for mid in member_ids[:4]:
            m_before = make_mock_member(user_id=mid, roles=[role_a])
            m_after = make_mock_member(user_id=mid, roles=[role_a, role_b])
            await roster_cog.on_member_update(m_before, m_after)

        # -------------------------------------------------------------------
        # RÁFAGA 3: Miembros 4, 5 también se unen a Team Gamma (Multiclub)
        # -------------------------------------------------------------------
        for mid in member_ids[4:6]:
            m_before = make_mock_member(user_id=mid, roles=[role_a])
            m_after = make_mock_member(user_id=mid, roles=[role_a, role_c])
            await roster_cog.on_member_update(m_before, m_after)

        # -------------------------------------------------------------------
        # RÁFAGA 4: Asignaciones de posiciones y capitanía bajo tensión
        # -------------------------------------------------------------------
        # Miembro 0: MID en Team A (competitivo)
        await roster_sync_service.change_player_position(
            discord_user_id=member_id_strs[0],
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=staff_id,
        )

        # Miembro 0: Intento de asignar TOP en Team Beta -> DEBE LANZAR ERROR
        with pytest.raises(CompetitivePositionConflictError):
            await roster_sync_service.change_player_position(
                discord_user_id=member_id_strs[0],
                team_id=team_b.id,
                new_role=RosterRole.TOP,
                actor_id=staff_id,
            )

        # Miembro 0: Asignar COACH en Team Beta -> DEBE TENER ÉXITO (no competitivo)
        await roster_sync_service.change_player_position(
            discord_user_id=member_id_strs[0],
            team_id=team_b.id,
            new_role=RosterRole.COACH,
            actor_id=staff_id,
        )

        # Miembro 1: TOP en Team B (competitivo en Beta)
        await roster_sync_service.change_player_position(
            discord_user_id=member_id_strs[1],
            team_id=team_b.id,
            new_role=RosterRole.TOP,
            actor_id=staff_id,
        )

        # Miembro 2: Promovido a Capitán (SUPPORT) en Team A
        await roster_sync_service.change_player_position(
            discord_user_id=member_id_strs[2],
            team_id=team_a.id,
            new_role=RosterRole.SUPPORT,
            actor_id=staff_id,
            is_captain=True,
        )

        # -------------------------------------------------------------------
        # RÁFAGA 5: Salida rápida de Team Alpha para miembros 0, 1, 2, 3
        # -------------------------------------------------------------------
        for mid in member_ids[:4]:
            m_before = make_mock_member(user_id=mid, roles=[role_a, role_b])
            m_after = make_mock_member(user_id=mid, roles=[role_b])
            await roster_cog.on_member_update(m_before, m_after)

        # -------------------------------------------------------------------
        # RÁFAGA 6: Desbloqueo competitivo verificado tras salida
        # Miembro 0, habiendo salido de Team A, ahora SÍ puede asumir ADC en Team Beta
        # -------------------------------------------------------------------
        updated_m0_b = await roster_sync_service.change_player_position(
            discord_user_id=member_id_strs[0],
            team_id=team_b.id,
            new_role=RosterRole.ADC,
            actor_id=staff_id,
        )
        assert updated_m0_b.role == RosterRole.ADC
        assert updated_m0_b.team_id == team_b.id

        # -------------------------------------------------------------------
        # Verificación Integral y Forense del Estado
        # -------------------------------------------------------------------
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)

            # Team A ahora solo tiene a miembros 4, 5, 6, 7 (4 miembros)
            active_a = await m_repo.list_by_team(team_a.id)
            assert len(active_a) == 4
            user_ids_in_a = {m.discord_user_id for m in active_a}
            assert user_ids_in_a == set(member_id_strs[4:8])

            # Team B tiene a miembros 0, 1, 2, 3 (4 miembros)
            active_b = await m_repo.list_by_team(team_b.id)
            assert len(active_b) == 4
            user_ids_in_b = {m.discord_user_id for m in active_b}
            assert user_ids_in_b == set(member_id_strs[:4])

            # Team C tiene a miembros 4, 5 (2 miembros)
            active_c = await m_repo.list_by_team(team_c.id)
            assert len(active_c) == 2
            user_ids_in_c = {m.discord_user_id for m in active_c}
            assert user_ids_in_c == set(member_id_strs[4:6])

            # Invariante Competitiva: Ningún usuario tiene más de 1 rol competitivo en toda la BD
            stmt_comp = select(TeamMembership).where(
                TeamMembership.role.in_(
                    [
                        RosterRole.TOP,
                        RosterRole.JUNGLE,
                        RosterRole.MID,
                        RosterRole.ADC,
                        RosterRole.SUPPORT,
                        RosterRole.SUBSTITUTE,
                    ]
                )
            )
            res_comp = await session.execute(stmt_comp)
            all_comp = list(res_comp.scalars().all())

            # Verificar que no hay discord_user_id duplicado entre membresías competitivas
            comp_user_counts: dict[str, int] = {}
            for cm in all_comp:
                comp_user_counts[cm.discord_user_id] = (
                    comp_user_counts.get(cm.discord_user_id, 0) + 1
                )
            assert all(count <= 1 for count in comp_user_counts.values()), (
                f"Violación de invariante competitiva detectada: {comp_user_counts}"
            )

            # Verificación de correspondencia 1:1 entre RosterMovement y AuditLog
            res_all_mov = await session.execute(select(RosterMovement))
            all_movs = list(res_all_mov.scalars().all())

            res_all_aud = await session.execute(select(AuditLog))
            all_auds = list(res_all_aud.scalars().all())

            assert len(all_movs) == len(all_auds), (
                f"Discrepancia entre movimientos ({len(all_movs)}) y auditorías ({len(all_auds)})"
            )

    @pytest.mark.asyncio
    async def test_stress_concurrent_burst_operations(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_three_teams: tuple[Team, Team, Team],
    ) -> None:
        """
        Estrés Concurrente con asyncio.gather:
        - 6 miembros y 3 clubes.
        - Altas simultáneas masivas en paralelo.
        - Altas y bajas concurrentes cruzadas.
        - Verificación de consistencia y no-corrupción transaccional.
        """
        team_a, team_b, team_c = seed_three_teams
        role_a = make_mock_role(8001, "Team Alpha")
        role_b = make_mock_role(8002, "Team Beta")
        role_c = make_mock_role(8003, "Team Gamma")
        staff_id = "999999001"

        members = [make_mock_member(user_id=600000 + i, roles=[]) for i in range(6)]
        member_strs = [str(600000 + i) for i in range(6)]

        # Pre-sembrar usuarios en discord_users para evitar conflicto de clave primaria
        async with session_factory() as session:
            session.add(DiscordUser(discord_id=staff_id, username="StaffActor"))
            for m in members:
                session.add(DiscordUser(discord_id=str(m.id), username=f"User_{m.id}"))
            await session.commit()

        # Ráfaga concurrente 1: 6 altas concurrentes a diferentes equipos
        tasks_1 = [
            roster_sync_service.handle_role_added(members[0], role_a, actor_id=staff_id),
            roster_sync_service.handle_role_added(members[1], role_b, actor_id=staff_id),
            roster_sync_service.handle_role_added(members[2], role_c, actor_id=staff_id),
            roster_sync_service.handle_role_added(members[3], role_a, actor_id=staff_id),
            roster_sync_service.handle_role_added(members[4], role_b, actor_id=staff_id),
            roster_sync_service.handle_role_added(members[5], role_c, actor_id=staff_id),
        ]
        results_1 = await asyncio.gather(*tasks_1, return_exceptions=False)
        assert len(results_1) == 6
        assert all(r is not None for r in results_1)

        # Ráfaga concurrente 2: Operaciones cruzadas simultáneas (altas y bajas combinadas)
        tasks_2 = [
            # Miembro 0 se une también a Beta (concurrent add)
            roster_sync_service.handle_role_added(members[0], role_b, actor_id=staff_id),
            # Miembro 1 abandona Beta (concurrent remove)
            roster_sync_service.handle_role_removed(members[1], role_b, actor_id=staff_id),
            # Miembro 2 se une también a Alpha (concurrent add)
            roster_sync_service.handle_role_added(members[2], role_a, actor_id=staff_id),
            # Miembro 3 abandona Alpha (concurrent remove)
            roster_sync_service.handle_role_removed(members[3], role_a, actor_id=staff_id),
        ]
        results_2 = await asyncio.gather(*tasks_2, return_exceptions=False)
        assert len(results_2) == 4
        assert results_2[0] is not None  # Alta M0 en Beta
        assert results_2[1] is True  # Baja M1 en Beta
        assert results_2[2] is not None  # Alta M2 en Alpha
        assert results_2[3] is True  # Baja M3 en Alpha

        # Verificación forense del estado resultante
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)

            # M0 tiene membresías en A y B
            m0_teams = {m.team_id for m in await m_repo.list_by_user(member_strs[0])}
            assert m0_teams == {team_a.id, team_b.id}

            # M1 no tiene membresía en B (dado de baja)
            m1_teams = {m.team_id for m in await m_repo.list_by_user(member_strs[1])}
            assert len(m1_teams) == 0

            # M2 tiene membresías en C y A
            m2_teams = {m.team_id for m in await m_repo.list_by_user(member_strs[2])}
            assert m2_teams == {team_c.id, team_a.id}

            # M3 no tiene membresía en A (dado de baja)
            m3_teams = {m.team_id for m in await m_repo.list_by_user(member_strs[3])}
            assert len(m3_teams) == 0

            # M4 sigue en B
            assert await m_repo.get(team_b.id, member_strs[4]) is not None
            # M5 sigue en C
            assert await m_repo.get(team_c.id, member_strs[5]) is not None

            # Total de movimientos: 6 de ráfaga 1 + 4 de ráfaga 2 = 10 movimientos
            mov_res = await session.execute(select(RosterMovement))
            total_movs = list(mov_res.scalars().all())
            assert len(total_movs) == 10

            # Total de logs de auditoría: exactamente 10
            aud_res = await session.execute(select(AuditLog))
            total_auds = list(aud_res.scalars().all())
            assert len(total_auds) == 10

    @pytest.mark.asyncio
    async def test_stress_mass_disband_rapid_removal(
        self,
        roster_cog: RosterCog,
        session_factory: async_sessionmaker[AsyncSession],
        seed_three_teams: tuple[Team, Team, Team],
    ) -> None:
        """
        Estrés de Desbandada Masiva:
        - 10 miembros asignados a Team Alpha.
        - En rápida sucesión, los 10 miembros pierden el rol de Team Alpha en Discord.
        - Verifica que:
          1. El equipo queda con exactamente 0 membresías activas.
          2. Se registran exactamente 10 movimientos JOINED y 10 movimientos LEFT.
          3. Cada movimiento LEFT tiene su registro en audit_logs con snapshot before.
          4. Otros equipos permanecen inalterados.
        """
        team_alpha, team_beta, _ = seed_three_teams
        role_alpha = make_mock_role(8001, "Team Alpha")
        role_beta = make_mock_role(8002, "Team Beta")

        members_count = 10
        disband_ids = [700000 + i for i in range(members_count)]

        # 1. Incorporación previa de miembros 0 y 1 a Team Beta
        for uid in disband_ids[:2]:
            m_init = make_mock_member(user_id=uid, roles=[])
            m_beta = make_mock_member(user_id=uid, roles=[role_beta])
            await roster_cog.on_member_update(m_init, m_beta)

        # 2. Incorporación masiva a Team Alpha
        for uid in disband_ids:
            roles_before = [role_beta] if uid in disband_ids[:2] else []
            roles_after = roles_before + [role_alpha]

            m_b = make_mock_member(user_id=uid, roles=roles_before)
            m_a = make_mock_member(user_id=uid, roles=roles_after)
            await roster_cog.on_member_update(m_b, m_a)

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            assert len(await m_repo.list_by_team(team_alpha.id)) == members_count

        # 2. Desbandada masiva rápida: retirar rol de Team Alpha a los 10 miembros
        for uid in disband_ids:
            roles_before = [role_beta, role_alpha] if uid in disband_ids[:2] else [role_alpha]
            roles_after = [role_beta] if uid in disband_ids[:2] else []

            m_b = make_mock_member(user_id=uid, roles=roles_before)
            m_a = make_mock_member(user_id=uid, roles=roles_after)
            await roster_cog.on_member_update(m_b, m_a)

        # 3. Verificación de estado post-desbandada
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            # Team Alpha debe estar completamente vacío
            assert len(await m_repo.list_by_team(team_alpha.id)) == 0

            # Team Beta retiene intactos a los miembros 0 y 1
            beta_members = await m_repo.list_by_team(team_beta.id)
            assert len(beta_members) == 2
            assert {m.discord_user_id for m in beta_members} == {
                str(disband_ids[0]),
                str(disband_ids[1]),
            }

            # Movimientos de Team Alpha: exactamente 10 JOINED y 10 LEFT
            mov_repo = RosterMovementRepository(session)
            movs_alpha = await mov_repo.list_by_team(team_alpha.id)
            assert len(movs_alpha) == members_count * 2
            joined_movs = [m for m in movs_alpha if m.action == RosterMovementAction.JOINED]
            left_movs = [m for m in movs_alpha if m.action == RosterMovementAction.LEFT]
            assert len(joined_movs) == members_count
            assert len(left_movs) == members_count

            # Auditoría: todos los LEFT tienen snapshot before completo
            audit_repo = AuditLogRepository(session)
            audits_alpha = await audit_repo.list_by_entity("team_membership", team_alpha.id)
            left_audits = [a for a in audits_alpha if a.action == "roster.member_left"]
            assert len(left_audits) == members_count
            for audit in left_audits:
                assert audit.before is not None
                assert audit.before["team_id"] == str(team_alpha.id)
                assert audit.after is None

    @pytest.mark.asyncio
    async def test_stress_competitive_invariant_matrix(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_three_teams: tuple[Team, Team, Team],
    ) -> None:
        """
        Matriz de Estrés de la Invariante Competitiva:
        - Para todas las posiciones competitivas (TOP, JUNGLE, MID, ADC, SUPPORT, SUBSTITUTE):
          1. Usuario en Team Alpha toma rol competitivo C1.
          2. Intento de asignar cualquier rol competitivo C2 en Team Beta -> RECHAZO 100%.
          3. Intento de asignar cualquier rol competitivo C3 en Team Gamma -> RECHAZO 100%.
          4. Intento de asignar rol no competitivo (COACH, STAFF, PARTNERS) en Team Beta -> ÉXITO.
          5. Bajar a rol no competitivo en Team Alpha -> Desbloqueo inmediato.
        """
        team_a, team_b, team_c = seed_three_teams
        player_id = "888000111"
        staff_id = "999000222"

        competitive_roles = [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
            RosterRole.SUBSTITUTE,
        ]
        non_competitive_roles = [
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
        ]

        # Crear membresías iniciales como STAFF en los 3 clubes
        async with session_factory() as s:
            s.add(DiscordUser(discord_id=player_id, username="MatrixPlayer"))
            s.add(DiscordUser(discord_id=staff_id, username="StaffActor"))
            await s.flush()
            m_repo = TeamMembershipRepository(s)
            await m_repo.create(team_a.id, player_id, RosterRole.STAFF)
            await m_repo.create(team_b.id, player_id, RosterRole.STAFF)
            await m_repo.create(team_c.id, player_id, RosterRole.STAFF)
            await s.commit()

        # Probar cada rol competitivo como titular en Team A
        for comp_role_a in competitive_roles:
            # 1. Asignar rol competitivo en Team A
            await roster_sync_service.change_player_position(
                discord_user_id=player_id,
                team_id=team_a.id,
                new_role=comp_role_a,
                actor_id=staff_id,
            )

            # 2. Intentar asignar CUALQUIER rol competitivo en Team B -> Debe fallar siempre
            for comp_role_b in competitive_roles:
                with pytest.raises(CompetitivePositionConflictError) as exc_b:
                    await roster_sync_service.change_player_position(
                        discord_user_id=player_id,
                        team_id=team_b.id,
                        new_role=comp_role_b,
                        actor_id=staff_id,
                    )
                assert exc_b.value.existing_team_id == team_a.id
                assert exc_b.value.new_team_id == team_b.id
                assert exc_b.value.existing_role == comp_role_a.value
                assert exc_b.value.attempted_role == comp_role_b.value

            # 3. Intentar asignar CUALQUIER rol competitivo en Team C -> Debe fallar siempre
            for comp_role_c in competitive_roles:
                with pytest.raises(CompetitivePositionConflictError) as exc_c:
                    await roster_sync_service.change_player_position(
                        discord_user_id=player_id,
                        team_id=team_c.id,
                        new_role=comp_role_c,
                        actor_id=staff_id,
                    )
                assert exc_c.value.existing_team_id == team_a.id
                assert exc_c.value.new_team_id == team_c.id

            # 4. Asignar rol no competitivo en Team B -> Debe funcionar sin problema
            for non_comp in non_competitive_roles:
                updated_b = await roster_sync_service.change_player_position(
                    discord_user_id=player_id,
                    team_id=team_b.id,
                    new_role=non_comp,
                    actor_id=staff_id,
                )
                assert updated_b.role == non_comp

            # 5. Desbloquear a Team A cambiándolo a rol no competitivo (STAFF)
            await roster_sync_service.change_player_position(
                discord_user_id=player_id,
                team_id=team_a.id,
                new_role=RosterRole.STAFF,
                actor_id=staff_id,
            )

        # Estado final en BD: Todas las membresías son STAFF o no competitivas
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            comp = await m_repo.get_competitive_membership(player_id)
            assert comp is None, "Al finalizar no debe existir membresía competitiva activa"
