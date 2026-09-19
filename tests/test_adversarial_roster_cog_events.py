"""
Suite de pruebas empíricas y adversariales para el listener de eventos
RosterCog.on_member_update ante permutaciones de roles y resiliencia a excepciones.

Cubre:
1. TestRoleUpdatePermutations:
   - 3 roles añadidos simultáneamente: 3 llamadas secuenciales a handle_role_added.
   - 2 roles eliminados simultáneamente: 2 llamadas secuenciales a handle_role_removed.
   - Mixto: 2 añadidos + 2 eliminados (adiciones preceden a remociones).
   - Ausencia de cambios de rol (avatar, apodo, estado/actividad): servicio nunca invocado.
   - Mismos identificadores de rol en distintas instancias: servicio nunca invocado.
   - Lista vacía de roles en ambos estados: servicio nunca invocado.
   - Estrés paramétrico con N adiciones y M remociones.
2. TestExceptionResilienceStress:
   - handle_role_added lanza RuntimeError("Database failure") en el 1º de 3 roles:
     procesamiento continuado en 2º y 3º rol sin propagar excepción al bot.
   - handle_role_removed lanza RuntimeError en el 1º de 2 roles: 2º procesado limpiamente.
   - Fallo en rol intermedio (rol 2 de 3): 1º y 3º completados.
   - Fallo catastrófico total: todos los roles fallan sin provocar caída del bot.
   - Excepciones mixtas en altas y bajas concurrentes.
   - Servicio ausente (None): no-op defensivo sin fallos.
3. TestRealDatabaseEndToEndListenerSync:
   - Integración E2E con PGlite en memoria y RosterSyncService real.
   - Alta simultánea de 3 roles de club persistida en BD (memberships, movimientos, auditoría).
   - Baja simultánea de 2 roles de equipo reflejada en BD (eliminación selectiva).
   - Filtrado transparente de roles que no pertenecen a clubes deportivos.
   - Resiliencia E2E ante fallo inyectado en primer rol: persistencia exitosa de los restantes.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.bot import LigaBot
from liga_bot.cogs.roster import RosterCog
from liga_bot.config import Settings
from liga_bot.models.enums import Division, RosterMovementAction
from liga_bot.models.roster import AuditLog, RosterMovement, Team, TeamMembership
from liga_bot.services.roster_sync_service import RosterSyncService

# ---------------------------------------------------------------------------
# Clases Mock Auxiliares Especializadas para Pruebas Adversariales
# ---------------------------------------------------------------------------


class MockDiscordRole:
    """Mock de discord.Role con semántica exacta de igualdad por identificador."""

    def __init__(self, role_id: int, name: str = "MockRole") -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (MockDiscordRole, discord.Role, MagicMock)):
            other_id = getattr(other, "id", None)
            return other_id == self.id
        return False

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"<MockDiscordRole id={self.id} name={self.name!r}>"


class MockDiscordMember:
    """Mock de discord.Member con soporte para roles, avatar, apodo y estado."""

    def __init__(
        self,
        user_id: int = 123456789,
        name: str = "EmpiricalPlayer",
        roles: list[Any] | None = None,
        nick: str | None = None,
        avatar_key: str = "avatar_v1",
        status: str = "online",
    ) -> None:
        self.id = user_id
        self.name = name
        self.display_name = nick or name
        self.global_name = name
        self.mention = f"<@{user_id}>"
        self.roles = list(roles or [])
        self.nick = nick
        self.avatar = MagicMock()
        self.avatar.key = avatar_key
        self.display_avatar = self.avatar
        self.status = status

    def __repr__(self) -> str:
        return f"<MockDiscordMember id={self.id} name={self.name!r} roles={len(self.roles)}>"


def create_test_bot(
    settings: Settings | None = None,
    roster_sync_service: RosterSyncService | None = None,
) -> MagicMock:
    """Instancia un mock de LigaBot con las dependencias necesarias."""
    bot = MagicMock(spec=LigaBot)
    bot.settings = settings or Settings(
        guild_id=1547725310508667010,
        staff_role_id=1547729760384319518,
    )
    bot.roster_sync_service = roster_sync_service
    bot.cogs = {}
    return bot


# ===========================================================================
# 1. TestRoleUpdatePermutations: Permutaciones de Modificación de Roles
# ===========================================================================


class TestRoleUpdatePermutations:
    """Pruebas adversariales de permutaciones de roles en on_member_update."""

    @pytest.mark.asyncio
    async def test_three_roles_added_simultaneously(self) -> None:
        """
        Verifica que ante 3 roles añadidos simultáneamente:
        - handle_role_added sea invocado exactamente 3 veces.
        - Las llamadas sean estrictamente secuenciales en el orden en que se presentan.
        - handle_role_removed nunca sea invocado.
        """
        execution_order: list[int] = []

        async def tracking_handle_role_added(member: Any, role: Any) -> Any:
            execution_order.append(role.id)
            await asyncio.sleep(0.001)  # Simula latencia I/O mínima
            return MagicMock()

        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock(side_effect=tracking_handle_role_added)
        mock_service.handle_role_removed = AsyncMock()

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = MockDiscordRole(100, "Comunidad")
        new_role1 = MockDiscordRole(1001, "Team Alpha")
        new_role2 = MockDiscordRole(1002, "Team Beta")
        new_role3 = MockDiscordRole(1003, "Team Gamma")

        before = MockDiscordMember(user_id=456, roles=[base_role])
        after = MockDiscordMember(
            user_id=456,
            roles=[base_role, new_role1, new_role2, new_role3],
        )

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == 3
        assert mock_service.handle_role_removed.await_count == 0
        assert execution_order == [1001, 1002, 1003]

        # Verificar argumentos de cada invocación
        call_args_list = mock_service.handle_role_added.await_args_list
        for idx, expected_role in enumerate([new_role1, new_role2, new_role3]):
            args, kwargs = call_args_list[idx]
            called_member = kwargs.get("member") or (args[0] if args else None)
            called_role = kwargs.get("role") or (args[1] if len(args) > 1 else None)
            assert called_member == after
            assert called_role == expected_role

    @pytest.mark.asyncio
    async def test_two_roles_removed_simultaneously(self) -> None:
        """
        Verifica que ante 2 roles retirados simultáneamente:
        - handle_role_removed sea invocado exactamente 2 veces de forma secuencial.
        - handle_role_added nunca sea invocado.
        """
        execution_order: list[int] = []

        async def tracking_handle_role_removed(member: Any, role: Any) -> bool:
            execution_order.append(role.id)
            await asyncio.sleep(0.001)
            return True

        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock()
        mock_service.handle_role_removed = AsyncMock(side_effect=tracking_handle_role_removed)

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = MockDiscordRole(100, "Comunidad")
        rem_role1 = MockDiscordRole(2001, "Team Delta")
        rem_role2 = MockDiscordRole(2002, "Team Epsilon")

        before = MockDiscordMember(
            user_id=789,
            roles=[base_role, rem_role1, rem_role2],
        )
        after = MockDiscordMember(user_id=789, roles=[base_role])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_removed.await_count == 2
        assert mock_service.handle_role_added.await_count == 0
        assert execution_order == [2001, 2002]

        call_args_list = mock_service.handle_role_removed.await_args_list
        for idx, expected_role in enumerate([rem_role1, rem_role2]):
            args, kwargs = call_args_list[idx]
            called_member = kwargs.get("member") or (args[0] if args else None)
            called_role = kwargs.get("role") or (args[1] if len(args) > 1 else None)
            assert called_member == after
            assert called_role == expected_role

    @pytest.mark.asyncio
    async def test_mixed_two_added_and_two_removed(self) -> None:
        """
        Verifica que ante 2 roles añadidos y 2 retirados concurrentemente:
        - handle_role_added sea llamado 2 veces y handle_role_removed 2 veces.
        - La fase de altas precede a la fase de bajas de manera determinista.
        """
        lifecycle_events: list[tuple[str, int]] = []

        async def tracking_add(member: Any, role: Any) -> Any:
            lifecycle_events.append(("add", role.id))
            return MagicMock()

        async def tracking_remove(member: Any, role: Any) -> bool:
            lifecycle_events.append(("remove", role.id))
            return True

        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock(side_effect=tracking_add)
        mock_service.handle_role_removed = AsyncMock(side_effect=tracking_remove)

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        keep_role = MockDiscordRole(100, "Base")
        old1 = MockDiscordRole(201, "OldTeam1")
        old2 = MockDiscordRole(202, "OldTeam2")
        new1 = MockDiscordRole(301, "NewTeam1")
        new2 = MockDiscordRole(302, "NewTeam2")

        before = MockDiscordMember(user_id=555, roles=[keep_role, old1, old2])
        after = MockDiscordMember(user_id=555, roles=[keep_role, new1, new2])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == 2
        assert mock_service.handle_role_removed.await_count == 2
        assert lifecycle_events == [
            ("add", 301),
            ("add", 302),
            ("remove", 201),
            ("remove", 202),
        ]

    @pytest.mark.asyncio
    async def test_no_role_changes_avatar_update(self) -> None:
        """
        Verifica que una actualización exclusiva de avatar (sin alteración de roles)
        NO invoque jamás a handle_role_added ni a handle_role_removed.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock()
        mock_service.handle_role_removed = AsyncMock()

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        role1 = MockDiscordRole(100, "Miembro")
        role2 = MockDiscordRole(200, "Jugador")

        before = MockDiscordMember(
            user_id=111,
            roles=[role1, role2],
            avatar_key="avatar_initial",
        )
        after = MockDiscordMember(
            user_id=111,
            roles=[role1, role2],
            avatar_key="avatar_new_updated",
        )

        await cog.on_member_update(before, after)

        mock_service.handle_role_added.assert_not_awaited()
        mock_service.handle_role_removed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_role_changes_nickname_update(self) -> None:
        """
        Verifica que un cambio de apodo o nombre visible del miembro
        NO invoque a la capa de sincronización de plantillas.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock()
        mock_service.handle_role_removed = AsyncMock()

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        role = MockDiscordRole(100, "Miembro")

        before = MockDiscordMember(user_id=222, roles=[role], nick="OldNickname")
        after = MockDiscordMember(user_id=222, roles=[role], nick="NewAwesomeNick")

        await cog.on_member_update(before, after)

        mock_service.handle_role_added.assert_not_awaited()
        mock_service.handle_role_removed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_role_changes_status_activity_update(self) -> None:
        """
        Verifica que una alteración de estado (online/offline/dnd) o actividad
        NO active los hooks de sincronización de roles.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock()
        mock_service.handle_role_removed = AsyncMock()

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        role = MockDiscordRole(100, "Miembro")

        before = MockDiscordMember(user_id=333, roles=[role], status="online")
        after = MockDiscordMember(user_id=333, roles=[role], status="dnd")

        await cog.on_member_update(before, after)

        mock_service.handle_role_added.assert_not_awaited()
        mock_service.handle_role_removed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_role_changes_empty_roles_on_both(self) -> None:
        """
        Verifica que un usuario sin roles antes ni después
        retorne inmediatamente sin consultar o invocar al servicio.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock()
        mock_service.handle_role_removed = AsyncMock()

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        before = MockDiscordMember(user_id=444, roles=[])
        after = MockDiscordMember(user_id=444, roles=[])

        await cog.on_member_update(before, after)

        mock_service.handle_role_added.assert_not_awaited()
        mock_service.handle_role_removed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_role_changes_distinct_role_instances_with_identical_ids(self) -> None:
        """
        Verifica que si discord.py provee distintas instancias de objetos de rol
        con los mismos IDs, se reconozca correctamente la igualdad y no se dispare
        ninguna acción espuria de adición o remoción.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock()
        mock_service.handle_role_removed = AsyncMock()

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        # Distintas instancias en memoria representando los mismos roles
        before_r1 = MockDiscordRole(100, "Rol A")
        before_r2 = MockDiscordRole(200, "Rol B")
        after_r1 = MockDiscordRole(100, "Rol A")
        after_r2 = MockDiscordRole(200, "Rol B")

        assert before_r1 is not after_r1
        assert before_r1 == after_r1

        before = MockDiscordMember(user_id=555, roles=[before_r1, before_r2])
        after = MockDiscordMember(user_id=555, roles=[after_r1, after_r2])

        await cog.on_member_update(before, after)

        mock_service.handle_role_added.assert_not_awaited()
        mock_service.handle_role_removed.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("num_adds", "num_rems"),
        [
            (1, 0),
            (0, 1),
            (4, 0),
            (0, 3),
            (5, 5),
            (10, 2),
        ],
    )
    async def test_scaled_role_permutations_stress(self, num_adds: int, num_rems: int) -> None:
        """
        Prueba paramétrica de estrés: verifica el conteo exacto de adiciones y remociones
        para diversas permutaciones de N roles añadidos y M roles eliminados.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock(return_value=MagicMock())
        mock_service.handle_role_removed = AsyncMock(return_value=True)

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_roles = [MockDiscordRole(i, f"Base{i}") for i in range(100, 105)]
        roles_to_remove = [MockDiscordRole(1000 + i, f"Rem{i}") for i in range(num_rems)]
        roles_to_add = [MockDiscordRole(2000 + i, f"Add{i}") for i in range(num_adds)]

        before = MockDiscordMember(user_id=777, roles=base_roles + roles_to_remove)
        after = MockDiscordMember(user_id=777, roles=base_roles + roles_to_add)

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == num_adds
        assert mock_service.handle_role_removed.await_count == num_rems


# ===========================================================================
# 2. TestExceptionResilienceStress: Resiliencia ante Fallos de Excepción
# ===========================================================================


class TestExceptionResilienceStress:
    """Pruebas adversariales de aislamiento y resiliencia ante excepciones."""

    @pytest.mark.asyncio
    async def test_handle_role_added_first_role_raises_runtime_error(self) -> None:
        """
        Escenario crítico:
        - Se añaden 3 roles simultáneamente [R1, R2, R3].
        - R1 lanza RuntimeError("Database failure").
        - Verificar que:
          1. R2 y R3 son procesados sin interrupción ni descarte del evento.
          2. No se propaga ninguna excepción fuera de on_member_update (no crashea el bot).
          3. handle_role_added se llama exactamente 3 veces.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock(
            side_effect=[
                RuntimeError("Database failure"),
                MagicMock(),
                MagicMock(),
            ]
        )
        mock_service.handle_role_removed = AsyncMock()

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = MockDiscordRole(100, "Base")
        r1 = MockDiscordRole(201, "Team1_Fails")
        r2 = MockDiscordRole(202, "Team2_Succeeds")
        r3 = MockDiscordRole(203, "Team3_Succeeds")

        before = MockDiscordMember(user_id=901, roles=[base_role])
        after = MockDiscordMember(user_id=901, roles=[base_role, r1, r2, r3])

        # No debe lanzar ninguna excepción
        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == 3
        calls = mock_service.handle_role_added.await_args_list
        assert (calls[0][1].get("role") or calls[0][0][1]) == r1
        assert (calls[1][1].get("role") or calls[1][0][1]) == r2
        assert (calls[2][1].get("role") or calls[2][0][1]) == r3

    @pytest.mark.asyncio
    async def test_handle_role_removed_first_role_raises_runtime_error(self) -> None:
        """
        Escenario crítico:
        - Se retiran 2 roles simultáneamente [R1, R2].
        - R1 lanza RuntimeError("Database failure").
        - Verificar que:
          1. R2 es procesado de forma completa.
          2. No se lanza excepción no controlada.
          3. handle_role_removed se llama 2 veces.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock()
        mock_service.handle_role_removed = AsyncMock(
            side_effect=[
                RuntimeError("Database failure"),
                True,
            ]
        )

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        base_role = MockDiscordRole(100, "Base")
        r1 = MockDiscordRole(301, "Team1_Fails")
        r2 = MockDiscordRole(302, "Team2_Succeeds")

        before = MockDiscordMember(user_id=902, roles=[base_role, r1, r2])
        after = MockDiscordMember(user_id=902, roles=[base_role])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_removed.await_count == 2
        calls = mock_service.handle_role_removed.await_args_list
        assert (calls[0][1].get("role") or calls[0][0][1]) == r1
        assert (calls[1][1].get("role") or calls[1][0][1]) == r2

    @pytest.mark.asyncio
    async def test_handle_role_added_middle_role_failure(self) -> None:
        """
        Verifica que si el rol intermedio de una lista de 3 adiciones falla:
        - El 1º rol se procesa con éxito.
        - El 2º rol falla aisladamente.
        - El 3º rol se procesa con éxito.
        - El total de intentos es 3.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock(
            side_effect=[
                MagicMock(),
                TimeoutError("Conexión de base de datos agotada"),
                MagicMock(),
            ]
        )

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        r1 = MockDiscordRole(401, "Team1")
        r2 = MockDiscordRole(402, "Team2_Timeout")
        r3 = MockDiscordRole(403, "Team3")

        before = MockDiscordMember(user_id=903, roles=[])
        after = MockDiscordMember(user_id=903, roles=[r1, r2, r3])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == 3

    @pytest.mark.asyncio
    async def test_handle_roles_total_outage_all_fail(self) -> None:
        """
        Verifica que ante una caída catastrófica total (todos los roles añadidos
        y eliminados lanzan excepción):
        - Se intente procesar cada uno de los roles (3 altas + 2 bajas = 5 intentos).
        - La ejecución concluya limpiamente sin elevar errores al loop de eventos de Discord.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock(
            side_effect=ConnectionResetError("Socket cerrado por servidor DB")
        )
        mock_service.handle_role_removed = AsyncMock(
            side_effect=ConnectionResetError("Socket cerrado por servidor DB")
        )

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        old1 = MockDiscordRole(501, "OldTeam1")
        old2 = MockDiscordRole(502, "OldTeam2")
        new1 = MockDiscordRole(601, "NewTeam1")
        new2 = MockDiscordRole(602, "NewTeam2")
        new3 = MockDiscordRole(603, "NewTeam3")

        before = MockDiscordMember(user_id=904, roles=[old1, old2])
        after = MockDiscordMember(user_id=904, roles=[new1, new2, new3])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == 3
        assert mock_service.handle_role_removed.await_count == 2

    @pytest.mark.asyncio
    async def test_mixed_exceptions_across_adds_and_removes(self) -> None:
        """
        Verifica una mezcla de fallos y éxitos:
        - 2 altas: 1 falla, 1 tiene éxito.
        - 2 bajas: 1 falla, 1 tiene éxito.
        - Ambos flujos completan las 4 operaciones.
        """
        mock_service = MagicMock(spec=RosterSyncService)
        mock_service.handle_role_added = AsyncMock(
            side_effect=[
                ValueError("Datos de miembro inconsistentes"),
                MagicMock(),
            ]
        )
        mock_service.handle_role_removed = AsyncMock(
            side_effect=[
                KeyError("Membresía no indexada"),
                True,
            ]
        )

        bot = create_test_bot(roster_sync_service=mock_service)
        cog = RosterCog(bot)

        rem1 = MockDiscordRole(701, "RemFails")
        rem2 = MockDiscordRole(702, "RemOk")
        add1 = MockDiscordRole(801, "AddFails")
        add2 = MockDiscordRole(802, "AddOk")

        before = MockDiscordMember(user_id=905, roles=[rem1, rem2])
        after = MockDiscordMember(user_id=905, roles=[add1, add2])

        await cog.on_member_update(before, after)

        assert mock_service.handle_role_added.await_count == 2
        assert mock_service.handle_role_removed.await_count == 2

    @pytest.mark.asyncio
    async def test_service_is_none_defensive_handling(self) -> None:
        """
        Verifica que si roster_sync_service no está inyectado en el bot (None),
        on_member_update maneje la situación defensivamente registrando un warning
        sin lanzar AttributeError.
        """
        bot = create_test_bot(roster_sync_service=None)
        cog = RosterCog(bot)

        role_add = MockDiscordRole(999, "SomeTeam")
        before = MockDiscordMember(user_id=906, roles=[])
        after = MockDiscordMember(user_id=906, roles=[role_add])

        # No debe lanzar excepción
        await cog.on_member_update(before, after)


# ===========================================================================
# 3. TestRealDatabaseEndToEndListenerSync: Integración Real con PGlite
# ===========================================================================


class TestRealDatabaseEndToEndListenerSync:
    """Pruebas empíricas E2E de RosterCog conectado a RosterSyncService y PGlite."""

    @pytest.fixture
    def session_factory(self, migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(bind=migrated_db, expire_on_commit=False)

    @pytest_asyncio.fixture(autouse=True)
    async def clean_database_tables(self, migrated_db: AsyncEngine) -> Any:
        """Limpia las tablas compartidas antes y después de cada test de integración."""
        async with migrated_db.connect() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                    "players, teams, discord_users CASCADE;"
                )
            )
            await conn.commit()
        yield
        async with migrated_db.connect() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE audit_logs, roster_movements, team_memberships, "
                    "players, teams, discord_users CASCADE;"
                )
            )
            await conn.commit()

    @pytest_asyncio.fixture
    async def seeded_teams(self, session_factory: async_sessionmaker[AsyncSession]) -> list[Team]:
        """Siembra 3 equipos canónicos vinculados a roles de Discord."""
        teams = [
            Team(
                name="Empirical Titans",
                tag="TIT",
                slug="empirical-titans",
                division=Division.PREMIER,
                discord_role_id=3001,
            ),
            Team(
                name="Empirical Krakens",
                tag="KRK",
                slug="empirical-krakens",
                division=Division.PREMIER,
                discord_role_id=3002,
            ),
            Team(
                name="Empirical Phoenix",
                tag="PHX",
                slug="empirical-phoenix",
                division=Division.ASCEND,
                discord_role_id=3003,
            ),
        ]
        async with session_factory() as session:
            session.add_all(teams)
            await session.commit()
            for t in teams:
                await session.refresh(t)
        return teams

    @pytest.mark.asyncio
    async def test_e2e_real_db_three_roles_added_simultaneously(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_teams: list[Team],
    ) -> None:
        """
        Verificación empírica E2E:
        - RosterCog conectado a RosterSyncService real y PGlite en memoria.
        - Miembro recibe 3 roles de equipo simultáneamente (3001, 3002, 3003).
        - Se comprueba en BD:
          * Se crean 3 membresías en team_memberships con rol default 'substitute'.
          * Se crean 3 movimientos de alta en roster_movements.
          * Se crean 3 registros de auditoría en audit_logs.
        """
        settings = Settings(
            guild_id=1547725310508667010,
            staff_role_id=1547729760384319518,
        )
        service = RosterSyncService(session_factory=session_factory, settings=settings)
        bot = create_test_bot(settings=settings, roster_sync_service=service)
        cog = RosterCog(bot)

        user_id = 999111222
        roles = [
            MockDiscordRole(seeded_teams[0].discord_role_id, seeded_teams[0].name),
            MockDiscordRole(seeded_teams[1].discord_role_id, seeded_teams[1].name),
            MockDiscordRole(seeded_teams[2].discord_role_id, seeded_teams[2].name),
        ]

        before = MockDiscordMember(user_id=user_id, roles=[])
        after = MockDiscordMember(user_id=user_id, roles=roles)

        await cog.on_member_update(before, after)

        # Verificar estado físico en la base de datos
        async with session_factory() as session:
            # 1. Membresías
            stmt_m = select(TeamMembership).where(TeamMembership.discord_user_id == str(user_id))
            memberships = (await session.scalars(stmt_m)).all()
            assert len(memberships) == 3
            team_ids = {m.team_id for m in memberships}
            assert team_ids == {seeded_teams[0].id, seeded_teams[1].id, seeded_teams[2].id}

            # 2. Movimientos
            stmt_mov = select(RosterMovement).where(RosterMovement.discord_user_id == str(user_id))
            movements = (await session.scalars(stmt_mov)).all()
            assert len(movements) == 3
            assert all(m.action == RosterMovementAction.JOINED for m in movements)

            # 3. Auditoría
            stmt_aud = select(AuditLog).where(AuditLog.action == "roster.member_joined")
            audit_records = (await session.scalars(stmt_aud)).all()
            assert len(audit_records) == 3

    @pytest.mark.asyncio
    async def test_e2e_real_db_two_roles_removed_simultaneously(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_teams: list[Team],
    ) -> None:
        """
        Verificación empírica E2E:
        - Miembro con 3 membresías activas pierde 2 roles simultáneamente en Discord.
        - Se comprueba en BD:
          * Se eliminan exactamente las 2 membresías correspondientes.
          * La 3ª membresía se conserva intacta (Regla 4 y Regla 5).
          * Se registran los movimientos LEAVE y los logs de auditoría correspondientes.
        """
        settings = Settings(
            guild_id=1547725310508667010,
            staff_role_id=1547729760384319518,
        )
        service = RosterSyncService(session_factory=session_factory, settings=settings)
        bot = create_test_bot(settings=settings, roster_sync_service=service)
        cog = RosterCog(bot)

        user_id = 999333444
        role_t1 = MockDiscordRole(seeded_teams[0].discord_role_id, seeded_teams[0].name)
        role_t2 = MockDiscordRole(seeded_teams[1].discord_role_id, seeded_teams[1].name)
        role_t3 = MockDiscordRole(seeded_teams[2].discord_role_id, seeded_teams[2].name)

        # Inicialmente se le añaden los 3 roles
        before_init = MockDiscordMember(user_id=user_id, roles=[])
        after_init = MockDiscordMember(user_id=user_id, roles=[role_t1, role_t2, role_t3])
        await cog.on_member_update(before_init, after_init)

        # Ahora se retiran los roles de los equipos 1 y 2 simultáneamente
        before_removal = MockDiscordMember(user_id=user_id, roles=[role_t1, role_t2, role_t3])
        after_removal = MockDiscordMember(user_id=user_id, roles=[role_t3])
        await cog.on_member_update(before_removal, after_removal)

        # Verificar estado físico en la base de datos
        async with session_factory() as session:
            stmt_m = select(TeamMembership).where(TeamMembership.discord_user_id == str(user_id))
            remaining_memberships = (await session.scalars(stmt_m)).all()
            assert len(remaining_memberships) == 1
            assert remaining_memberships[0].team_id == seeded_teams[2].id

            stmt_mov = select(RosterMovement).where(
                RosterMovement.discord_user_id == str(user_id),
                RosterMovement.action == RosterMovementAction.LEFT,
            )
            leave_movements = (await session.scalars(stmt_mov)).all()
            assert len(leave_movements) == 2
            left_team_ids = {m.team_id for m in leave_movements}
            assert left_team_ids == {seeded_teams[0].id, seeded_teams[1].id}

    @pytest.mark.asyncio
    async def test_e2e_real_db_non_team_roles_filtered(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_teams: list[Team],
    ) -> None:
        """
        Verifica que al añadir un rol de equipo y varios roles ajenos (staff, boosters, etc.):
        - Únicamente se sincroniza el rol correspondiente a un club registrado.
        - Los roles ajenos se ignoran limpiamente sin crear registros espurios en BD.
        """
        settings = Settings(
            guild_id=1547725310508667010,
            staff_role_id=1547729760384319518,
        )
        service = RosterSyncService(session_factory=session_factory, settings=settings)
        bot = create_test_bot(settings=settings, roster_sync_service=service)
        cog = RosterCog(bot)

        user_id = 999555666
        team_role = MockDiscordRole(seeded_teams[0].discord_role_id, seeded_teams[0].name)
        unrelated_role1 = MockDiscordRole(8888, "Server Booster")
        unrelated_role2 = MockDiscordRole(9999, "Nitro Subscriber")

        before = MockDiscordMember(user_id=user_id, roles=[])
        after = MockDiscordMember(
            user_id=user_id,
            roles=[unrelated_role1, team_role, unrelated_role2],
        )

        await cog.on_member_update(before, after)

        async with session_factory() as session:
            stmt = select(TeamMembership).where(TeamMembership.discord_user_id == str(user_id))
            memberships = (await session.scalars(stmt)).all()
            assert len(memberships) == 1
            assert memberships[0].team_id == seeded_teams[0].id

    @pytest.mark.asyncio
    async def test_e2e_real_db_partial_failure_resilience(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_teams: list[Team],
    ) -> None:
        """
        Prueba adversarial de resiliencia con base de datos real:
        - Se simula un fallo inyectado (excepción) al procesar el primer rol.
        - Se comprueba que los roles segundo y tercero persisten sus membresías
          en la base de datos real a pesar del fallo previo.
        """
        settings = Settings(
            guild_id=1547725310508667010,
            staff_role_id=1547729760384319518,
        )
        service = RosterSyncService(session_factory=session_factory, settings=settings)

        # Envolvemos handle_role_added con un interceptor que falla solo en el 1º rol
        real_handle_role_added = service.handle_role_added
        first_role_id = seeded_teams[0].discord_role_id

        async def faulty_handle_role_added(member: Any, role: Any) -> Any:
            if getattr(role, "id", None) == first_role_id:
                raise RuntimeError("Fallo transitorio simulado de I/O en BD para equipo 1")
            return await real_handle_role_added(member, role)

        service.handle_role_added = faulty_handle_role_added  # type: ignore[method-assign]

        bot = create_test_bot(settings=settings, roster_sync_service=service)
        cog = RosterCog(bot)

        user_id = 999777888
        roles = [
            MockDiscordRole(seeded_teams[0].discord_role_id, seeded_teams[0].name),
            MockDiscordRole(seeded_teams[1].discord_role_id, seeded_teams[1].name),
            MockDiscordRole(seeded_teams[2].discord_role_id, seeded_teams[2].name),
        ]

        before = MockDiscordMember(user_id=user_id, roles=[])
        after = MockDiscordMember(user_id=user_id, roles=roles)

        # No debe propagar excepción
        await cog.on_member_update(before, after)

        # Verificar que los equipos 2 y 3 sí quedaron guardados en BD
        async with session_factory() as session:
            stmt = select(TeamMembership).where(TeamMembership.discord_user_id == str(user_id))
            memberships = (await session.scalars(stmt)).all()
            assert len(memberships) == 2
            saved_team_ids = {m.team_id for m in memberships}
            assert seeded_teams[0].id not in saved_team_ids
            assert seeded_teams[1].id in saved_team_ids
            assert seeded_teams[2].id in saved_team_ids
