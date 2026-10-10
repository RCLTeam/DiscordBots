"""
Pruebas unitarias y de integración para RosterSyncService (Milestone 3).

Valida de forma exhaustiva:
1. Sincronización de roles añadidos (handle_role_added) y retirados (handle_role_removed).
2. Regla 4: No retirada de roles de equipo en Discord al añadir otro club.
3. Regla 5: Baja de membresía exclusivamente ante remoción del rol en Discord.
4. Regla 6: Invariante de posición competitiva única (CompetitivePositionConflictError).
5. Regla 7: Trazabilidad completa en roster_movements y audit_logs con snapshots JSONB.
6. Consulta de equipos de un usuario con carga ansiosa (get_user_teams).
7. Inyección de dependencias en LigaBot.setup_hook().
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.bot import LigaBot
from liga_bot.config import Settings
from liga_bot.models.enums import AppRole, Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import DiscordUser, Player, Team
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
)
from liga_bot.services.roster_sync_service import (
    CompetitivePositionConflictError,
    InvalidCaptainRoleError,
    PlayerNotTeamMemberError,
    RosterSyncError,
    RosterSyncService,
)

# ---------------------------------------------------------------------------
# Helpers y Mocks de Discord
# ---------------------------------------------------------------------------


def create_mock_role(role_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.Role con id, name y mention."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def create_mock_member(
    user_id: int,
    name: str = "TestPlayer",
    global_name: str | None = "Test Global Name",
    roles: list[MagicMock] | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.Member con métodos de tracking de roles."""
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.global_name = global_name
    member.display_name = global_name or name
    member.avatar = MagicMock()
    member.avatar.key = f"avatar_{user_id}"
    member.roles = list(roles or [])
    member.guild = guild
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    return member


def create_mock_guild(roles: list[MagicMock] | None = None) -> MagicMock:
    """Crea un mock de discord.Guild resolviendo roles por get_role."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1547725310508667010
    all_roles = list(roles or [])
    role_map = {r.id: r for r in all_roles}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_map.get(rid)
    return guild


# ---------------------------------------------------------------------------
# Fixtures de Persistencia PGlite y Datos Base
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_settings() -> Settings:
    return Settings(
        guild_id=1547725310508667010,
        staff_role_id=1547729760384319518,
    )


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def clean_roster_tables(migrated_db: AsyncEngine) -> AsyncGenerator[None, None]:
    """Limpia las tablas compartidas antes y después de cada test para aislar commits."""
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


@pytest.fixture
def roster_sync_service(
    session_factory: async_sessionmaker[AsyncSession], clean_settings: Settings
) -> RosterSyncService:
    return RosterSyncService(session_factory=session_factory, settings=clean_settings)


@pytest_asyncio.fixture
async def seed_teams(session_factory: async_sessionmaker[AsyncSession]) -> tuple[Team, Team, Team]:
    """Siembra tres equipos con roles de Discord conocidos."""
    async with session_factory() as session:
        t1 = Team(
            name="Team Alpha",
            tag="ALP",
            slug="team-alpha",
            division=Division.PREMIER,
            discord_role_id=1001,
        )
        t2 = Team(
            name="Team Beta",
            tag="BET",
            slug="team-beta",
            division=Division.PREMIER,
            discord_role_id=1002,
        )
        t3 = Team(
            name="Team Gamma",
            tag="GAM",
            slug="team-gamma",
            division=Division.ASCEND,
            discord_role_id=1003,
        )
        session.add_all([t1, t2, t3])
        await session.commit()
        await session.refresh(t1)
        await session.refresh(t2)
        await session.refresh(t3)
        return t1, t2, t3


@pytest_asyncio.fixture
async def seed_actor(session_factory: async_sessionmaker[AsyncSession]) -> DiscordUser:
    """Siembra un usuario de staff para satisfacer Foreign Keys de actor_id."""
    async with session_factory() as session:
        actor = DiscordUser(
            discord_id="999000",
            username="staff_admin",
            role=AppRole.ADMIN,
        )
        session.add(actor)
        await session.commit()
        await session.refresh(actor)
        return actor


# ===========================================================================
# 1. Pruebas de Inicialización e Inyección en LigaBot
# ===========================================================================


class TestRosterSyncServiceInit:
    """Pruebas para inicialización de RosterSyncService e inyección en el bot."""

    def test_init_with_explicit_dependencies(
        self, session_factory: async_sessionmaker[AsyncSession], clean_settings: Settings
    ):
        bot_mock = MagicMock()
        service = RosterSyncService(
            session_factory=session_factory, settings=clean_settings, bot=bot_mock
        )
        assert service.session_factory is session_factory
        assert service.settings is clean_settings
        assert service.bot is bot_mock
        assert service.default_join_role == RosterRole.STAFF

    def test_init_with_defaults(self):
        with (
            patch("liga_bot.services.roster_sync_service.get_session_factory") as mock_get_sf,
            patch("liga_bot.services.roster_sync_service.get_settings") as mock_get_settings,
        ):
            mock_sf = MagicMock()
            mock_settings = MagicMock()
            mock_get_sf.return_value = mock_sf
            mock_get_settings.return_value = mock_settings

            service = RosterSyncService()
            assert service.session_factory is mock_sf
            assert service.settings is mock_settings
            assert service.bot is None
            assert service.default_join_role == RosterRole.STAFF

    def test_init_with_competitive_default_role_raises_value_error(
        self, session_factory: async_sessionmaker[AsyncSession], clean_settings: Settings
    ):
        with pytest.raises(ValueError, match="default_join_role debe ser un rol no competitivo"):
            RosterSyncService(
                session_factory=session_factory,
                settings=clean_settings,
                default_join_role=RosterRole.MID,
            )

    @pytest.mark.asyncio
    async def test_bot_setup_hook_injects_roster_sync_service(self):
        """Verifica que LigaBot inicializa self.roster_sync_service en setup_hook."""
        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()
        mock_session_factory = MagicMock()

        with (
            patch("liga_bot.bot.get_engine", new_callable=AsyncMock) as mock_get_engine,
            patch("liga_bot.bot.get_session_factory") as mock_get_factory,
        ):
            mock_get_engine.return_value = mock_engine
            mock_get_factory.return_value = mock_session_factory

            bot = LigaBot(extensions=())
            bot.load_extension = AsyncMock()

            assert getattr(bot, "roster_sync_service", None) is None
            await bot.setup_hook()

            assert isinstance(bot.roster_sync_service, RosterSyncService)
            assert bot.roster_sync_service.session_factory is mock_session_factory
            assert bot.roster_sync_service.settings is bot.settings
            assert bot.roster_sync_service.bot is bot

            await bot.close()


# ===========================================================================
# 2. Pruebas de handle_role_added
# ===========================================================================


class TestHandleRoleAdded:
    """Pruebas unitarias y de integración para la adición de roles de equipo."""

    @pytest.mark.asyncio
    async def test_handle_role_added_brand_new_discord_user(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Usuario nuevo en Discord recibe rol de equipo: crea DiscordUser y TeamMembership."""
        team_alpha, _, _ = seed_teams
        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456789, name="new_summoner", guild=guild)

        membership = await roster_sync_service.handle_role_added(
            member=member, role=role, actor_id=seed_actor.discord_id
        )

        assert membership is not None
        assert membership.team_id == team_alpha.id
        assert membership.discord_user_id == "123456789"
        assert membership.role == RosterRole.STAFF
        assert membership.is_captain is False

        # Verificar persistencia en base de datos
        async with session_factory() as session:
            user = await session.get(DiscordUser, "123456789")
            assert user is not None
            assert user.username == "new_summoner"

            m_repo = TeamMembershipRepository(session)
            persisted_m = await m_repo.get(team_alpha.id, "123456789")
            assert persisted_m is not None
            assert persisted_m.role == RosterRole.STAFF

            mov_repo = RosterMovementRepository(session)
            movements = await mov_repo.list_by_team(team_alpha.id)
            assert len(movements) == 1
            assert movements[0].action == RosterMovementAction.JOINED
            assert movements[0].role == RosterRole.STAFF
            assert movements[0].actor_id == seed_actor.discord_id

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_alpha.id)
            assert len(audits) == 1
            assert audits[0].action == "roster.member_joined"
            assert audits[0].actor_discord_user_id == seed_actor.discord_id
            assert audits[0].before is None
            assert audits[0].after["role"] == "staff"

    @pytest.mark.asyncio
    async def test_handle_role_added_existing_discord_user_updates_profile(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Usuario existente en Discord actualiza su perfil al unirse a un equipo."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(
                discord_id="123456780",
                username="old_name",
                global_name="Old Global",
                avatar_hash="old_avatar",
            )
            session.add(user)
            await session.commit()

        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(
            user_id=123456780,
            name="new_name",
            global_name="New Global",
            guild=guild,
        )

        membership = await roster_sync_service.handle_role_added(member=member, role=role)
        assert membership is not None

        async with session_factory() as session:
            updated_user = await session.get(DiscordUser, "123456780")
            assert updated_user is not None
            assert updated_user.username == "new_name"
            assert updated_user.global_name == "New Global"
            assert updated_user.avatar_hash == "avatar_123456780"

    @pytest.mark.asyncio
    async def test_handle_role_added_non_team_role_ignored(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Un rol que no corresponde a ningún club es ignorado y retorna None."""
        guild = create_mock_guild()
        role = create_mock_role(999888777, "Random Non-Team Role")
        member = create_mock_member(user_id=123456790, name="ignored_player", guild=guild)

        membership = await roster_sync_service.handle_role_added(member=member, role=role)

        assert membership is None
        async with session_factory() as session:
            user = await session.get(DiscordUser, "123456790")
            assert user is None
            mov_repo = RosterMovementRepository(session)
            assert len(await mov_repo.list_by_user("123456790")) == 0

    @pytest.mark.asyncio
    async def test_handle_role_added_existing_member_is_idempotent(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Si el usuario ya pertenece al equipo, la operación es idempotente."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456791", username="existing_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456791", RosterRole.MID)
            await session.commit()

        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456791, name="existing_player", guild=guild)

        result = await roster_sync_service.handle_role_added(member=member, role=role)

        assert result is not None
        assert result.team_id == team_alpha.id
        assert result.role == RosterRole.MID

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            all_m = await m_repo.list_by_team(team_alpha.id)
            assert len(all_m) == 1
            mov_repo = RosterMovementRepository(session)
            assert len(await mov_repo.list_by_team(team_alpha.id)) == 0

    @pytest.mark.asyncio
    async def test_handle_role_added_auto_provisions_unregistered_actor(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Si actor_id no está en discord_users, se aprovisiona automáticamente."""
        team_alpha, _, _ = seed_teams
        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456799, name="actor_test_member", guild=guild)

        membership = await roster_sync_service.handle_role_added(
            member=member, role=role, actor_id="777888"
        )
        assert membership is not None

        async with session_factory() as session:
            actor = await session.get(DiscordUser, "777888")
            assert actor is not None
            assert actor.discord_id == "777888"

    @pytest.mark.asyncio
    async def test_handle_role_added_rule_4_never_strips_existing_roles(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
    ):
        """Regla 4: El bot jamás llama a remove_roles al asignar un nuevo club."""
        team_alpha, team_beta, _ = seed_teams
        guild = create_mock_guild()
        role_alpha = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        role_beta = create_mock_role(team_beta.discord_role_id, "Team Beta")
        member = create_mock_member(
            user_id=123456792,
            name="multi_club_player",
            roles=[role_alpha],
            guild=guild,
        )

        # 1. Unirse a Team Alpha
        await roster_sync_service.handle_role_added(member=member, role=role_alpha)
        # 2. Unirse a Team Beta
        member.roles.append(role_beta)
        await roster_sync_service.handle_role_added(member=member, role=role_beta)

        # Verificación empírica de ausencia de retiro de roles
        member.remove_roles.assert_not_called()
        teams = await roster_sync_service.get_user_teams("123456792")
        assert len(teams) == 2

    @pytest.mark.asyncio
    async def test_handle_role_added_custom_default_join_role(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_settings: Settings,
        seed_teams: tuple[Team, Team, Team],
    ):
        """Permite configurar un rol no competitivo por defecto personalizado (ej. COACH)."""
        service = RosterSyncService(
            session_factory=session_factory,
            settings=clean_settings,
            default_join_role=RosterRole.COACH,
        )
        team_alpha, _, _ = seed_teams
        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456781, name="coach_member", guild=guild)

        membership = await service.handle_role_added(member=member, role=role)
        assert membership is not None
        assert membership.role == RosterRole.COACH


# ===========================================================================
# 3. Pruebas de handle_role_removed
# ===========================================================================


class TestHandleRoleRemoved:
    """Pruebas unitarias y de integración para la remoción de roles de equipo."""

    @pytest.mark.asyncio
    async def test_handle_role_removed_existing_member(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Remover rol en Discord elimina membresía y registra movimiento y auditoría."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456793", username="departing_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456793", RosterRole.TOP, is_captain=True)
            await session.commit()

        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456793, name="departing_player", guild=guild)

        success = await roster_sync_service.handle_role_removed(
            member=member, role=role, actor_id=seed_actor.discord_id
        )

        assert success is True
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            assert await m_repo.get(team_alpha.id, "123456793") is None

            mov_repo = RosterMovementRepository(session)
            movements = await mov_repo.list_by_team(team_alpha.id)
            assert len(movements) == 1
            assert movements[0].action == RosterMovementAction.LEFT
            assert movements[0].role == RosterRole.TOP
            assert movements[0].actor_id == seed_actor.discord_id

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_alpha.id)
            assert len(audits) == 1
            assert audits[0].action == "roster.member_left"
            assert audits[0].before["role"] == "top"
            assert audits[0].before["is_captain"] is True
            assert audits[0].after is None

    @pytest.mark.asyncio
    async def test_handle_role_removed_non_team_role_ignored(
        self,
        roster_sync_service: RosterSyncService,
    ):
        """Remover un rol que no es de equipo retorna False y no muta la base de datos."""
        guild = create_mock_guild()
        role = create_mock_role(999111222, "Non-Team Role")
        member = create_mock_member(user_id=123456794, name="sample_user", guild=guild)

        success = await roster_sync_service.handle_role_removed(member=member, role=role)
        assert success is False

    @pytest.mark.asyncio
    async def test_handle_role_removed_non_member_ignored(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
    ):
        """Remover un rol de equipo para un usuario no en plantilla retorna False."""
        team_alpha, _, _ = seed_teams
        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456798, name="not_in_team", guild=guild)

        success = await roster_sync_service.handle_role_removed(member=member, role=role)
        assert success is False

    @pytest.mark.asyncio
    async def test_handle_role_removed_rule_5_selective_removal(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Regla 5: Remover el rol de Team Alpha conserva la membresía de Team Beta."""
        team_alpha, team_beta, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456795", username="dual_member")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456795", RosterRole.STAFF)
            await m_repo.create(team_beta.id, "123456795", RosterRole.COACH)
            await session.commit()

        guild = create_mock_guild()
        role_alpha = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456795, name="dual_member", guild=guild)

        success = await roster_sync_service.handle_role_removed(member=member, role=role_alpha)
        assert success is True

        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            assert await m_repo.get(team_alpha.id, "123456795") is None
            beta_m = await m_repo.get(team_beta.id, "123456795")
            assert beta_m is not None
            assert beta_m.role == RosterRole.COACH

    @pytest.mark.asyncio
    async def test_handle_role_removed_with_actor_provisions_actor(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Remover rol con un actor desconocido aprovisiona al actor en discord_users."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456796", username="removed_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456796", RosterRole.STAFF)
            await session.commit()

        guild = create_mock_guild()
        role = create_mock_role(team_alpha.discord_role_id, "Team Alpha")
        member = create_mock_member(user_id=123456796, name="removed_player", guild=guild)

        success = await roster_sync_service.handle_role_removed(
            member=member, role=role, actor_id="555666"
        )
        assert success is True

        async with session_factory() as session:
            actor = await session.get(DiscordUser, "555666")
            assert actor is not None


# ===========================================================================
# 4. Pruebas de change_player_position
# ===========================================================================


class TestChangePlayerPosition:
    """Pruebas unitarias para cambio de posición y verificación de la Regla 6."""

    @pytest.mark.asyncio
    async def test_change_position_non_competitive_to_competitive(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Cambio exitoso de posición de STAFF a MID."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456801", username="promoted_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456801", RosterRole.STAFF)
            await session.commit()

        updated = await roster_sync_service.change_player_position(
            discord_user_id="123456801",
            team_id=team_alpha.id,
            new_role=RosterRole.MID,
            actor_id=seed_actor.discord_id,
        )

        assert updated.role == RosterRole.MID
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            m = await m_repo.get(team_alpha.id, "123456801")
            assert m is not None
            assert m.role == RosterRole.MID

            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_team(team_alpha.id)
            assert len(movs) == 1
            assert movs[0].action == RosterMovementAction.ROLE_CHANGED
            assert movs[0].role == RosterRole.MID

            audit_repo = AuditLogRepository(session)
            audits = await audit_repo.list_by_entity("team_membership", team_alpha.id)
            assert len(audits) == 1
            assert audits[0].before["role"] == "staff"
            assert audits[0].after["role"] == "mid"

    @pytest.mark.asyncio
    async def test_change_position_rule_6_conflict_raises_exception(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Regla 6: Si ya tiene posición competitiva en Team A, asignar en Team B falla."""
        team_alpha, team_beta, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456802", username="conflict_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456802", RosterRole.TOP)
            await m_repo.create(team_beta.id, "123456802", RosterRole.STAFF)
            await session.commit()

        with pytest.raises(CompetitivePositionConflictError) as exc_info:
            await roster_sync_service.change_player_position(
                discord_user_id="123456802",
                team_id=team_beta.id,
                new_role=RosterRole.ADC,
                actor_id=seed_actor.discord_id,
            )

        err_msg = str(exc_info.value)
        assert "123456802" in err_msg
        assert "top" in err_msg.lower() or "competitiva" in err_msg.lower()
        assert exc_info.value.existing_team_id == team_alpha.id
        assert exc_info.value.new_team_id == team_beta.id
        assert exc_info.value.existing_role == "top"
        assert exc_info.value.attempted_role == "adc"

        # Asegurar que no hubo mutación en base de datos
        async with session_factory() as session:
            m_repo = TeamMembershipRepository(session)
            beta_m = await m_repo.get(team_beta.id, "123456802")
            assert beta_m is not None
            assert beta_m.role == RosterRole.STAFF

    @pytest.mark.asyncio
    async def test_change_position_rule_6_allows_non_competitive_roles(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Regla 6: Rol competitivo en Team A permite roles no competitivos en Team B."""
        team_alpha, team_beta, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456803", username="flexible_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456803", RosterRole.TOP)
            await m_repo.create(team_beta.id, "123456803", RosterRole.PARTNERS)
            await session.commit()

        for non_comp_role in (RosterRole.COACH, RosterRole.STAFF, RosterRole.PARTNERS):
            updated = await roster_sync_service.change_player_position(
                discord_user_id="123456803",
                team_id=team_beta.id,
                new_role=non_comp_role,
                actor_id=seed_actor.discord_id,
            )
            assert updated.role == non_comp_role

    @pytest.mark.asyncio
    async def test_change_position_within_same_team_allowed(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Cambiar de rol competitivo a otro dentro del MISMO equipo está plenamente permitido."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456804", username="lane_swap_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456804", RosterRole.TOP)
            await session.commit()

        updated = await roster_sync_service.change_player_position(
            discord_user_id="123456804",
            team_id=team_alpha.id,
            new_role=RosterRole.JUNGLE,
            actor_id=seed_actor.discord_id,
        )
        assert updated.role == RosterRole.JUNGLE

    @pytest.mark.asyncio
    async def test_change_position_invalid_captain_role_raises_exception(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Designar capitanía a un rol no titular lanza InvalidCaptainRoleError."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456805", username="captain_fail_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456805", RosterRole.STAFF)
            await session.commit()

        invalid_roles = (
            RosterRole.SUBSTITUTE,
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
        )
        for invalid_role in invalid_roles:
            with pytest.raises(InvalidCaptainRoleError) as exc_info:
                await roster_sync_service.change_player_position(
                    discord_user_id="123456805",
                    team_id=team_alpha.id,
                    new_role=invalid_role,
                    actor_id=seed_actor.discord_id,
                    is_captain=True,
                )
            assert exc_info.value.role == invalid_role.value

    @pytest.mark.asyncio
    async def test_change_position_valid_captain_role_succeeds(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Designar capitanía a un rol titular ('mid') es válido."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456806", username="mid_captain")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456806", RosterRole.STAFF)
            await session.commit()

        updated = await roster_sync_service.change_player_position(
            discord_user_id="123456806",
            team_id=team_alpha.id,
            new_role=RosterRole.MID,
            actor_id=seed_actor.discord_id,
            is_captain=True,
        )
        assert updated.role == RosterRole.MID
        assert updated.is_captain is True

    @pytest.mark.asyncio
    async def test_change_position_promoted_and_demoted_from_captain_actions(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Cambio de estado de capitanía genera acciones PROMOTED / DEMOTED."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456807", username="cap_status_player")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456807", RosterRole.SUPPORT, is_captain=False)
            await session.commit()

        # Promoción a capitán
        promoted = await roster_sync_service.change_player_position(
            discord_user_id="123456807",
            team_id=team_alpha.id,
            new_role=RosterRole.SUPPORT,
            actor_id=seed_actor.discord_id,
            is_captain=True,
        )
        assert promoted.is_captain is True

        async with session_factory() as session:
            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_user("123456807")
            assert movs[0].action == RosterMovementAction.PROMOTED_TO_CAPTAIN

        # Descenso de capitán
        demoted = await roster_sync_service.change_player_position(
            discord_user_id="123456807",
            team_id=team_alpha.id,
            new_role=RosterRole.SUPPORT,
            actor_id=seed_actor.discord_id,
            is_captain=False,
        )
        assert demoted.is_captain is False

        async with session_factory() as session:
            mov_repo = RosterMovementRepository(session)
            movs = await mov_repo.list_by_user("123456807")
            assert movs[0].action == RosterMovementAction.DEMOTED_FROM_CAPTAIN

    @pytest.mark.asyncio
    async def test_change_position_not_member_raises_exception(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
    ):
        """Intentar cambiar la posición de un usuario no miembro lanza PlayerNotTeamMemberError."""
        team_alpha, _, _ = seed_teams
        with pytest.raises(PlayerNotTeamMemberError) as exc_info:
            await roster_sync_service.change_player_position(
                discord_user_id="999999999",
                team_id=team_alpha.id,
                new_role=RosterRole.ADC,
                actor_id=seed_actor.discord_id,
            )
        assert exc_info.value.discord_user_id == "999999999"
        assert exc_info.value.team_id == team_alpha.id

    @pytest.mark.asyncio
    async def test_change_position_invalid_arguments_raise_value_error(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
    ):
        """Argumentos inválidos (vacíos o UUIDs corruptos) lanzan ValueError."""
        team_alpha, _, _ = seed_teams

        with pytest.raises(ValueError, match="discord_user_id"):
            await roster_sync_service.change_player_position(
                discord_user_id="",
                team_id=team_alpha.id,
                new_role=RosterRole.MID,
                actor_id=seed_actor.discord_id,
            )

        with pytest.raises(ValueError, match="actor_id"):
            await roster_sync_service.change_player_position(
                discord_user_id="123456",
                team_id=team_alpha.id,
                new_role=RosterRole.MID,
                actor_id="   ",
            )

        with pytest.raises(ValueError, match="Identificador de equipo"):
            await roster_sync_service.change_player_position(
                discord_user_id="123456",
                team_id="not-a-uuid",
                new_role=RosterRole.MID,
                actor_id=seed_actor.discord_id,
            )


# ===========================================================================
# 5. Pruebas de get_user_teams
# ===========================================================================


class TestGetUserTeams:
    """Pruebas para consulta de equipos del usuario con carga ansiosa."""

    @pytest.mark.asyncio
    async def test_get_user_teams_multiple(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        team_alpha, team_beta, team_gamma = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456810", username="omnipresent_user")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456810", RosterRole.TOP)
            await m_repo.create(team_beta.id, "123456810", RosterRole.COACH)
            await m_repo.create(team_gamma.id, "123456810", RosterRole.STAFF)
            await session.commit()

        teams = await roster_sync_service.get_user_teams("123456810")
        assert len(teams) == 3
        team_names = {t.name for t, m in teams}
        assert team_names == {"Team Alpha", "Team Beta", "Team Gamma"}

        # Verificar acceso sin lazy loading error fuera de sesión
        for t, m in teams:
            assert t.tag is not None
            assert m.role is not None

    @pytest.mark.asyncio
    async def test_get_user_teams_empty(
        self,
        roster_sync_service: RosterSyncService,
    ):
        teams = await roster_sync_service.get_user_teams("non_existent_user")
        assert teams == []

    @pytest.mark.asyncio
    async def test_get_user_teams_invalid_id(
        self,
        roster_sync_service: RosterSyncService,
    ):
        assert await roster_sync_service.get_user_teams("") == []
        assert await roster_sync_service.get_user_teams("   ") == []

    @pytest.mark.asyncio
    async def test_get_user_teams_single(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="123456811", username="single_team_user")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "123456811", RosterRole.MID)
            await session.commit()

        teams = await roster_sync_service.get_user_teams("123456811")
        assert len(teams) == 1
        t, m = teams[0]
        assert t.id == team_alpha.id
        assert m.role == RosterRole.MID


# ===========================================================================
# 7. Pruebas de traspaso de jugadores (transfer_player)
# ===========================================================================


class TestTransferPlayer:
    """Traspasos entre equipos con posición competitiva y no competitiva."""

    @pytest.mark.asyncio
    async def test_transfer_moves_competitive_membership(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
    ):
        """Una posición competitiva se libera del club anterior y se crea en el destino."""
        alpha, beta, _ = seed_teams
        role_alpha = create_mock_role(1001, "Team Alpha")
        role_beta = create_mock_role(1002, "Team Beta")
        member = create_mock_member(500100, name="Ninym", roles=[role_alpha])

        await roster_sync_service.handle_role_added(member, role_alpha, actor_id="999000")
        async with session_factory() as session:
            await TeamMembershipRepository(session).update_role(alpha.id, "500100", RosterRole.MID)
            await session.commit()

        membership, _, previous_team = await roster_sync_service.transfer_player(
            member=member,
            team_role=role_beta,
            new_position=RosterRole.ADC,
            actor_id="999000",
        )

        assert membership.team_id == beta.id
        assert membership.role == RosterRole.ADC
        assert previous_team is not None
        assert previous_team.id == alpha.id

        async with session_factory() as session:
            repo = TeamMembershipRepository(session)
            assert await repo.get(alpha.id, "500100") is None
            assert await repo.get(beta.id, "500100") is not None

            movimientos = await RosterMovementRepository(session).list_by_user("500100")
            acciones = [m.action for m in movimientos]
            assert RosterMovementAction.LEFT in acciones
            assert RosterMovementAction.JOINED in acciones

    @pytest.mark.asyncio
    async def test_transfer_keeps_non_competitive_memberships(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
    ):
        """Un rol no competitivo (coach) en otro club se conserva tras el traspaso."""
        alpha, beta, _ = seed_teams
        role_alpha = create_mock_role(1001, "Team Alpha")
        role_beta = create_mock_role(1002, "Team Beta")
        member = create_mock_member(500101, name="Coach", roles=[role_alpha])

        await roster_sync_service.handle_role_added(member, role_alpha, actor_id="999000")
        async with session_factory() as session:
            await TeamMembershipRepository(session).update_role(
                alpha.id, "500101", RosterRole.COACH
            )
            await session.commit()

        await roster_sync_service.transfer_player(
            member=member,
            team_role=role_beta,
            new_position=RosterRole.STAFF,
            actor_id="999000",
        )

        async with session_factory() as session:
            repo = TeamMembershipRepository(session)
            assert await repo.get(alpha.id, "500101") is not None
            assert await repo.get(beta.id, "500101") is not None

    @pytest.mark.asyncio
    async def test_transfer_replaces_membership_in_same_team(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
        seed_teams: tuple[Team, Team, Team],
        seed_actor: DiscordUser,
    ):
        """Traspasar al mismo equipo sustituye el registro y cambia la posición."""
        alpha, _, _ = seed_teams
        role_alpha = create_mock_role(1001, "Team Alpha")
        member = create_mock_member(500102, name="Jugador", roles=[role_alpha])

        await roster_sync_service.handle_role_added(member, role_alpha, actor_id="999000")

        membership, _, previous_team = await roster_sync_service.transfer_player(
            member=member,
            team_role=role_alpha,
            new_position=RosterRole.TOP,
            actor_id="999000",
        )

        assert previous_team is None
        assert membership.role == RosterRole.TOP
        async with session_factory() as session:
            memberships = await TeamMembershipRepository(session).list_by_user("500102")
            assert len(memberships) == 1

    @pytest.mark.asyncio
    async def test_transfer_unknown_role_raises(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
    ):
        """Un rol de Discord que no pertenece a ningún equipo aborta el traspaso."""
        unknown_role = create_mock_role(9999, "Rol Cualquiera")
        member = create_mock_member(500103, name="Nadie")

        with pytest.raises(RosterSyncError):
            await roster_sync_service.transfer_player(
                member=member,
                team_role=unknown_role,
                new_position=RosterRole.TOP,
                actor_id="999000",
            )


# ===========================================================================
# 8. Pruebas de alta de cuentas de juego (ensure_player)
# ===========================================================================


class TestEnsurePlayer:
    """Alta e idempotencia de la cuenta de juego en la tabla players."""

    @pytest.mark.asyncio
    async def test_creates_player_linked_to_discord_user(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """La primera cuenta de un usuario se crea, se vincula y queda como principal."""
        member = create_mock_member(600100, name="Ninym")

        player = await roster_sync_service.ensure_player(
            member=member, game_name="Ninym", riot_tag="EUW"
        )

        assert player.game_name == "Ninym"
        assert player.riot_tag == "EUW"
        assert player.discord_user_id == "600100"
        assert player.is_main is True

        async with session_factory() as session:
            # El DiscordUser se crea también, por integridad referencial
            assert await session.get(DiscordUser, "600100") is not None

    @pytest.mark.asyncio
    async def test_is_idempotent_for_same_account(
        self,
        roster_sync_service: RosterSyncService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Repetir el alta con los mismos datos no duplica la cuenta."""
        member = create_mock_member(600101, name="Hiperxp")

        first = await roster_sync_service.ensure_player(
            member=member, game_name="Hiperxp", riot_tag="EUW"
        )
        second = await roster_sync_service.ensure_player(
            member=member, game_name="Hiperxp", riot_tag="EUW"
        )

        assert first.id == second.id
        async with session_factory() as session:
            result = await session.execute(select(Player).where(Player.discord_user_id == "600101"))
            assert len(result.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_second_account_is_not_main(
        self,
        roster_sync_service: RosterSyncService,
    ):
        """Una segunda cuenta del mismo usuario no se marca como principal."""
        member = create_mock_member(600102, name="Smurf")

        await roster_sync_service.ensure_player(member=member, game_name="Cuenta1", riot_tag="EUW")
        second = await roster_sync_service.ensure_player(
            member=member, game_name="Cuenta2", riot_tag="EUW"
        )

        assert second.is_main is False


# ===========================================================================
# 9. Pruebas de resolución de apodo canónico (resolve_canonical_nick)
# ===========================================================================


class TestResolveCanonicalNick:
    """Pruebas unitarias para resolve_canonical_nick según prioridad competitiva."""

    @pytest.mark.asyncio
    async def test_resolve_canonical_nick_competitive_member_applies_tag(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Un usuario con membresía competitiva recibe su apodo con el tag del equipo."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="700100", username="test_mid")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "700100", RosterRole.MID)
            await session.commit()

        nick = await roster_sync_service.resolve_canonical_nick("700100", "Hiperxp")
        assert nick == f"{team_alpha.tag} Hiperxp"

    @pytest.mark.asyncio
    async def test_resolve_canonical_nick_competitive_overrides_non_competitive_tag(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Un jugador competitivo en un club con rol no competitivo en otro
        mantiene el tag competitivo.
        """
        team_alpha, team_beta, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="700101", username="dual_role_user")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            # Rol competitivo en Alpha ('ALP')
            await m_repo.create(team_alpha.id, "700101", RosterRole.TOP)
            # Rol no competitivo en Beta ('BET')
            await m_repo.create(team_beta.id, "700101", RosterRole.PARTNERS)
            await session.commit()

        # Incluso si el base_name trae el tag del club secundario/partner
        nick = await roster_sync_service.resolve_canonical_nick(
            "700101", f"{team_beta.tag} Hiperxp"
        )
        assert nick == f"{team_alpha.tag} Hiperxp"

    @pytest.mark.asyncio
    async def test_resolve_canonical_nick_only_non_competitive_strips_tag(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Un usuario que solo tiene rol no competitivo ve eliminado cualquier tag de club."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="700102", username="partner_only")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "700102", RosterRole.PARTNERS)
            await session.commit()

        nick_with_tag = await roster_sync_service.resolve_canonical_nick(
            "700102", f"{team_alpha.tag} Hiperxp"
        )
        assert nick_with_tag == "Hiperxp"

        nick_clean = await roster_sync_service.resolve_canonical_nick("700102", "Hiperxp")
        assert nick_clean == "Hiperxp"

    @pytest.mark.asyncio
    async def test_resolve_canonical_nick_invalid_user_id(
        self,
        roster_sync_service: RosterSyncService,
    ):
        """Si el discord_user_id es vacío o inválido, retorna el base_name limpio truncado a 32."""
        assert await roster_sync_service.resolve_canonical_nick("", "  Hiperxp  ") == "Hiperxp"
        assert await roster_sync_service.resolve_canonical_nick("   ", "Hiperxp") == "Hiperxp"

    @pytest.mark.asyncio
    async def test_resolve_canonical_nick_truncates_to_32_chars(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """El apodo canónico se trunca a 32 caracteres (límite de apodo de Discord)."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="700103", username="long_name_user")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "700103", RosterRole.MID)
            await session.commit()

        long_name = "SuperUltraMegaLongPlayerNickname1234567890"
        nick = await roster_sync_service.resolve_canonical_nick("700103", long_name)
        assert len(nick) == 32
        assert nick.startswith(f"{team_alpha.tag} ")

    @pytest.mark.asyncio
    async def test_resolve_canonical_nick_with_explicit_session_and_int_id(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """
        Invocar resolve_canonical_nick con sesión abierta e ID entero
        cubre la rama inyectada y el casteo de ID.
        """
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="700104", username="int_user")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "700104", RosterRole.SUPPORT)
            await session.commit()

        async with session_factory() as session:
            nick = await roster_sync_service.resolve_canonical_nick(
                700104, "Hiperxp", session=session
            )
            assert nick == f"{team_alpha.tag} Hiperxp"

    @pytest.mark.asyncio
    async def test_resolve_canonical_nick_strips_trailing_whitespace_on_truncation(
        self,
        roster_sync_service: RosterSyncService,
        seed_teams: tuple[Team, Team, Team],
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verifica que el recorte de 32 caracteres no deja espacios en blanco al final."""
        team_alpha, _, _ = seed_teams
        async with session_factory() as session:
            user = DiscordUser(discord_id="700105", username="space_cut_user")
            session.add(user)
            await session.flush()
            m_repo = TeamMembershipRepository(session)
            await m_repo.create(team_alpha.id, "700105", RosterRole.MID)
            await session.commit()

        # 'ALP ' (4) + 27 'A' + ' EXTRA' -> char 32 cae en espacio
        base_name = ("A" * 27) + " EXTRA"
        nick = await roster_sync_service.resolve_canonical_nick("700105", base_name)
        assert not nick.endswith(" ")
        assert len(nick) <= 32
