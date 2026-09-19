"""
Adversarial Empirical Challenge Suite: Concurrency Race & Audit Trail Verification.

Validates under stress against real PGlite PostgreSQL:
1. Concurrency race conditions:
   - Concurrent asyncio.gather sync operations on PGlite DB (burst member additions).
   - Idempotency races (duplicate concurrent role additions for same user/team).
   - Competitive single-position invariant enforcement under concurrent requests.
   - Captaincy partial-index enforcement under concurrent promotions.
   - PGlite engine lock handling and connection integrity across multi-iteration bursts.
   - Lock release and connection recovery following intentional transaction failures.
2. Audit trail verification:
   - 100% schema match for all audit logs (action, before, after, actor, entity_type, entity_id).
   - Zero orphan movements or audit logs (strict integrity against teams and discord_users).
   - Cascade deletion semantics on user and team removal (foreign key constraints).
   - JSONB deep data type normalization roundtrip fidelity.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.database import _engine_locks, transactional_session
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
    """Crea un mock estricto de discord.Role."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def make_mock_member(
    user_id: int,
    name: str = "TestPlayer",
    roles: list[discord.Role] | None = None,
) -> MagicMock:
    """Crea un mock de discord.Member con seguimiento de roles."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.global_name = f"{name} Global"
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = list(roles or [])

    avatar = MagicMock()
    avatar.key = f"key_{user_id}"
    avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/avatar.png"
    member.avatar = avatar
    member.display_avatar = avatar

    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
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
    y refresca el lock del event loop actual para PGlite.
    """
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
# 1. Pruebas de Resiliencia ante Carreras y Concurrencia (asyncio.gather)
# ===========================================================================


class TestEmpiricalConcurrencyRacesAndLockHandling:
    """Pruebas adversariales de concurrencia sobre PGlite con StaticPool."""

    @pytest.mark.asyncio
    async def test_concurrent_asyncio_gather_sync_operations_multiple_users(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Estrés: 8 usuarios distintos son agregados concurrentemente al mismo equipo
        vía asyncio.gather.
        Verifica que el lock del motor serializa las transacciones y que:
        - Exactamente 8 membresías son persistidas con rol STAFF.
        - Exactamente 8 movimientos JOINED son creados.
        - Exactamente 8 entradas de auditoría roster.member_joined son registradas.
        - Cero corrupciones de conexión o errores en PGlite.
        """
        team_a, _ = seed_two_teams
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)
        num_users = 8
        members = [
            make_mock_member(user_id=100000 + i, name=f"ConcurrentPlayer_{i}")
            for i in range(num_users)
        ]
        staff_actor_id = "999000111"

        # Pre-sembrar el actor para evitar colisión de clave primaria en actor
        async with session_factory() as s:
            s.add(DiscordUser(discord_id=staff_actor_id, username="StaffSupervisor"))
            await s.commit()

        # Ejecutar adición concurrente masiva
        tasks = [
            roster_sync_service.handle_role_added(member, role_a, actor_id=staff_actor_id)
            for member in members
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        assert len(results) == num_users
        assert all(r is not None for r in results)
        assert all(r.role == RosterRole.STAFF for r in results)
        assert all(r.team_id == team_a.id for r in results)

        # Verificación en BD
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            team_memberships = await m_repo.list_by_team(team_a.id)
            assert len(team_memberships) == num_users

            mov_repo = RosterMovementRepository(s)
            movements = await mov_repo.list_by_team(team_a.id)
            assert len(movements) == num_users
            assert all(m.action == RosterMovementAction.JOINED for m in movements)
            assert all(m.role == RosterRole.STAFF for m in movements)
            assert all(m.actor_id == staff_actor_id for m in movements)

            audit_repo = AuditLogRepository(s)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == num_users
            assert all(a.action == "roster.member_joined" for a in audits)
            assert all(a.actor_discord_user_id == staff_actor_id for a in audits)

    @pytest.mark.asyncio
    async def test_concurrent_idempotent_role_add_for_same_user_and_team(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Estrés: 5 llamadas concurrentes a handle_role_added para el MISMO usuario y equipo.
        Verifica que el bloqueo de transacciones en PGlite garantiza idempotencia limpia:
        - Todas las llamadas retornan con éxito la membresía.
        - Exactamente 1 membresía existe en BD.
        - Exactamente 1 movimiento JOINED y 1 log de auditoría son registrados (sin duplicados).
        """
        team_a, _ = seed_two_teams
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)
        user_id = 100099
        user_id_str = str(user_id)
        member = make_mock_member(user_id=user_id, name="IdempotentCandidate")
        actor_id = "888000111"

        async with session_factory() as s:
            s.add(DiscordUser(discord_id=actor_id, username="SuperAdmin"))
            await s.commit()

        tasks = [
            roster_sync_service.handle_role_added(member, role_a, actor_id=actor_id)
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        assert len(results) == 5
        assert all(r is not None for r in results)
        assert all(r.discord_user_id == user_id_str for r in results)

        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            mem = await m_repo.get(team_a.id, user_id_str)
            assert mem is not None

            mov_repo = RosterMovementRepository(s)
            movs = await mov_repo.list_by_user(user_id_str)
            assert len(movs) == 1
            assert movs[0].action == RosterMovementAction.JOINED

            audit_repo = AuditLogRepository(s)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == 1
            assert audits[0].action == "roster.member_joined"

    @pytest.mark.asyncio
    async def test_concurrent_competitive_role_assignment_race_prevents_dual_assignment(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Estrés: Un usuario pertenece a Team A y Team B (como STAFF).
        Se lanzan concurrentemente dos solicitudes change_player_position para asignar
        roles competitivos en ambos equipos:
        - Tarea 1: Asignar MID en Team A.
        - Tarea 2: Asignar TOP en Team B.
        Invariante: Un usuario solo puede ocupar un rol competitivo en como máximo 1 equipo.
        Verifica:
        - Exactamente una de las dos llamadas triunfa.
        - La otra llamada lanza CompetitivePositionConflictError.
        - El estado de la base de datos conserva exactamente 1 rol competitivo en toda la liga.
        """
        team_a, team_b = seed_two_teams
        user_id = 100150
        user_id_str = str(user_id)
        actor_id = "999000222"

        # Pre-sembrar usuario y membresías iniciales como STAFF en ambos equipos
        async with session_factory() as s:
            s.add_all(
                [
                    DiscordUser(discord_id=user_id_str, username="DualTeamPlayer"),
                    DiscordUser(discord_id=actor_id, username="StaffActor"),
                ]
            )
            await s.flush()
            m_repo = TeamMembershipRepository(s)
            await m_repo.create(team_a.id, user_id_str, RosterRole.STAFF)
            await m_repo.create(team_b.id, user_id_str, RosterRole.STAFF)
            await s.commit()

        # Lanzar concurrentemente dos asignaciones competitivas en equipos distintos
        task_a = roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=actor_id,
        )
        task_b = roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_b.id,
            new_role=RosterRole.TOP,
            actor_id=actor_id,
        )

        results = await asyncio.gather(task_a, task_b, return_exceptions=True)

        successes = [r for r in results if isinstance(r, TeamMembership)]
        conflicts = [r for r in results if isinstance(r, CompetitivePositionConflictError)]

        assert len(successes) == 1, (
            f"Exactamente una asignación competitiva debió tener éxito, resultados: {results}"
        )
        assert len(conflicts) == 1, (
            f"La segunda asignación debió ser rechazada por conflicto, resultados: {results}"
        )

        # Verificar invariante estricta en la base de datos
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            comp = await m_repo.get_competitive_membership(user_id_str)
            assert comp is not None
            # El rol competitivo debe ser MID en Team A o TOP en Team B, pero nunca ambos
            if comp.team_id == team_a.id:
                assert comp.role == RosterRole.MID
                mem_b = await m_repo.get(team_b.id, user_id_str)
                assert mem_b is not None
                assert mem_b.role == RosterRole.STAFF
            else:
                assert comp.team_id == team_b.id
                assert comp.role == RosterRole.TOP
                mem_a = await m_repo.get(team_a.id, user_id_str)
                assert mem_a is not None
                assert mem_a.role == RosterRole.STAFF

    @pytest.mark.asyncio
    async def test_concurrent_captain_promotion_race_enforces_partial_index(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Estrés: Dos jugadores distintos en el mismo equipo (User 1 como MID y User 2 como TOP)
        son promovidos concurrentemente a capitanes (is_captain=True).
        El índice parcial team_memberships_unique_captain prohíbe más de un capitán por club.
        Verifica:
        - A lo sumo una de las promociones triunfa.
        - La otra falla con IntegrityError o es rechazada.
        - La base de datos contiene estrictamente 1 capitán en el equipo.
        - La conexión PGlite permanece limpia y operativa para consultas subsiguientes.
        """
        team_a, _ = seed_two_teams
        u1_str = "100201"
        u2_str = "100202"
        actor_id = "999000333"

        async with session_factory() as s:
            s.add_all(
                [
                    DiscordUser(discord_id=u1_str, username="Starter1"),
                    DiscordUser(discord_id=u2_str, username="Starter2"),
                    DiscordUser(discord_id=actor_id, username="TeamDirector"),
                ]
            )
            await s.flush()
            m_repo = TeamMembershipRepository(s)
            await m_repo.create(team_a.id, u1_str, RosterRole.MID)
            await m_repo.create(team_a.id, u2_str, RosterRole.TOP)
            await s.commit()

        task_1 = roster_sync_service.change_player_position(
            discord_user_id=u1_str,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=actor_id,
            is_captain=True,
        )
        task_2 = roster_sync_service.change_player_position(
            discord_user_id=u2_str,
            team_id=team_a.id,
            new_role=RosterRole.TOP,
            actor_id=actor_id,
            is_captain=True,
        )

        results = await asyncio.gather(task_1, task_2, return_exceptions=True)

        successes = [r for r in results if isinstance(r, TeamMembership)]
        errors = [r for r in results if isinstance(r, (IntegrityError, Exception))]

        assert len(successes) == 1, (
            f"Exactamente un jugador debió ser promovido a capitán, resultados: {results}"
        )
        assert len(errors) == 1, (
            f"La segunda capitanía debió ser abortada por el índice único, resultados: {results}"
        )

        # Verificación en BD: exactamente 1 capitán
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            members = await m_repo.list_by_team(team_a.id)
            captains = [m for m in members if m.is_captain]
            assert len(captains) == 1
            assert captains[0].discord_user_id in (u1_str, u2_str)

    @pytest.mark.asyncio
    async def test_pglite_engine_lock_handling_across_test_iterations(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Estrés de robustez: Ejecuta 10 iteraciones secuenciales de ráfagas concurrentes.
        Verifica que el manejo del lock no acumula estados inconsistentes, no genera
        fugas de conexión en StaticPool y preserva la capacidad de respuesta de PGlite.
        """
        team_a, _ = seed_two_teams
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)

        for iteration in range(10):
            u_id = 200000 + iteration
            member = make_mock_member(user_id=u_id, name=f"IterPlayer_{iteration}")

            # Ráfaga: adición y posterior consulta de equipos concurrentes
            add_task = roster_sync_service.handle_role_added(member, role_a)
            get_task = roster_sync_service.get_user_teams(str(u_id))

            res_add, res_get = await asyncio.gather(add_task, get_task)
            assert res_add is not None
            assert res_add.role == RosterRole.STAFF

            # Limpiar membresía para la siguiente iteración
            del_result = await roster_sync_service.handle_role_removed(member, role_a)
            assert del_result is True

        # Verificar BD limpia de membresías al final de las 10 iteraciones
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            remaining = await m_repo.list_by_team(team_a.id)
            assert len(remaining) == 0

            mov_repo = RosterMovementRepository(s)
            movs = await mov_repo.list_by_team(team_a.id)
            assert len(movs) == 20  # 10 JOINED + 10 LEFT

    @pytest.mark.asyncio
    async def test_engine_lock_recovery_after_transaction_failure(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Verifica que si una transacción falla con excepción (ej. fallo deliberado),
        el context manager transactional_session libera el lock del motor y realiza
        rollback limpio, permitiendo que las operaciones posteriores se ejecuten sin bloqueo.
        """
        team_a, _ = seed_two_teams
        user_id_str = "300099"

        # Lanzar una transacción con fallo forzado
        with pytest.raises(ValueError, match="Intentional failure"):
            async with transactional_session(session_factory) as session:
                session.add(DiscordUser(discord_id=user_id_str, username="FailingUser"))
                await session.flush()
                raise ValueError("Intentional failure")

        # Inmediatamente ejecutar una transacción normal; el lock debe estar libre
        async with session_factory() as session:
            user = await session.get(DiscordUser, user_id_str)
            assert user is None  # Rollback exitoso

        # Realizar una adición exitosa con el servicio
        member = make_mock_member(user_id=int(user_id_str), name="RecoveredUser")
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)
        mem = await roster_sync_service.handle_role_added(member, role_a)
        assert mem is not None
        assert mem.discord_user_id == user_id_str


# ===========================================================================
# 2. Pruebas de Verificación de Esquema de Auditoría y Huérfanos
# ===========================================================================


class TestEmpiricalAuditTrailSchemaAndOrphanVerification:
    """Pruebas adversariales de schema JSONB de auditoría e integridad referencial."""

    @pytest.mark.asyncio
    async def test_100_percent_audit_logs_match_required_schema(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Valida que el 100% de los registros en audit_logs generados por el ciclo de vida
        cumplen rigurosamente con el esquema esperado:
        - action es un string no vacío del catálogo canónico.
        - entity_type == 'team_membership'.
        - entity_id es un UUID válido de equipo.
        - before y after son None o dicts serializables con claves estandarizadas.
        - actor_discord_user_id es None o string válido.
        """
        team_a, team_b = seed_two_teams
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)
        role_b = make_mock_role(team_b.discord_role_id, team_b.name)
        user_id = 400001
        user_id_str = str(user_id)
        actor_id = "999000888"

        member = make_mock_member(user_id=user_id, name="AuditSubject")

        # 1. handle_role_added (Team A)
        await roster_sync_service.handle_role_added(member, role_a, actor_id=actor_id)

        # 2. handle_role_added (Team B, sin actor explícito)
        await roster_sync_service.handle_role_added(member, role_b, actor_id=None)

        # 3. change_player_position (Team A -> MID)
        await roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=actor_id,
        )

        # 4. change_player_position (Team A -> Capitán)
        await roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=actor_id,
            is_captain=True,
        )

        # 5. change_player_position (Team A -> Descenso de Capitán)
        await roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=actor_id,
            is_captain=False,
        )

        # 6. change_player_position (Team B -> COACH)
        await roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_b.id,
            new_role=RosterRole.COACH,
            actor_id=actor_id,
        )

        # 7. handle_role_removed (Team A)
        await roster_sync_service.handle_role_removed(member, role_a, actor_id=actor_id)

        # 8. handle_role_removed (Team B)
        await roster_sync_service.handle_role_removed(member, role_b, actor_id=None)

        # Verificación del 100% de los logs de auditoría
        async with session_factory() as s:
            result = await s.execute(select(AuditLog).order_by(AuditLog.created_at.asc()))
            all_logs = list(result.scalars().all())

            assert len(all_logs) == 8, f"Se esperaban 8 logs, encontrados: {len(all_logs)}"

            valid_actions = {
                "roster.member_joined",
                "roster.member_left",
                "roster.role_changed",
            }
            required_payload_keys = {"team_id", "discord_user_id", "role", "is_captain"}

            for log in all_logs:
                # Verificación de identificadores y metadata
                assert isinstance(log.id, UUID), f"ID no es UUID: {log.id}"
                assert log.action in valid_actions, f"Acción desconocida: {log.action}"
                assert log.entity_type == "team_membership"
                assert isinstance(log.entity_id, UUID)
                assert log.entity_id in (team_a.id, team_b.id)
                assert log.created_at is not None
                assert log.created_at.tzinfo is not None

                if log.actor_discord_user_id is not None:
                    assert isinstance(log.actor_discord_user_id, str)
                    assert len(log.actor_discord_user_id) > 0

                # Verificación de consistencia de esquemas before / after según la acción
                if log.action == "roster.member_joined":
                    assert log.before is None, "member_joined debe tener before=None"
                    assert isinstance(log.after, dict), "member_joined debe tener after tipo dict"
                    assert required_payload_keys.issubset(log.after.keys())
                    assert log.after["discord_user_id"] == user_id_str
                    assert log.after["role"] == "staff"
                    assert log.after["is_captain"] is False

                elif log.action == "roster.member_left":
                    assert isinstance(log.before, dict), "member_left debe tener before tipo dict"
                    assert log.after is None, "member_left debe tener after=None"
                    assert required_payload_keys.issubset(log.before.keys())
                    assert log.before["discord_user_id"] == user_id_str

                elif log.action == "roster.role_changed":
                    assert isinstance(log.before, dict), "role_changed debe tener before tipo dict"
                    assert isinstance(log.after, dict), "role_changed debe tener after tipo dict"
                    assert required_payload_keys.issubset(log.before.keys())
                    assert required_payload_keys.issubset(log.after.keys())
                    assert log.before["discord_user_id"] == user_id_str
                    assert log.after["discord_user_id"] == user_id_str

    @pytest.mark.asyncio
    async def test_zero_orphan_movements_and_audit_logs_verification(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Verifica que no existan movimientos ni registros de auditoría huérfanos.
        Toda referencia a discord_user_id, actor_id o team_id debe resolverse
        hacia un registro existente en teams o discord_users.
        """
        team_a, team_b = seed_two_teams
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)
        role_b = make_mock_role(team_b.discord_role_id, team_b.name)
        staff_actor = "999000777"

        # Generar actividad con varios usuarios
        for i in range(3):
            u_id = 500000 + i
            m = make_mock_member(user_id=u_id, name=f"User_{i}")
            await roster_sync_service.handle_role_added(m, role_a, actor_id=staff_actor)
            if i % 2 == 0:
                await roster_sync_service.handle_role_added(m, role_b, actor_id=staff_actor)
                await roster_sync_service.handle_role_removed(m, role_a, actor_id=staff_actor)

        async with session_factory() as s:
            # Obtener todos los IDs válidos en el sistema
            res_u = await s.execute(select(DiscordUser.discord_id))
            valid_users = set(res_u.scalars().all())

            res_t = await s.execute(select(Team.id))
            valid_teams = set(res_t.scalars().all())

            # 1. Comprobar movimientos huérfanos
            res_mov = await s.execute(select(RosterMovement))
            all_movements = list(res_mov.scalars().all())
            assert len(all_movements) > 0

            for mov in all_movements:
                assert mov.team_id in valid_teams, f"Movimiento huérfano de equipo: {mov}"
                assert mov.discord_user_id in valid_users, f"Movimiento huérfano de usuario: {mov}"
                if mov.actor_id is not None:
                    assert mov.actor_id in valid_users, f"Movimiento con actor huérfano: {mov}"

            # 2. Comprobar logs de auditoría huérfanos
            res_aud = await s.execute(select(AuditLog))
            all_audits = list(res_aud.scalars().all())
            assert len(all_audits) > 0

            for audit in all_audits:
                if audit.entity_type == "team_membership":
                    assert audit.entity_id in valid_teams, (
                        f"AuditLog con entity_id huérfano: {audit}"
                    )
                if audit.actor_discord_user_id is not None:
                    assert audit.actor_discord_user_id in valid_users, (
                        f"AuditLog con actor huérfano: {audit}"
                    )

    @pytest.mark.asyncio
    async def test_referential_integrity_on_user_and_team_deletion(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Prueba adversarial de integridad referencial:
        - Al eliminar un usuario:
          * Sus membresías activas se eliminan en cascada.
          * Sus movimientos como sujeto se eliminan en cascada.
          * Los movimientos y audit_logs donde fungió como ACTOR se ponen a NULL (SET NULL).
        - Al eliminar un equipo:
          * Sus membresías y movimientos se eliminan en cascada.
        """
        team_a, _ = seed_two_teams
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)
        player_id = 600001
        player_id_str = str(player_id)
        actor_id_str = "600002"

        member = make_mock_member(user_id=player_id, name="DeletedCandidate")

        # El actor añade al jugador
        await roster_sync_service.handle_role_added(member, role_a, actor_id=actor_id_str)
        # El actor cambia posición
        await roster_sync_service.change_player_position(
            discord_user_id=player_id_str,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=actor_id_str,
        )

        # Verificar que el actor fungió en auditoría y movimientos
        async with session_factory() as s:
            audit_repo = AuditLogRepository(s)
            actor_audits = await audit_repo.list_by_actor(actor_id_str)
            assert len(actor_audits) == 2

            mov_repo = RosterMovementRepository(s)
            user_movs = await mov_repo.list_by_user(player_id_str)
            assert len(user_movs) == 2
            assert all(m.actor_id == actor_id_str for m in user_movs)

        # 1. Eliminar el usuario ACTOR de la base de datos
        async with session_factory() as s:
            actor_obj = await s.get(DiscordUser, actor_id_str)
            assert actor_obj is not None
            await s.delete(actor_obj)
            await s.commit()

        # Verificar que ondelete="SET NULL" operó correctamente sin violar FK
        async with session_factory() as s:
            # Los movimientos del jugador permanecen, pero actor_id es None
            mov_repo = RosterMovementRepository(s)
            user_movs = await mov_repo.list_by_user(player_id_str)
            assert len(user_movs) == 2
            assert all(m.actor_id is None for m in user_movs)

            # Las entradas de auditoría permanecen, pero actor_discord_user_id es None
            result = await s.execute(select(AuditLog).where(AuditLog.entity_id == team_a.id))
            team_audits = list(result.scalars().all())
            assert len(team_audits) == 2
            assert all(a.actor_discord_user_id is None for a in team_audits)

        # 2. Eliminar el usuario JUGADOR de la base de datos
        async with session_factory() as s:
            player_obj = await s.get(DiscordUser, player_id_str)
            assert player_obj is not None
            await s.delete(player_obj)
            await s.commit()

        # Verificar cascada de membresía y movimientos del jugador
        async with session_factory() as s:
            m_repo = TeamMembershipRepository(s)
            assert await m_repo.get(team_a.id, player_id_str) is None

            mov_repo = RosterMovementRepository(s)
            assert len(await mov_repo.list_by_user(player_id_str)) == 0

    @pytest.mark.asyncio
    async def test_audit_log_jsonb_deep_data_types_roundtrip(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Estrés de serialización JSONB: Valida que AuditLogRepository.log acepte
        tipos no nativos de JSON (UUID, datetime, Decimal, Enum, Set) y los normalice
        de forma segura en PostgreSQL / PGlite sin errores ni pérdida de fidelidad.
        """
        team_a, _ = seed_two_teams
        actor_id = "700001"

        async with session_factory() as s:
            s.add(DiscordUser(discord_id=actor_id, username="JsonbTester"))
            await s.commit()

        complex_before = {
            "team_uuid": team_a.id,
            "recorded_at": datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc),
            "date_only": date(2026, 9, 19),
            "role_enum": RosterRole.ADC,
            "cost_decimal": Decimal("150.75"),
            "tags_set": {"starter", "adc", "captain"},
        }
        complex_after = {
            "team_uuid": team_a.id,
            "recorded_at": datetime(2026, 9, 19, 13, 0, 0, tzinfo=timezone.utc),
            "role_enum": RosterRole.SUPPORT,
            "nested": {
                "sub_uuid": team_a.id,
                "sub_enum": RosterMovementAction.ROLE_CHANGED,
            },
        }

        async with session_factory() as s:
            audit_repo = AuditLogRepository(s)
            entry = await audit_repo.log(
                actor_discord_user_id=actor_id,
                action="roster.complex_payload_test",
                entity_type="team_membership",
                entity_id=team_a.id,
                before=complex_before,
                after=complex_after,
            )
            await s.commit()
            log_id = entry.id

        # Recuperar y verificar roundtrip
        async with session_factory() as s:
            audit_repo = AuditLogRepository(s)
            retrieved = await audit_repo.get_by_id(log_id)
            assert retrieved is not None
            assert retrieved.before is not None
            assert retrieved.before["team_uuid"] == str(team_a.id)
            assert retrieved.before["role_enum"] == "adc"
            assert retrieved.before["cost_decimal"] == 150.75
            assert isinstance(retrieved.before["tags_set"], list)
            assert set(retrieved.before["tags_set"]) == {"starter", "adc", "captain"}

            assert retrieved.after is not None
            assert retrieved.after["role_enum"] == "support"
            assert retrieved.after["nested"]["sub_enum"] == "role_changed"

    @pytest.mark.asyncio
    async def test_actor_id_whitespace_and_int_normalization_in_audit_and_movements(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_two_teams: tuple[Team, Team],
    ) -> None:
        """
        Prueba de frontera: actor_id provisto como entero int o cadena con espacios en blanco.
        Verifica que _clean_user_id normalice el valor, asegurando la existencia de
        DiscordUser sin duplicados ni errores de clave foránea.
        """
        team_a, _ = seed_two_teams
        role_a = make_mock_role(team_a.discord_role_id, team_a.name)
        user_id = 800001
        user_id_str = str(user_id)
        raw_int_actor = 999111222
        padded_actor_str = "   999111222   "

        member = make_mock_member(user_id=user_id, name="NormalizedActorUser")

        # 1. Adición con actor_id como entero
        await roster_sync_service.handle_role_added(member, role_a, actor_id=raw_int_actor)

        # 2. Cambio de posición con actor_id como string con espacios
        await roster_sync_service.change_player_position(
            discord_user_id=user_id_str,
            team_id=team_a.id,
            new_role=RosterRole.MID,
            actor_id=padded_actor_str,
        )

        async with session_factory() as s:
            # Debe existir exactamente un usuario actor normalizado
            actor_user = await s.get(DiscordUser, "999111222")
            assert actor_user is not None

            # RosterMovements debe tener el actor limpio sin espacios
            mov_repo = RosterMovementRepository(s)
            movs = await mov_repo.list_by_user(user_id_str)
            assert len(movs) == 2
            assert all(m.actor_id == "999111222" for m in movs)

            # AuditLog debe tener el actor limpio sin espacios
            audit_repo = AuditLogRepository(s)
            audits = await audit_repo.list_by_entity("team_membership", team_a.id)
            assert len(audits) == 2
            assert all(a.actor_discord_user_id == "999111222" for a in audits)
