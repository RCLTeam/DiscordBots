"""
Empirical Adversarial Challenge Suite: UI State Transitions & Dropdowns.

Stress-tests GestionarPosicionView against:
1. Rapid dropdown selections:
   - Sequential rapid selections on TeamSelect verifying selected_team_id tracks last selection.
   - Switch between competitive and non-competitive roles in PositionSelect verifying captaincy
     is automatically reset/disabled when switching to non-starter roles.
2. Single-team vs multi-team auto-selection:
   - Exactly 1 team: verify selected_team_id is auto-selected and default option is set.
   - Exactly 0 teams: verify view handles empty gracefully without crashing, buttons are disabled.
   - Exactly 5 teams: verify all 5 teams are represented, no premature auto-selection.
3. Concurrent execution & End-to-End PGlite integration:
   - Concurrent dropdown interactions with asyncio.gather.
   - Unauthorized user interaction blocking via interaction_check.
   - Resilient embed generation when avatar or team lookup is missing.
   - Real PGlite database integration: full flow with conflict recovery and audit trail.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.models.enums import Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import DiscordUser, Player, Team, TeamMembership
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    TeamMembershipRepository,
)
from liga_bot.services.roster_sync_service import RosterSyncService
from liga_bot.ui.roster import GestionarPosicionView

# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


def make_mock_member(
    user_id: int = 123456789,
    name: str = "TestPlayer",
    display_name: str | None = None,
    with_avatar: bool = True,
) -> MagicMock:
    """Crea un mock de discord.Member con avatar y permisos simulados."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = display_name or name
    member.mention = f"<@{user_id}>"
    member.roles = []
    member.guild_permissions = MagicMock()
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    member.send = AsyncMock()

    if with_avatar:
        avatar = MagicMock()
        avatar.url = f"https://cdn.discordapp.com/avatars/{user_id}/avatar.png"
        member.display_avatar = avatar
    else:
        member.display_avatar = None
    return member


def make_mock_team(
    team_id: uuid.UUID | None = None,
    name: str = "Team Alpha",
    tag: str = "ALP",
    slug: str = "team-alpha",
    division: Division = Division.PREMIER,
    discord_role_id: int = 1001,
) -> Team:
    """Crea una instancia en memoria de Team."""
    return Team(
        id=team_id or uuid.uuid4(),
        name=name,
        tag=tag,
        slug=slug,
        division=division,
        discord_role_id=discord_role_id,
    )


def make_mock_membership(
    team: Team,
    discord_user_id: str = "123456789",
    role: RosterRole = RosterRole.STAFF,
    is_captain: bool = False,
) -> TeamMembership:
    """Crea una instancia en memoria de TeamMembership."""
    membership = TeamMembership(
        team_id=team.id,
        discord_user_id=discord_user_id,
        role=role,
        is_captain=is_captain,
    )
    membership.team = team
    return membership


def make_mock_interaction(
    user: MagicMock | None = None,
    guild: MagicMock | None = None,
    channel: MagicMock | None = None,
) -> MagicMock:
    """Crea un mock de discord.Interaction con response y followup asíncronos."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user or make_mock_member(user_id=999000, name="staff_actor")
    interaction.guild = guild or MagicMock(spec=discord.Guild)
    interaction.channel = channel or MagicMock(spec=discord.TextChannel)
    interaction.channel_id = interaction.channel.id
    interaction.client = MagicMock()
    interaction.edit_original_response = AsyncMock()

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(return_value=False)
    response.send_message = AsyncMock()
    response.defer = AsyncMock()
    response.edit_message = AsyncMock()
    interaction.response = response

    followup = MagicMock(spec=discord.Webhook)
    followup.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    followup.edit_message = AsyncMock()
    interaction.followup = followup

    return interaction


def set_select_values(select: discord.ui.Select[Any], values: list[str]) -> None:
    """Asigna valores simulados a un componente Select."""
    select.values = list(values)


# ---------------------------------------------------------------------------
# 1. Challenge: Rapid Dropdown Selections & Captaincy Reset Invariants
# ---------------------------------------------------------------------------


class TestAdversarialRapidDropdownSelections:
    """Stress-test rapid sequential selections on TeamSelect and PositionSelect."""

    @pytest.mark.asyncio
    async def test_rapid_sequential_team_selections(self) -> None:
        """
        Verify that sequentially selecting multiple teams updates selected_team_id accurately,
        maintains default options properly, and tracks the last selection without desync.
        """
        target_member = make_mock_member(user_id=101, name="MultiTeamMember")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        teams = [
            make_mock_team(name=f"Team {name}", tag=tag)
            for name, tag in [
                ("Alpha", "ALP"),
                ("Beta", "BET"),
                ("Gamma", "GAM"),
                ("Delta", "DEL"),
                ("Epsilon", "EPS"),
            ]
        ]
        user_teams = [
            (team, make_mock_membership(team, str(target_member.id), RosterRole.STAFF))
            for team in teams
        ]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        assert view.selected_team_id is None
        assert len(view.team_select.options) == 5

        # Rapid permutation: Delta (3) -> Alpha (0) -> Epsilon (4) -> Beta (1) -> Gamma (2)
        selection_order = [3, 0, 4, 1, 2]
        for idx in selection_order:
            chosen_team = teams[idx]
            interaction = make_mock_interaction(user=actor_member)
            set_select_values(view.team_select, [str(chosen_team.id)])
            await view.team_select.callback(interaction)

            # Invariant 1: selected_team_id strictly matches the latest selection
            assert view.selected_team_id == chosen_team.id, (
                f"Expected selected_team_id={chosen_team.id}, got {view.selected_team_id}"
            )

            # Invariant 2: exactly one option is marked default, matching the selected team
            default_options = [opt for opt in view.team_select.options if opt.default]
            assert len(default_options) == 1, (
                f"Expected exactly 1 default option, found {len(default_options)}"
            )
            assert default_options[0].value == str(chosen_team.id)

            # Invariant 3: interaction deferral was awaited
            interaction.response.defer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_captaincy_reset_on_non_starter_roles(self) -> None:
        """
        Verify that captaincy is automatically reset when switching to any non-starter role:
        - substitute (competitive, but non-starter)
        - coach (non-competitive, non-starter)
        - staff (non-competitive, non-starter)
        - partners (non-competitive, non-starter)
        And preserved when switching between starter roles (top, jungle, mid, adc, support).
        """
        target_member = make_mock_member(user_id=202, name="CaptainCandidate")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        team = make_mock_team(name="Alpha", tag="ALP")
        user_teams = [(team, make_mock_membership(team, str(target_member.id), RosterRole.TOP))]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        interaction = make_mock_interaction(user=actor_member)

        # 1. Select starter role TOP and set is_captain = True
        set_select_values(view.position_select, ["top"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.TOP
        view.is_captain = True

        # 2. Switch to starter role JUNGLE -> is_captain must remain True
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["jungle"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.JUNGLE
        assert view.is_captain is True, "Starter role should not reset is_captain"

        # 3. Switch to SUBSTITUTE (competitive non-starter) -> is_captain must reset to False!
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["substitute"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.SUBSTITUTE
        assert view.is_captain is False, (
            "Switching to substitute must automatically reset is_captain to False"
        )

        # 4. Re-enable is_captain, switch to COACH -> is_captain must reset to False!
        view.is_captain = True
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["coach"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.COACH
        assert view.is_captain is False, (
            "Switching to coach must automatically reset is_captain to False"
        )

        # 5. Re-enable is_captain, switch to STAFF -> is_captain must reset to False!
        view.is_captain = True
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["staff"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.STAFF
        assert view.is_captain is False, (
            "Switching to staff must automatically reset is_captain to False"
        )

        # 6. Re-enable is_captain, switch to PARTNERS -> is_captain must reset to False!
        view.is_captain = True
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["partners"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.PARTNERS
        assert view.is_captain is False, (
            "Switching to partners must automatically reset is_captain to False"
        )

        # 7. Switch back to starter MID -> is_captain does not self-promote, remains False
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["mid"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.MID
        assert view.is_captain is False

        # 8. Set is_captain = True for MID, switch to ADC, then SUPPORT -> remains True
        view.is_captain = True
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["adc"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.ADC
        assert view.is_captain is True

        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["support"])
        await view.position_select.callback(interaction)
        assert view.selected_role == RosterRole.SUPPORT
        assert view.is_captain is True

    @pytest.mark.asyncio
    async def test_rapid_alternating_role_and_team_selections(self) -> None:
        """
        Verify state integrity when alternating selections between TeamSelect and PositionSelect.
        """
        target_member = make_mock_member(user_id=303, name="AgilePlayer")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        t1 = make_mock_team(name="Alpha", tag="ALP")
        t2 = make_mock_team(name="Beta", tag="BET")
        user_teams = [
            (t1, make_mock_membership(t1, str(target_member.id), RosterRole.TOP)),
            (t2, make_mock_membership(t2, str(target_member.id), RosterRole.STAFF)),
        ]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        # Alternating sequence:
        # Team 1 -> Role ADC -> Team 2 -> Role COACH -> Team 1 -> Role TOP
        steps = [
            ("team", str(t1.id), t1.id, None),
            ("role", "adc", t1.id, RosterRole.ADC),
            ("team", str(t2.id), t2.id, RosterRole.ADC),
            ("role", "coach", t2.id, RosterRole.COACH),
            ("team", str(t1.id), t1.id, RosterRole.COACH),
            ("role", "top", t1.id, RosterRole.TOP),
        ]

        for step_type, val, expected_team, expected_role in steps:
            inter = make_mock_interaction(user=actor_member)
            if step_type == "team":
                set_select_values(view.team_select, [val])
                await view.team_select.callback(inter)
            else:
                set_select_values(view.position_select, [val])
                await view.position_select.callback(inter)

            assert view.selected_team_id == expected_team
            assert view.selected_role == expected_role


# ---------------------------------------------------------------------------
# 2. Challenge: Single-Team vs Multi-Team Auto-Selection & Boundary Conditions
# ---------------------------------------------------------------------------


class TestAdversarialTeamAutoSelectionAndBoundaries:
    """Stress-test 0-team, 1-team, and 5-team initialization conditions and UI invariants."""

    def test_single_team_auto_selection_invariants(self) -> None:
        """
        Verify that initializing with exactly 1 team:
        - Automatically sets selected_team_id to that team's ID.
        - Marks that single option as default=True in TeamSelect.
        - Keeps TeamSelect enabled.
        - Keeps SaveButton enabled.
        - Leaves selected_role as None (operator must explicitly pick new role).
        """
        target_member = make_mock_member(user_id=401, name="MonoTeamPlayer")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        solo_team = make_mock_team(name="Solo Club", tag="SOLO")
        solo_membership = make_mock_membership(
            solo_team, str(target_member.id), RosterRole.MID, is_captain=True
        )
        user_teams = [(solo_team, solo_membership)]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        assert view.selected_team_id == solo_team.id
        assert view.selected_role is None
        assert view.team_select.disabled is False
        assert view.save_button.disabled is False
        assert len(view.team_select.options) == 1

        opt = view.team_select.options[0]
        assert opt.value == str(solo_team.id)
        assert opt.default is True
        assert opt.label == "Solo Club [SOLO]"
        assert "Capitán" in (opt.description or "")

    @pytest.mark.asyncio
    async def test_zero_teams_graceful_handling_and_no_crashes(self) -> None:
        """
        Verify that initializing with 0 teams:
        - Does not raise any exception.
        - Sets selected_team_id to None.
        - Disables TeamSelect.
        - Disables SaveButton.
        - Has a single placeholder option with value='none' and disabled message.
        - Attempting to invoke callbacks handles the state gracefully without crashing.
        """
        target_member = make_mock_member(user_id=402, name="FreeAgent")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        view = GestionarPosicionView(
            member=target_member,
            user_teams=[],
            roster_sync_service=service,
            actor=actor_member,
        )

        assert view.selected_team_id is None
        assert view.selected_role is None
        assert view.team_select.disabled is True
        assert view.save_button.disabled is True

        assert len(view.team_select.options) == 1
        placeholder = view.team_select.options[0]
        assert placeholder.value == "none"
        assert placeholder.label == "Sin equipos asignados"

        # Adversarial probe 1: Trigger TeamSelect callback with ['none']
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.team_select, ["none"])
        await view.team_select.callback(interaction)
        assert view.selected_team_id is None
        # Defer should NOT be called when value is 'none'
        interaction.response.defer.assert_not_awaited()

        # Adversarial probe 2: Trigger TeamSelect callback with empty list []
        interaction = make_mock_interaction(user=actor_member)
        set_select_values(view.team_select, [])
        await view.team_select.callback(interaction)
        assert view.selected_team_id is None
        interaction.response.defer.assert_not_awaited()

        # Adversarial probe 3: Force SaveButton callback despite being disabled
        interaction = make_mock_interaction(user=actor_member)
        await view.save_button.callback(interaction)
        service.change_player_position.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.call_args[0][0]
        assert "Faltan campos por seleccionar" in sent_text
        assert "equipo" in sent_text
        assert "posición/rol" in sent_text

        # Adversarial probe 4: CancelButton works cleanly on empty view
        interaction = make_mock_interaction(user=actor_member)
        await view.cancel_button.callback(interaction)
        assert view.is_finished()
        assert all(child.disabled for child in view.children)

    def test_five_teams_representation_and_no_auto_selection(self) -> None:
        """
        Verify that initializing with 5 teams:
        - Does NOT auto-select any team (selected_team_id is None).
        - Correctly maps all 5 teams into TeamSelect options.
        - Formats labels, tags, roles, and captain badges accurately.
        - Ensures no option has default=True initially.
        - Leaves TeamSelect and SaveButton enabled.
        """
        target_member = make_mock_member(user_id=403, name="BusyStaff")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        team_configs = [
            ("Team Red", "RED", RosterRole.COACH, False),
            ("Team Blue", "BLU", RosterRole.STAFF, False),
            ("Team Green", "GRN", RosterRole.PARTNERS, False),
            ("Team Yellow", "YEL", RosterRole.MID, True),
            ("Team Purple", "PUR", RosterRole.SUBSTITUTE, False),
        ]

        teams = [make_mock_team(name=name, tag=tag) for name, tag, _, _ in team_configs]
        user_teams = [
            (
                team,
                make_mock_membership(team, str(target_member.id), role=role, is_captain=is_captain),
            )
            for team, (_, _, role, is_captain) in zip(teams, team_configs, strict=True)
        ]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        assert view.selected_team_id is None, "Must not auto-select when multiple teams exist"
        assert view.selected_role is None
        assert view.team_select.disabled is False
        assert view.save_button.disabled is False
        assert len(view.team_select.options) == 5

        for opt, (team, (name, tag, role, is_captain)) in zip(
            view.team_select.options, zip(teams, team_configs, strict=True), strict=True
        ):
            assert opt.value == str(team.id)
            assert opt.label == f"{name} [{tag}]"
            assert opt.default is False
            assert f"Rol actual: {role.value}" in (opt.description or "")
            if is_captain:
                assert "(Capitán)" in (opt.description or "")
            else:
                assert "(Capitán)" not in (opt.description or "")


# ---------------------------------------------------------------------------
# 3. Challenge: Concurrency, Error Recovery & End-to-End Database Integration
# ---------------------------------------------------------------------------


class TestAdversarialConcurrencyAndEndToEnd:
    """Stress-test concurrent callbacks and full end-to-end flow with PGlite."""

    @pytest.mark.asyncio
    async def test_concurrent_dropdown_interactions(self) -> None:
        """
        Verify that concurrent invocations of TeamSelect and PositionSelect callbacks
        do not deadlock or create inconsistent race-condition states.
        """
        target_member = make_mock_member(user_id=501, name="ConcurrentTester")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        t1 = make_mock_team(name="Team One", tag="T1")
        t2 = make_mock_team(name="Team Two", tag="T2")
        user_teams = [
            (t1, make_mock_membership(t1, str(target_member.id))),
            (t2, make_mock_membership(t2, str(target_member.id))),
        ]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        inter1 = make_mock_interaction(user=actor_member)
        inter2 = make_mock_interaction(user=actor_member)

        set_select_values(view.team_select, [str(t2.id)])
        set_select_values(view.position_select, ["mid"])

        # Run both callbacks simultaneously
        await asyncio.gather(
            view.team_select.callback(inter1),
            view.position_select.callback(inter2),
        )

        assert view.selected_team_id == t2.id
        assert view.selected_role == RosterRole.MID
        inter1.response.defer.assert_awaited_once()
        inter2.response.defer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interaction_check_blocks_unauthorized_actors(self) -> None:
        """
        Verify that any user other than the instantiating actor is strictly rejected
        and cannot interact with the controls or mutate view state.
        """
        target_member = make_mock_member(user_id=601, name="TargetPlayer")
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        intruder = make_mock_member(user_id=666, name="IntruderUser")
        service = MagicMock(spec=RosterSyncService)

        t1 = make_mock_team(name="Team One")
        user_teams = [(t1, make_mock_membership(t1, str(target_member.id)))]

        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        intruder_interaction = make_mock_interaction(user=intruder)
        allowed = await view.interaction_check(intruder_interaction)

        assert allowed is False
        intruder_interaction.response.send_message.assert_awaited_once()
        call_msg = intruder_interaction.response.send_message.call_args[0][0]
        assert "No tienes autorización" in call_msg
        assert intruder_interaction.response.send_message.call_args[1].get("ephemeral") is True

    def test_resilient_embed_builders_with_edge_cases(self) -> None:
        """
        Verify that build_initial_embed and build_success_embed handle edge cases gracefully:
        - Target member without avatar (display_avatar is None).
        - Selected team ID not present in user_teams fallback.
        - Missing previous membership in user_teams fallback.
        """
        target_member_no_avatar = make_mock_member(
            user_id=701, name="NoAvatarUser", with_avatar=False
        )
        actor_member = make_mock_member(user_id=999, name="StaffOperator")
        service = MagicMock(spec=RosterSyncService)

        view = GestionarPosicionView(
            member=target_member_no_avatar,
            user_teams=[],
            roster_sync_service=service,
            actor=actor_member,
        )

        initial_embed = view.build_initial_embed()
        assert initial_embed.thumbnail.url is None
        team_count_field = [
            f.value for f in initial_embed.fields if f.name == "🛡️ Equipos Pertenecientes"
        ][0]
        assert "0" in team_count_field

        # Success embed with unmapped team ID and no avatar
        unknown_team_id = uuid.uuid4()
        view.selected_team_id = unknown_team_id
        mock_updated = make_mock_membership(
            team=make_mock_team(team_id=unknown_team_id),
            discord_user_id=str(target_member_no_avatar.id),
            role=RosterRole.COACH,
            is_captain=False,
        )

        success_embed = view.build_success_embed(mock_updated)
        team_name_field = [f.value for f in success_embed.fields if f.name == "🛡️ Equipo"][0]
        assert str(unknown_team_id) in team_name_field
        prev_role_field = [f.value for f in success_embed.fields if f.name == "📋 Rol Anterior"][0]
        assert "`Desconocido`" in prev_role_field

    @pytest.mark.asyncio
    async def test_end_to_end_conflict_recovery_with_real_pglite(
        self, migrated_db: AsyncEngine
    ) -> None:
        """
        Full End-to-End integration test using real PGlite database:
        1. Target user belongs to Team A (as competitive MID) and Team B (as STAFF).
        2. Operator opens GestionarPosicionView.
        3. Operator selects Team B and tries to assign competitive role 'top'.
        4. SaveButton calls real RosterSyncService, which triggers CompetitivePositionConflictError.
        5. View displays conflict embed ephemerally without crashing, staying active.
        6. Operator recovers: changes role to non-competitive 'coach' and clicks Save.
        7. Real RosterSyncService persists change, writes movement and audit log in PGlite.
        8. View finishes, components are disabled, and success embed is emitted.
        """
        session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
        settings = Settings(guild_id=123, staff_role_id=456)
        service = RosterSyncService(session_factory, settings)

        actor_discord_id = "999888777"
        target_discord_id = "111222333"

        # 1. Seed database with DiscordUsers, Teams, and Memberships
        team_a_id = uuid.uuid4()
        team_b_id = uuid.uuid4()

        async with session_factory() as session:
            actor_user = DiscordUser(discord_id=actor_discord_id, username="StaffOperator")
            target_user = DiscordUser(discord_id=target_discord_id, username="PlayerOne")
            session.add_all([actor_user, target_user])

            player = Player(
                id=uuid.uuid4(),
                discord_user_id=target_discord_id,
                game_name="PlayerOne#EUW",
                is_main=True,
            )
            session.add(player)

            team_a = Team(
                id=team_a_id,
                name="Team Alpha",
                tag="ALP",
                slug="team-alpha",
                division=Division.PREMIER,
                discord_role_id=2001,
            )
            team_b = Team(
                id=team_b_id,
                name="Team Beta",
                tag="BET",
                slug="team-beta",
                division=Division.PREMIER,
                discord_role_id=2002,
            )
            session.add_all([team_a, team_b])

            # Target is competitive MID in Team A, and non-competitive STAFF in Team B
            mem_a = TeamMembership(
                team_id=team_a_id,
                discord_user_id=target_discord_id,
                role=RosterRole.MID,
                is_captain=False,
            )
            mem_b = TeamMembership(
                team_id=team_b_id,
                discord_user_id=target_discord_id,
                role=RosterRole.STAFF,
                is_captain=False,
            )
            session.add_all([mem_a, mem_b])
            await session.commit()

        # Retrieve user teams via service
        user_teams = await service.get_user_teams(target_discord_id)
        assert len(user_teams) == 2

        target_member = make_mock_member(user_id=int(target_discord_id), name="PlayerOne")
        actor_member = make_mock_member(user_id=int(actor_discord_id), name="StaffOperator")

        # 2. Instantiate View
        view = GestionarPosicionView(
            member=target_member,
            user_teams=user_teams,
            roster_sync_service=service,
            actor=actor_member,
        )

        # 3. Select Team B and attempt competitive role TOP
        inter_team = make_mock_interaction(user=actor_member)
        set_select_values(view.team_select, [str(team_b_id)])
        await view.team_select.callback(inter_team)

        inter_role = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["top"])
        await view.position_select.callback(inter_role)

        assert view.selected_team_id == team_b_id
        assert view.selected_role == RosterRole.TOP

        # 4. Click SaveButton -> Triggers Conflict!
        inter_save1 = make_mock_interaction(user=actor_member)
        inter_save1.response.is_done.return_value = True  # Simulated deferral
        await view.save_button.callback(inter_save1)

        # 5. Verify conflict embed was sent ephemerally and view remains alive
        inter_save1.followup.send.assert_awaited_once()
        conflict_kwargs = inter_save1.followup.send.call_args.kwargs
        assert conflict_kwargs.get("ephemeral") is True
        conflict_embed = conflict_kwargs.get("embed")
        assert conflict_embed is not None
        assert "Conflicto de Posición Competitiva" in conflict_embed.title
        assert view.is_finished() is False, "View must remain active after conflict error"
        assert not any(child.disabled for child in view.children)

        # 6. Operator recovers: switch role to non-competitive COACH
        inter_role_recovery = make_mock_interaction(user=actor_member)
        set_select_values(view.position_select, ["coach"])
        await view.position_select.callback(inter_role_recovery)
        assert view.selected_role == RosterRole.COACH

        # 7. Click SaveButton again -> Must succeed!
        inter_save2 = make_mock_interaction(user=actor_member)
        inter_save2.response.is_done.return_value = True
        await view.save_button.callback(inter_save2)

        # 8. Verify success receipt and view completion
        assert view.is_finished() is True, "View must be stopped on success"
        assert all(child.disabled for child in view.children), "All components must be disabled"
        inter_save2.edit_original_response.assert_awaited_once()
        edit_kwargs = inter_save2.edit_original_response.call_args.kwargs
        success_embed = edit_kwargs.get("embed")
        assert success_embed is not None
        assert "Posición Actualizada Exitosamente" in success_embed.title

        # 9. Verify real database state in PGlite
        async with session_factory() as session:
            membership_repo = TeamMembershipRepository(session)
            movement_repo = RosterMovementRepository(session)
            audit_repo = AuditLogRepository(session)

            updated_mem = await membership_repo.get(team_b_id, target_discord_id)
            assert updated_mem is not None
            assert updated_mem.role == RosterRole.COACH

            # Verify movement recorded
            movements = await movement_repo.list_by_team(team_b_id)
            assert len(movements) == 1
            assert movements[0].action == RosterMovementAction.ROLE_CHANGED
            assert movements[0].role == RosterRole.COACH
            assert movements[0].actor_id == actor_discord_id

            # Verify audit log recorded
            audit_entries = await audit_repo.list_by_actor(actor_discord_id)
            assert len(audit_entries) == 1
            assert audit_entries[0].action == "roster.role_changed"
            assert audit_entries[0].before == {
                "team_id": str(team_b_id),
                "discord_user_id": target_discord_id,
                "role": "staff",
                "is_captain": False,
            }
            assert audit_entries[0].after == {
                "team_id": str(team_b_id),
                "discord_user_id": target_discord_id,
                "role": "coach",
                "is_captain": False,
            }
