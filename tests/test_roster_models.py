"""
Pruebas exhaustivas para los modelos de plantillas (roster) y gobernanza compartida con RCL-Next.
Valida en PGlite:
- Enums de dominio y métodos auxiliares (is_competitive, is_starter).
- Modelos: DiscordUser, Player, Team, TeamMembership, RosterMovement, AuditLog.
- Restricciones DDL:
  * Check constraint: is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support').
  * Índice único parcial: máximo 1 capitán por equipo.
  * Clave primaria compuesta en team_memberships (team_id, discord_user_id).
  * Serialización y consultas JSONB en AuditLog.
  * Eliminación en cascada y SET NULL de claves foráneas.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models import Team as CanonicalTeam
from liga_bot.models.enums import AppRole, Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import (
    AuditLog,
    DiscordUser,
    Player,
    RosterMovement,
    Team,
    TeamMembership,
)


class TestRosterEnumsAndHelpers:
    """Verifica los valores de los enums y la lógica de negocio en métodos auxiliares."""

    @pytest.mark.parametrize(
        ("role", "expected_competitive"),
        [
            (RosterRole.TOP, True),
            (RosterRole.JUNGLE, True),
            (RosterRole.MID, True),
            (RosterRole.ADC, True),
            (RosterRole.SUPPORT, True),
            (RosterRole.SUBSTITUTE, True),
            (RosterRole.COACH, False),
            (RosterRole.STAFF, False),
            (RosterRole.PARTNERS, False),
        ],
    )
    def test_roster_role_is_competitive(self, role: RosterRole, expected_competitive: bool):
        """Verifica que is_competitive identifique con precisión las 6 posiciones de juego."""
        assert role.is_competitive() is expected_competitive

    @pytest.mark.parametrize(
        ("role", "expected_starter"),
        [
            (RosterRole.TOP, True),
            (RosterRole.JUNGLE, True),
            (RosterRole.MID, True),
            (RosterRole.ADC, True),
            (RosterRole.SUPPORT, True),
            (RosterRole.SUBSTITUTE, False),
            (RosterRole.COACH, False),
            (RosterRole.STAFF, False),
            (RosterRole.PARTNERS, False),
        ],
    )
    def test_roster_role_is_starter(self, role: RosterRole, expected_starter: bool):
        """Verifica que is_starter identifique únicamente a los 5 titulares."""
        assert role.is_starter() is expected_starter

    def test_roster_movement_action_values(self):
        """Verifica que las acciones de movimiento coincidan con el esquema RCL-Next."""
        expected_actions = {
            "joined",
            "left",
            "promoted_to_captain",
            "demoted_from_captain",
            "role_changed",
        }
        assert {a.value for a in RosterMovementAction} == expected_actions

    def test_app_role_values(self):
        """Verifica los valores del enum app_role."""
        assert {r.value for r in AppRole} == {"viewer", "admin"}


class TestRosterReexport:
    """Verifica que Team sea re-exportado y tenga sus relaciones de plantilla activas."""

    def test_team_identity(self):
        """Verifica que Team importado de roster sea el modelo canónico."""
        assert Team is CanonicalTeam

    def test_team_roster_relationships(self):
        """Verifica que las relaciones memberships y movements estén adjuntas a Team."""
        assert hasattr(Team, "memberships")
        assert hasattr(Team, "movements")


class TestDiscordUserModel:
    """Pruebas para la entidad discord_users."""

    @pytest.mark.asyncio
    async def test_create_discord_user_defaults(self, session: AsyncSession):
        """Verifica creación de usuario, clave primaria y rol por defecto."""
        user = DiscordUser(
            discord_id="111222333444555666",
            username="faker",
            global_name="Lee Sang-hyeok",
            avatar_hash="a_hash_123",
        )
        session.add(user)
        await session.flush()

        assert user.discord_id == "111222333444555666"
        assert user.username == "faker"
        assert user.role == AppRole.VIEWER
        assert isinstance(user.created_at, datetime)
        assert isinstance(user.updated_at, datetime)

    @pytest.mark.asyncio
    async def test_discord_user_pk_uniqueness(self, session: AsyncSession):
        """Verifica que discord_id sea clave primaria única."""
        u1 = DiscordUser(discord_id="999888777", username="user_one")
        session.add(u1)
        await session.flush()

        u2 = DiscordUser(discord_id="999888777", username="user_duplicate")
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(u2)
                await session.flush()


class TestPlayerModel:
    """Pruebas para la entidad players y su relación opcional con DiscordUser."""

    @pytest.mark.asyncio
    async def test_create_player_linked_to_user(self, session: AsyncSession):
        """Verifica creación de jugador vinculado a un usuario de Discord."""
        user = DiscordUser(discord_id="333444555", username="caps_mid")
        session.add(user)
        await session.flush()

        player = Player(
            discord_user_id=user.discord_id,
            game_name="Caps",
            riot_tag="EUW",
            puuid="sample-puuid-caps",
            country_code="DK",
            is_main=True,
        )
        session.add(player)
        await session.flush()

        assert isinstance(player.id, uuid.UUID)
        assert player.discord_user_id == user.discord_id
        assert player.is_main is True
        assert player.country_code == "DK"

    @pytest.mark.asyncio
    async def test_player_unique_game_name_riot_tag(self, session: AsyncSession):
        """Verifica la restricción de unicidad compuesta (game_name, riot_tag)."""
        p1 = Player(game_name="Jankos", riot_tag="PL")
        session.add(p1)
        await session.flush()

        p2 = Player(game_name="Jankos", riot_tag="PL")
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(p2)
                await session.flush()


class TestTeamMembershipModel:
    """Pruebas de integridad relacional en team_memberships."""

    @pytest.fixture
    async def base_team_and_users(self, session: AsyncSession):
        """Crea un equipo y varios usuarios para pruebas de membresía."""
        team = Team(
            name="Fnatic",
            tag="FNC",
            slug="fnatic",
            division=Division.PREMIER,
            discord_role_id=112233445566,
        )
        u1 = DiscordUser(discord_id="101", username="player_top")
        u2 = DiscordUser(discord_id="102", username="player_sub")
        u3 = DiscordUser(discord_id="103", username="coach_bob")
        session.add_all([team, u1, u2, u3])
        await session.flush()
        return team, u1, u2, u3

    @pytest.mark.parametrize(
        "starter_role",
        [
            RosterRole.TOP,
            RosterRole.JUNGLE,
            RosterRole.MID,
            RosterRole.ADC,
            RosterRole.SUPPORT,
        ],
    )
    @pytest.mark.asyncio
    async def test_check_constraint_captain_allowed_for_starters(
        self, session: AsyncSession, base_team_and_users, starter_role: RosterRole
    ):
        """Verifica que las 5 posiciones titulares PUEDEN ser capitanes (is_captain=True)."""
        team, user, _, _ = base_team_and_users
        membership = TeamMembership(
            team_id=team.id,
            discord_user_id=user.discord_id,
            role=starter_role,
            is_captain=True,
        )
        session.add(membership)
        await session.flush()
        assert membership.is_captain is True
        assert membership.role == starter_role

    @pytest.mark.asyncio
    async def test_check_constraint_captain_rejected_for_substitute(
        self, session: AsyncSession, base_team_and_users
    ):
        """Verifica que un 'substitute' NO PUEDE ser capitán (violación del CheckConstraint)."""
        team, _, user_sub, _ = base_team_and_users
        invalid_membership = TeamMembership(
            team_id=team.id,
            discord_user_id=user_sub.discord_id,
            role=RosterRole.SUBSTITUTE,
            is_captain=True,
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin_nested():
                session.add(invalid_membership)
                await session.flush()

    @pytest.mark.parametrize(
        "non_competitive_role",
        [
            RosterRole.COACH,
            RosterRole.STAFF,
            RosterRole.PARTNERS,
        ],
    )
    @pytest.mark.asyncio
    async def test_check_constraint_captain_rejected_for_non_competitive_roles(
        self, session: AsyncSession, base_team_and_users, non_competitive_role: RosterRole
    ):
        """Verifica que roles no competitivos NO PUEDEN ser capitanes."""
        team, _, _, user_coach = base_team_and_users
        invalid_membership = TeamMembership(
            team_id=team.id,
            discord_user_id=user_coach.discord_id,
            role=non_competitive_role,
            is_captain=True,
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            async with session.begin_nested():
                session.add(invalid_membership)
                await session.flush()

    @pytest.mark.parametrize(
        "role",
        list(RosterRole),
    )
    @pytest.mark.asyncio
    async def test_check_constraint_non_captains_allow_any_role(
        self, session: AsyncSession, role: RosterRole
    ):
        """Verifica que si is_captain=False, CUALQUIER rol es legal en el CheckConstraint."""
        team = Team(
            name=f"Team-{role.value}",
            tag="TAG",
            slug=f"team-{role.value}",
            division=Division.ASCEND,
            discord_role_id=int(f"99{len(role.value)}{ord(role.value[0])}"),
        )
        user = DiscordUser(discord_id=f"user_{role.value}", username=f"u_{role.value}")
        session.add_all([team, user])
        await session.flush()

        membership = TeamMembership(
            team_id=team.id,
            discord_user_id=user.discord_id,
            role=role,
            is_captain=False,
        )
        session.add(membership)
        await session.flush()
        assert membership.is_captain is False
        assert membership.role == role

    @pytest.mark.asyncio
    async def test_composite_primary_key_prevents_duplicate_membership(
        self, session: AsyncSession, base_team_and_users
    ):
        """Verifica que la clave compuesta (team_id, user_id) impida membresías duplicadas."""
        team, user, _, _ = base_team_and_users
        m1 = TeamMembership(
            team_id=team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.TOP,
            is_captain=False,
        )
        session.add(m1)
        await session.flush()

        m2 = TeamMembership(
            team_id=team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(m2)
                await session.flush()

    @pytest.mark.asyncio
    async def test_partial_index_enforces_single_captain_per_team(self, session: AsyncSession):
        """
        Verifica que el índice único parcial (team_id WHERE is_captain = true)
        impida que un equipo tenga más de un capitán simultáneamente.
        """
        team = Team(
            name="G2 Esports",
            tag="G2",
            slug="g2-esports",
            division=Division.PREMIER,
            discord_role_id=222333444,
        )
        u1 = DiscordUser(discord_id="cap_1", username="caps")
        u2 = DiscordUser(discord_id="cap_2", username="miky")
        session.add_all([team, u1, u2])
        await session.flush()

        m1 = TeamMembership(
            team_id=team.id,
            discord_user_id=u1.discord_id,
            role=RosterRole.MID,
            is_captain=True,
        )
        session.add(m1)
        await session.flush()

        m2 = TeamMembership(
            team_id=team.id,
            discord_user_id=u2.discord_id,
            role=RosterRole.SUPPORT,
            is_captain=True,
        )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(m2)
                await session.flush()

    @pytest.mark.asyncio
    async def test_partial_index_allows_captains_in_different_teams(self, session: AsyncSession):
        """Verifica que dos equipos distintos puedan tener cada uno su propio capitán."""
        t1 = Team(
            name="Team Alpha",
            tag="ALP",
            slug="team-alpha",
            division=Division.PREMIER,
            discord_role_id=333001,
        )
        t2 = Team(
            name="Team Beta",
            tag="BET",
            slug="team-beta",
            division=Division.PREMIER,
            discord_role_id=333002,
        )
        u1 = DiscordUser(discord_id="capt_a", username="captain_a")
        u2 = DiscordUser(discord_id="capt_b", username="captain_b")
        session.add_all([t1, t2, u1, u2])
        await session.flush()

        m1 = TeamMembership(
            team_id=t1.id,
            discord_user_id=u1.discord_id,
            role=RosterRole.TOP,
            is_captain=True,
        )
        m2 = TeamMembership(
            team_id=t2.id,
            discord_user_id=u2.discord_id,
            role=RosterRole.JUNGLE,
            is_captain=True,
        )
        session.add_all([m1, m2])
        await session.flush()

        assert m1.is_captain is True
        assert m2.is_captain is True

    @pytest.mark.asyncio
    async def test_partial_index_allows_multiple_non_captains_in_same_team(
        self, session: AsyncSession
    ):
        """Verifica que múltiples miembros sin capitanía puedan coexistir en el mismo equipo."""
        team = Team(
            name="Team Multi",
            tag="MLT",
            slug="team-multi",
            division=Division.ASCEND,
            discord_role_id=444111222,
        )
        u1 = DiscordUser(discord_id="nc_1", username="non_cap_1")
        u2 = DiscordUser(discord_id="nc_2", username="non_cap_2")
        session.add_all([team, u1, u2])
        await session.flush()

        m1 = TeamMembership(
            team_id=team.id,
            discord_user_id=u1.discord_id,
            role=RosterRole.TOP,
            is_captain=False,
        )
        m2 = TeamMembership(
            team_id=team.id,
            discord_user_id=u2.discord_id,
            role=RosterRole.MID,
            is_captain=False,
        )
        session.add_all([m1, m2])
        await session.flush()

        assert m1.is_captain is False
        assert m2.is_captain is False


class TestRosterMovementModel:
    """Pruebas para el registro histórico de movimientos de plantilla."""

    @pytest.mark.asyncio
    async def test_create_roster_movement(self, session: AsyncSession):
        """Verifica inserción de movimiento de plantilla con actor y rol."""
        team = Team(
            name="KOI",
            tag="KOI",
            slug="koi",
            division=Division.PREMIER,
            discord_role_id=444555666,
        )
        target_user = DiscordUser(discord_id="target_user_id", username="player1")
        actor_user = DiscordUser(discord_id="actor_user_id", username="coach1")
        session.add_all([team, target_user, actor_user])
        await session.flush()

        mov = RosterMovement(
            team_id=team.id,
            discord_user_id=target_user.discord_id,
            action=RosterMovementAction.JOINED,
            role=RosterRole.ADC,
            actor_id=actor_user.discord_id,
        )
        session.add(mov)
        await session.flush()

        assert isinstance(mov.id, uuid.UUID)
        assert mov.action == RosterMovementAction.JOINED
        assert mov.role == RosterRole.ADC
        assert mov.actor_id == actor_user.discord_id
        assert isinstance(mov.created_at, datetime)


class TestAuditLogModel:
    """Pruebas para la persistencia transaccional y campos JSONB de audit_logs."""

    @pytest.mark.asyncio
    async def test_audit_log_jsonb_serialization(self, session: AsyncSession):
        """Verifica serialización y recuperación exacta de diccionarios JSONB estructurados."""
        actor = DiscordUser(discord_id="staff_audit", username="admin_staff")
        session.add(actor)
        await session.flush()

        before_state = {
            "role": "substitute",
            "is_captain": False,
            "flags": ["bench"],
            "metadata": {"updated_by": None},
        }
        after_state = {
            "role": "top",
            "is_captain": True,
            "flags": ["starter", "shotcaller"],
            "metadata": {"updated_by": "staff_audit"},
        }
        entity_uuid = uuid.uuid4()

        log_entry = AuditLog(
            actor_discord_user_id=actor.discord_id,
            action="roster.role_changed",
            entity_type="team_membership",
            entity_id=entity_uuid,
            before=before_state,
            after=after_state,
        )
        session.add(log_entry)
        await session.flush()

        res = await session.execute(select(AuditLog).where(AuditLog.id == log_entry.id))
        fetched = res.scalar_one()

        assert fetched.before == before_state
        assert fetched.after == after_state
        assert fetched.before["flags"] == ["bench"]
        assert fetched.after["flags"] == ["starter", "shotcaller"]
        assert fetched.after["metadata"]["updated_by"] == "staff_audit"

    @pytest.mark.asyncio
    async def test_audit_log_null_before_after(self, session: AsyncSession):
        """Verifica que before y after puedan ser nulos para eventos sin estado previo/posterior."""
        log_entry = AuditLog(
            action="system.ping",
            entity_type="system",
            before=None,
            after=None,
        )
        session.add(log_entry)
        await session.flush()

        res = await session.execute(select(AuditLog).where(AuditLog.id == log_entry.id))
        fetched = res.scalar_one()
        assert fetched.before is None
        assert fetched.after is None
        assert fetched.actor_discord_user_id is None


class TestCascadeDeletions:
    """Verifica que las restricciones ON DELETE CASCADE y ON DELETE SET NULL operen en DB."""

    @pytest.mark.asyncio
    async def test_cascade_delete_team_removes_memberships_and_movements(
        self, session: AsyncSession
    ):
        """Al eliminar un Team, se deben eliminar en cascada sus TeamMembership y RosterMovement."""
        team = Team(
            name="To Be Deleted",
            tag="DEL",
            slug="to-be-deleted",
            division=Division.ASCEND,
            discord_role_id=888999111,
        )
        user = DiscordUser(discord_id="user_del", username="del_player")
        session.add_all([team, user])
        await session.flush()

        membership = TeamMembership(
            team_id=team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.MID,
            is_captain=True,
        )
        movement = RosterMovement(
            team_id=team.id,
            discord_user_id=user.discord_id,
            action=RosterMovementAction.JOINED,
            role=RosterRole.MID,
        )
        session.add_all([membership, movement])
        await session.flush()

        team_id = team.id
        movement_id = movement.id

        await session.delete(team)
        await session.flush()
        session.expire_all()

        res_m = await session.execute(
            select(TeamMembership).where(TeamMembership.team_id == team_id)
        )
        assert res_m.scalars().all() == []

        res_mov = await session.execute(
            select(RosterMovement).where(RosterMovement.id == movement_id)
        )
        assert res_mov.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_cascade_delete_discord_user_removes_memberships(self, session: AsyncSession):
        """Al eliminar un DiscordUser, sus membresías de equipo se eliminan en cascada."""
        team = Team(
            name="Team Persistent",
            tag="PER",
            slug="team-persistent",
            division=Division.PREMIER,
            discord_role_id=888999222,
        )
        user = DiscordUser(discord_id="user_to_drop", username="dropped_user")
        session.add_all([team, user])
        await session.flush()

        membership = TeamMembership(
            team_id=team.id,
            discord_user_id=user.discord_id,
            role=RosterRole.ADC,
            is_captain=False,
        )
        session.add(membership)
        await session.flush()

        user_id = user.discord_id

        await session.delete(user)
        await session.flush()
        session.expire_all()

        res = await session.execute(
            select(TeamMembership).where(TeamMembership.discord_user_id == user_id)
        )
        assert res.scalars().all() == []

    @pytest.mark.asyncio
    async def test_cascade_delete_user_sets_null_on_player_and_audit_log(
        self, session: AsyncSession
    ):
        """Al eliminar un DiscordUser, las claves foráneas hacia él se establecen en NULL."""
        user = DiscordUser(discord_id="user_actor", username="actor_user")
        session.add(user)
        await session.flush()

        player = Player(
            discord_user_id=user.discord_id,
            game_name="Rekkles",
            riot_tag="SUPP",
        )
        audit = AuditLog(
            actor_discord_user_id=user.discord_id,
            action="test.action",
            entity_type="general",
        )
        session.add_all([player, audit])
        await session.flush()

        await session.delete(user)
        await session.flush()

        await session.refresh(player)
        await session.refresh(audit)

        assert player.discord_user_id is None
        assert audit.actor_discord_user_id is None


class TestModelReprs:
    """Verifica que __repr__ no provoque MissingGreenlet en operaciones asíncronas."""

    def test_safe_reprs_without_loaded_attributes(self):
        """Verifica que los __repr__ manejen atributos no cargados limpiamente."""
        user = DiscordUser(discord_id="123", username="test")
        membership = TeamMembership(
            team_id=uuid.uuid4(),
            discord_user_id="123",
            role=RosterRole.MID,
        )
        player = Player(game_name="TestPlayer", riot_tag="TAG")
        mov = RosterMovement(
            team_id=uuid.uuid4(),
            discord_user_id="123",
            action=RosterMovementAction.JOINED,
        )
        log = AuditLog(action="test", entity_type="demo")

        assert "DiscordUser" in repr(user)
        assert "TeamMembership" in repr(membership)
        assert "Player" in repr(player)
        assert "RosterMovement" in repr(mov)
        assert "AuditLog" in repr(log)
