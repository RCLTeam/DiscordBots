"""
Suite de pruebas exhaustivas para R2 y R3 (Milestone 2):
1. TeamMembershipRepository.insert_if_not_exists (ON CONFLICT DO NOTHING)
2. TeamMembershipRepository.upsert (ON CONFLICT DO UPDATE)
3. RosterSyncService.handle_role_added: preservación de rol/posición competitiva
4. RoleService.confirm_role_request: desacoplamiento transaccional estricto
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.models.enums import (
    Division,
    RoleRequestStatus,
    RosterMovementAction,
    RosterRole,
)
from liga_bot.models.roster import (
    DiscordUser,
    TeamMembership,
)
from liga_bot.models.team import Team
from liga_bot.repositories.role_request_repo import RoleRequestRepository
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
)
from liga_bot.services.role_service import RoleService
from liga_bot.services.roster_sync_service import RosterSyncService


# ===========================================================================
# Helpers y Mocks
# ===========================================================================
def create_mock_role(role_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.Role."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_member(
    user_id: int,
    name: str = "Player",
    display_name: str | None = None,
    roles: list[MagicMock] | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.Member con métodos de gestión de roles."""
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.global_name = name
    member.display_name = display_name or name
    member.roles = list(roles or [])
    member.guild = guild
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.edit = AsyncMock()
    member.send = AsyncMock()
    return member


def create_mock_guild(
    settings: Settings,
    roles: list[MagicMock] | None = None,
) -> MagicMock:
    """Crea un mock de discord.Guild con roles y miembros configurados."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = settings.guild_id
    guild.name = "RCL League Server"

    default_role = create_mock_role(settings.guild_id, "@everyone")
    guild.default_role = default_role

    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999999999999
    bot_member.name = "LigaBot"
    guild.me = bot_member

    staff_role = (
        create_mock_role(settings.staff_role_id, "Staff") if settings.staff_role_id else None
    )
    sin_verificar_role = (
        create_mock_role(settings.sin_verificar_role_id, "Sin Verificar")
        if settings.sin_verificar_role_id
        else None
    )

    all_roles = [default_role]
    if staff_role:
        all_roles.append(staff_role)
    if sin_verificar_role:
        all_roles.append(sin_verificar_role)
    if roles:
        all_roles.extend(roles)

    role_map = {r.id: r for r in all_roles}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_map.get(rid)

    members_map: dict[int, AsyncMock] = {}
    guild._members_map = members_map
    guild.get_member.side_effect = lambda uid: members_map.get(uid)

    async def mock_fetch_member(uid: int):
        if uid in members_map:
            return members_map[uid]
        mock_resp = MagicMock(status=404, reason="Not Found")
        raise discord.NotFound(mock_resp, f"Member {uid} not found")

    guild.fetch_member = AsyncMock(side_effect=mock_fetch_member)
    return guild


# ===========================================================================
# Fixtures de Persistencia
# ===========================================================================
@pytest.fixture
def clean_settings() -> Settings:
    """Configuración predecible para pruebas."""
    return Settings(
        guild_id=1547725310508667010,
        staff_role_id=1547729760384319518,
        admin_role_id=1548795786110967919,
        ceo_role_id=1548795782360993842,
        sin_verificar_role_id=1550000000000000001,
        ticket_rol_category_id=1550000000000000002,
        free_role_name="Libre",
    )


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factoría de sesiones asíncronas para pruebas."""
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded_team(session: AsyncSession) -> Team:
    """Equipo de prueba sembrado en base de datos."""
    team = Team(
        id=uuid.uuid4(),
        name="Fnatic",
        tag="FNC",
        slug="fnatic-upsert-test",
        division=Division.PREMIER,
        discord_role_id=200101,
    )
    session.add(team)
    await session.flush()
    return team


@pytest_asyncio.fixture
async def seeded_user(session: AsyncSession) -> DiscordUser:
    """Usuario de Discord de prueba sembrado en base de datos."""
    user = DiscordUser(
        discord_id="999001",
        username="rekkles",
        global_name="Rekkles",
    )
    session.add(user)
    await session.flush()
    return user


# ===========================================================================
# 1. Pruebas de TeamMembershipRepository: insert_if_not_exists & upsert
# ===========================================================================
class TestTeamMembershipRepositoryInsertIfNotExists:
    """Pruebas unitarias para insert_if_not_exists en TeamMembershipRepository."""

    @pytest.mark.asyncio
    async def test_insert_if_not_exists_inserts_new_membership(
        self, session: AsyncSession, seeded_team: Team, seeded_user: DiscordUser
    ):
        """Inserta exitosamente una nueva membresía cuando no existe fila previa."""
        repo = TeamMembershipRepository(session)

        membership = await repo.insert_if_not_exists(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )

        assert membership is not None
        assert membership.team_id == seeded_team.id
        assert membership.discord_user_id == seeded_user.discord_id
        assert membership.role == RosterRole.MID
        assert membership.is_captain is False

        # Confirmar en base de datos
        db_record = await repo.get(seeded_team.id, seeded_user.discord_id)
        assert db_record is not None
        assert db_record.role == RosterRole.MID

    @pytest.mark.asyncio
    async def test_insert_if_not_exists_returns_none_on_conflict_and_preserves_role(
        self, session: AsyncSession, seeded_team: Team, seeded_user: DiscordUser
    ):
        """Retorna None cuando ya existe membresía y no modifica el rol existente."""
        repo = TeamMembershipRepository(session)

        # Inserción inicial con rol competitivo MID
        first = await repo.insert_if_not_exists(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )
        assert first is not None
        assert first.role == RosterRole.MID

        # Intento de inserción concurrente con rol STAFF (DO NOTHING esperado)
        second = await repo.insert_if_not_exists(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.STAFF,
            is_captain=False,
        )
        assert second is None

        # Verificar que el rol en BD sigue siendo MID de forma inmutable
        db_record = await repo.get(seeded_team.id, seeded_user.discord_id)
        assert db_record is not None
        assert db_record.role == RosterRole.MID

    @pytest.mark.asyncio
    async def test_insert_if_not_exists_default_role_staff(
        self, session: AsyncSession, seeded_team: Team, seeded_user: DiscordUser
    ):
        """Si no se especifica el rol, se utiliza RosterRole.STAFF por defecto."""
        repo = TeamMembershipRepository(session)

        membership = await repo.insert_if_not_exists(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
        )

        assert membership is not None
        assert membership.role == RosterRole.STAFF

    @pytest.mark.asyncio
    async def test_insert_if_not_exists_invalid_inputs_raise_value_error(
        self, session: AsyncSession
    ):
        """Valida que identificadores nulos o inválidos levanten ValueError."""
        repo = TeamMembershipRepository(session)

        with pytest.raises(ValueError, match="Identificador de equipo inválido"):
            await repo.insert_if_not_exists(team_id="invalid-uuid", discord_user_id="123")

        with pytest.raises(ValueError, match="discord_user_id no puede ser nulo ni vacío"):
            await repo.insert_if_not_exists(team_id=uuid.uuid4(), discord_user_id="")


class TestTeamMembershipRepositoryUpsert:
    """Pruebas unitarias para upsert en TeamMembershipRepository."""

    @pytest.mark.asyncio
    async def test_upsert_inserts_when_not_exists(
        self, session: AsyncSession, seeded_team: Team, seeded_user: DiscordUser
    ):
        """Crea una nueva fila cuando no existía previamente."""
        repo = TeamMembershipRepository(session)

        membership = await repo.upsert(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.ADC,
            is_captain=True,
        )

        assert membership is not None
        assert membership.role == RosterRole.ADC
        assert membership.is_captain is True

        db_record = await repo.get(seeded_team.id, seeded_user.discord_id)
        assert db_record is not None
        assert db_record.role == RosterRole.ADC
        assert db_record.is_captain is True

    @pytest.mark.asyncio
    async def test_upsert_updates_when_already_exists(
        self, session: AsyncSession, seeded_team: Team, seeded_user: DiscordUser
    ):
        """Actualiza incondicionalmente rol y capitanía de una membresía existente."""
        repo = TeamMembershipRepository(session)

        # Crear como STAFF
        await repo.create(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.STAFF,
            is_captain=False,
        )

        # Transferencia / actualización explícita a TOP capitán
        updated = await repo.upsert(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.TOP,
            is_captain=True,
        )

        assert updated.role == RosterRole.TOP
        assert updated.is_captain is True

        db_record = await repo.get(seeded_team.id, seeded_user.discord_id)
        assert db_record is not None
        assert db_record.role == RosterRole.TOP
        assert db_record.is_captain is True

    @pytest.mark.asyncio
    async def test_upsert_invalid_inputs_raise_value_error(self, session: AsyncSession):
        """Valida entradas inválidas en upsert."""
        repo = TeamMembershipRepository(session)

        with pytest.raises(ValueError, match="Identificador de equipo inválido"):
            await repo.upsert(team_id="invalid-uuid", discord_user_id="123", role=RosterRole.MID)

        with pytest.raises(ValueError, match="discord_user_id no puede ser nulo ni vacío"):
            await repo.upsert(team_id=uuid.uuid4(), discord_user_id="", role=RosterRole.MID)


# ===========================================================================
# 2. Pruebas de RosterSyncService: Preservación de Posición (Requirement R2)
# ===========================================================================
class TestRosterSyncServiceRolePreservation:
    """Pruebas de preservación de rol y posición en RosterSyncService.handle_role_added."""

    @pytest.mark.asyncio
    async def test_handle_role_added_preserves_existing_player_position_mid(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_team: Team,
        seeded_user: DiscordUser,
    ):
        """
        Si un miembro ya está en la plantilla con posición competitiva (MID),
        handle_role_added la retorna inmediatamente sin sobreescribirla con STAFF.
        """
        membership_repo = TeamMembershipRepository(session)
        # Pre-sembrar jugador como MID
        await membership_repo.create(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )

        service = RosterSyncService(session_factory=session_factory)

        discord_role = create_mock_role(seeded_team.discord_role_id, seeded_team.name)
        member = create_mock_member(
            user_id=int(seeded_user.discord_id),
            name=seeded_user.username,
        )

        # Ejecutar handle_role_added
        result = await service.handle_role_added(
            member=member,
            role=discord_role,
            session=session,
        )

        assert result is not None
        assert result.role == RosterRole.MID

        # Verificar en base de datos que sigue siendo MID
        current = await membership_repo.get(seeded_team.id, seeded_user.discord_id)
        assert current is not None
        assert current.role == RosterRole.MID

        # Verificar que NO se registraron movimientos repetidos
        movements = await RosterMovementRepository(session).list_by_user(seeded_user.discord_id)
        assert len(movements) == 0

        # Verificar que NO se generaron logs de auditoría adicionales
        audit_logs = await AuditLogRepository(session).list_by_entity(
            entity_type="team_membership",
            entity_id=seeded_team.id,
        )
        assert len(audit_logs) == 0

    @pytest.mark.asyncio
    async def test_handle_role_added_preserves_existing_player_position_adc(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_team: Team,
        seeded_user: DiscordUser,
    ):
        """Preservación estricta de posición competitiva ADC frente a sincronización de roles."""
        membership_repo = TeamMembershipRepository(session)
        await membership_repo.create(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.ADC,
            is_captain=True,
        )

        service = RosterSyncService(session_factory=session_factory)
        discord_role = create_mock_role(seeded_team.discord_role_id, seeded_team.name)
        member = create_mock_member(
            user_id=int(seeded_user.discord_id),
            name=seeded_user.username,
        )

        result = await service.handle_role_added(
            member=member,
            role=discord_role,
            session=session,
        )

        assert result is not None
        assert result.role == RosterRole.ADC
        assert result.is_captain is True

        current = await membership_repo.get(seeded_team.id, seeded_user.discord_id)
        assert current is not None
        assert current.role == RosterRole.ADC
        assert current.is_captain is True

    @pytest.mark.asyncio
    async def test_handle_role_added_inserts_staff_when_not_exists(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_team: Team,
    ):
        """Un nuevo miembro recibe el rol no competitivo STAFF por defecto."""
        service = RosterSyncService(session_factory=session_factory)
        discord_role = create_mock_role(seeded_team.discord_role_id, seeded_team.name)
        new_member = create_mock_member(user_id=888123, name="NewRecruit")

        result = await service.handle_role_added(
            member=new_member,
            role=discord_role,
            session=session,
        )

        assert result is not None
        assert result.role == RosterRole.STAFF
        assert result.is_captain is False

        # Verificar movimiento registrado (JOINED)
        movements = await RosterMovementRepository(session).list_by_user("888123")
        assert len(movements) == 1
        assert movements[0].action == RosterMovementAction.JOINED
        assert movements[0].role == RosterRole.STAFF

        # Verificar auditoría
        audit_logs = await AuditLogRepository(session).list_by_entity(
            entity_type="team_membership",
            entity_id=seeded_team.id,
        )
        assert len(audit_logs) == 1
        assert audit_logs[0].action == "roster.member_joined"

    @pytest.mark.asyncio
    async def test_handle_role_added_concurrent_conflict_recovery(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_team: Team,
        seeded_user: DiscordUser,
    ):
        """
        Si ocurre una condición de carrera donde insert_if_not_exists retorna None
        (otro proceso insertó en paralelo), handle_role_added recupera y retorna
        la membresía existente sin lanzar excepciones ni duplicar movimientos.
        """
        # Sembrar membresía existente previa al conflicto
        membership_repo = TeamMembershipRepository(session)
        await membership_repo.create(
            team_id=seeded_team.id,
            discord_user_id=seeded_user.discord_id,
            role=RosterRole.SUPPORT,
            is_captain=False,
        )

        service = RosterSyncService(session_factory=session_factory)
        discord_role = create_mock_role(seeded_team.discord_role_id, seeded_team.name)
        member = create_mock_member(
            user_id=int(seeded_user.discord_id),
            name=seeded_user.username,
        )

        # Simula get() = None (carrera), y insert_if_not_exists = None (conflicto)
        with (
            patch.object(
                TeamMembershipRepository,
                "get",
                side_effect=[
                    None,  # Primer check de idempotencia
                    TeamMembership(  # Recuperación tras conflicto
                        team_id=seeded_team.id,
                        discord_user_id=seeded_user.discord_id,
                        role=RosterRole.SUPPORT,
                        is_captain=False,
                    ),
                ],
            ),
            patch.object(
                TeamMembershipRepository,
                "insert_if_not_exists",
                AsyncMock(return_value=None),
            ),
        ):
            result = await service.handle_role_added(
                member=member,
                role=discord_role,
                session=session,
            )

        assert result is not None
        assert result.role == RosterRole.SUPPORT

    @pytest.mark.asyncio
    async def test_transfer_player_uses_upsert_for_explicit_transfers(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_team: Team,
        seeded_user: DiscordUser,
    ):
        """transfer_player utiliza upsert para registrar o actualizar la nueva posición."""
        service = RosterSyncService(session_factory=session_factory)
        discord_role = create_mock_role(seeded_team.discord_role_id, seeded_team.name)
        member = create_mock_member(
            user_id=int(seeded_user.discord_id),
            name=seeded_user.username,
        )

        membership, team, prev_team = await service.transfer_player(
            member=member,
            team_role=discord_role,
            new_position=RosterRole.JUNGLE,
            session=session,
        )

        assert membership.role == RosterRole.JUNGLE
        assert team.id == seeded_team.id
        assert prev_team is None

        # Confirmar en BD
        db_rec = await TeamMembershipRepository(session).get(seeded_team.id, seeded_user.discord_id)
        assert db_rec is not None
        assert db_rec.role == RosterRole.JUNGLE


# ===========================================================================
# 3. Pruebas de RoleService: Desacoplamiento Transaccional (Requirement R3)
# ===========================================================================
class TestRoleServiceTransactionalDecoupling:
    """Pruebas para el desacoplamiento transaccional en confirm_role_request."""

    async def _setup_ticket_environment(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
        user_id: int,
        channel_id: int,
        team_name: str,
        team_role_id: int,
    ) -> tuple[MagicMock, AsyncMock, AsyncMock]:
        """Crea el entorno de ticket, equipo en BD y mocks de Discord."""
        team_role = create_mock_role(team_role_id, team_name)
        guild = create_mock_guild(clean_settings, roles=[team_role])
        staff = create_mock_member(900101, name="StaffAdmin", guild=guild)

        sin_verificar = guild.get_role(clean_settings.sin_verificar_role_id)
        assert sin_verificar is not None

        member = create_mock_member(
            user_id=user_id,
            name="TestCandidate",
            roles=[sin_verificar],
            guild=guild,
        )
        guild._members_map[user_id] = member

        async with session_factory() as session:
            team = Team(
                id=uuid.uuid4(),
                name=team_name,
                tag="TAG",
                slug=f"slug-{channel_id}",
                division=Division.PREMIER,
                discord_role_id=team_role_id,
            )
            session.add(team)

            repo = RoleRequestRepository(session)
            await repo.create_request(
                user_id=user_id,
                nombre_lol="Faker",
                riot_tag="KR1",
                equipo=team_name,
                canal_id=channel_id,
                posicion="mid",
            )
            await session.commit()

        return guild, staff, member

    @pytest.mark.asyncio
    async def test_confirm_role_request_db_commit_strictly_precedes_discord_api(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
    ):
        """
        Verifica de manera determinista que la transacción de BD YA está confirmada (COMMIT)
        en el momento exacto en que se invoca member.add_roles.
        """
        guild, staff, member = await self._setup_ticket_environment(
            session_factory, clean_settings, 600101, 700101, "T1 Telecom", 999101
        )

        db_committed_when_discord_called = False

        async def inspect_db_state_during_add_roles(*args, **kwargs):
            nonlocal db_committed_when_discord_called
            # Abrir una sesión completamente independiente y verificar estado en BD
            async with session_factory() as s:
                req = await RoleRequestRepository(s).get_by_channel_id(700101)
                memberships = await TeamMembershipRepository(s).list_by_user("600101")
                # Si el commit ya ocurrió, req.estado es APPROVED y la membresía existe
                if (
                    req is not None
                    and req.estado == RoleRequestStatus.APPROVED
                    and len(memberships) == 1
                    and memberships[0].role == RosterRole.MID
                ):
                    db_committed_when_discord_called = True

        member.add_roles.side_effect = inspect_db_state_during_add_roles

        bot_mock = MagicMock()
        bot_mock.roster_sync_service = RosterSyncService(session_factory=session_factory)
        service = RoleService(
            session_factory=session_factory,
            settings=clean_settings,
            bot=bot_mock,
        )

        ok, msg = await service.confirm_role_request(
            guild=guild,
            channel_id=700101,
            staff_member=staff,
        )

        assert ok is True, msg
        assert db_committed_when_discord_called is True
        member.add_roles.assert_awaited_once()
        member.remove_roles.assert_awaited_once()
        member.edit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirm_role_request_db_failure_never_calls_add_roles(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
    ):
        """
        Si la transacción en base de datos falla o se revierte, member.add_roles,
        member.remove_roles y member.edit NUNCA son invocados (cero efectos secundarios en Discord).
        """
        guild, staff, member = await self._setup_ticket_environment(
            session_factory, clean_settings, 600102, 700102, "DRX Gaming", 999102
        )

        bot_mock = MagicMock()
        roster_service = RosterSyncService(session_factory=session_factory)
        bot_mock.roster_sync_service = roster_service
        service = RoleService(
            session_factory=session_factory,
            settings=clean_settings,
            bot=bot_mock,
        )

        # Forzar un error fatal en la actualización del estado en base de datos
        with patch.object(
            RoleRequestRepository,
            "update_status",
            AsyncMock(side_effect=RuntimeError("Fallo catastrófico en commit de base de datos")),
        ):
            ok, msg = await service.confirm_role_request(
                guild=guild,
                channel_id=700102,
                staff_member=staff,
            )

        assert ok is False
        assert "No se ha aplicado ningún cambio." in msg

        # Verificación estricta: NINGUNA llamada a la API de Discord debe haberse ejecutado
        member.add_roles.assert_not_called()
        member.remove_roles.assert_not_called()
        member.edit.assert_not_called()

        # Verificar que en base de datos la solicitud continúa PENDING
        async with session_factory() as session:
            req = await RoleRequestRepository(session).get_by_channel_id(700102)
            assert req is not None
            assert req.estado == RoleRequestStatus.PENDING

    @pytest.mark.asyncio
    async def test_confirm_role_request_transfer_player_failure_aborts_discord_api(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
    ):
        """
        Si transfer_player falla dentro de la transacción, no se asignan roles en Discord.
        """
        guild, staff, member = await self._setup_ticket_environment(
            session_factory, clean_settings, 600103, 700103, "GenG Esports", 999103
        )

        bot_mock = MagicMock()
        roster_service = RosterSyncService(session_factory=session_factory)
        bot_mock.roster_sync_service = roster_service
        service = RoleService(
            session_factory=session_factory,
            settings=clean_settings,
            bot=bot_mock,
        )

        with patch.object(
            roster_service,
            "transfer_player",
            AsyncMock(side_effect=RuntimeError("Error en integridad de plantilla")),
        ):
            ok, msg = await service.confirm_role_request(
                guild=guild,
                channel_id=700103,
                staff_member=staff,
            )

        assert ok is False
        assert "No se ha aplicado ningún cambio." in msg

        member.add_roles.assert_not_called()
        member.remove_roles.assert_not_called()
        member.edit.assert_not_called()

        async with session_factory() as session:
            req = await RoleRequestRepository(session).get_by_channel_id(700103)
            assert req is not None
            assert req.estado == RoleRequestStatus.PENDING

    @pytest.mark.asyncio
    async def test_confirm_role_request_discord_forbidden_preserves_committed_db(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
    ):
        """
        Si Discord rechaza la asignación del rol (discord.Forbidden), la base de datos
        permanece confirmada como APPROVED (la transacción ya fue comprometida de forma segura).
        """
        guild, staff, member = await self._setup_ticket_environment(
            session_factory, clean_settings, 600104, 700104, "KT Rolster", 999104
        )

        # Discord falla con Forbidden
        mock_response = MagicMock(status=403, reason="Missing Permissions")
        member.add_roles.side_effect = discord.Forbidden(mock_response, "Missing Permissions")

        bot_mock = MagicMock()
        bot_mock.roster_sync_service = RosterSyncService(session_factory=session_factory)
        service = RoleService(
            session_factory=session_factory,
            settings=clean_settings,
            bot=bot_mock,
        )

        ok, msg = await service.confirm_role_request(
            guild=guild,
            channel_id=700104,
            staff_member=staff,
        )

        assert ok is True
        assert "confirmado" in msg

        # La base de datos está confirmada y es consistente
        async with session_factory() as session:
            req = await RoleRequestRepository(session).get_by_channel_id(700104)
            assert req is not None
            assert req.estado == RoleRequestStatus.APPROVED
            assert req.staff_id == staff.id

            memberships = await TeamMembershipRepository(session).list_by_user("600104")
            assert len(memberships) == 1
            assert memberships[0].role == RosterRole.MID

    @pytest.mark.asyncio
    async def test_confirm_role_request_sin_verificar_removed_and_nick_updated(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
    ):
        """
        Verifica que el rol 'Sin Verificar' es removido y el apodo es actualizado con
        el tag del equipo tras el commit exitoso en base de datos.
        """
        guild, staff, member = await self._setup_ticket_environment(
            session_factory, clean_settings, 600105, 700105, "Fnatic Squad", 999105
        )

        bot_mock = MagicMock()
        bot_mock.roster_sync_service = RosterSyncService(session_factory=session_factory)
        service = RoleService(
            session_factory=session_factory,
            settings=clean_settings,
            bot=bot_mock,
        )

        ok, msg = await service.confirm_role_request(
            guild=guild,
            channel_id=700105,
            staff_member=staff,
        )

        assert ok is True
        sin_verificar = guild.get_role(clean_settings.sin_verificar_role_id)
        member.remove_roles.assert_awaited_once_with(sin_verificar)
        member.edit.assert_awaited_once_with(nick="TAG Faker")

    @pytest.mark.asyncio
    async def test_confirm_role_request_member_not_in_guild_aborts_before_db_mutation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
    ):
        """
        Si el miembro solicitante no se encuentra en el servidor ni por caché ni por fetch,
        se aborta la operación y la base de datos permanece intacta en estado PENDING.
        """
        guild, staff, member = await self._setup_ticket_environment(
            session_factory, clean_settings, 600106, 700106, "Isolated Team", 999106
        )

        # Simular que el miembro abandonó el servidor
        guild._members_map.clear()
        guild.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "Not Found")

        bot_mock = MagicMock()
        bot_mock.roster_sync_service = RosterSyncService(session_factory=session_factory)
        service = RoleService(
            session_factory=session_factory,
            settings=clean_settings,
            bot=bot_mock,
        )

        ok, msg = await service.confirm_role_request(
            guild=guild,
            channel_id=700106,
            staff_member=staff,
        )

        assert ok is False
        assert "no se encuentra en el servidor" in msg

        async with session_factory() as session:
            req = await RoleRequestRepository(session).get_by_channel_id(700106)
            assert req is not None
            assert req.estado == RoleRequestStatus.PENDING
