"""
Pruebas exhaustivas para la prevención de auto-aprobación y auto-promoción de roles (Requisito R4).

Valida:
1. ConfirmarRolButton (UI Layer):
   - Rechazo inmediato efímero si interaction.user.id == self.user_id.
   - Aprobación permitida si interaction.user.id != self.user_id para staff.
   - Ninguna inspección de interaction.message.author (el mensaje es creado por el bot).
2. TicketView (UI Layer):
   - Rechazo inmediato efímero si self.user_id is not None y interaction.user.id == self.user_id.
   - Rechazo efímero si self.user_id is None pero el servicio detecta auto-denegación.
   - Denegación permitida si interaction.user.id != solicitante.
   - Preservación del prefijo "Error: " para errores genéricos de servicio.
3. RoleService.confirm_role_request (Service Layer):
   - Rechazo si staff_member.id == req.user_id sin modificar BD ni llamar a la API de Discord.
   - Éxito si staff_member.id != req.user_id.
4. RoleService.deny_role_request (Service Layer):
   - Rechazo si staff_member.id == req.user_id sin modificar BD.
   - Éxito si staff_member.id != req.user_id.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.config import Settings
from liga_bot.models.enums import RoleRequestStatus
from liga_bot.repositories.role_request_repo import RoleRequestRepository
from liga_bot.services.role_service import RoleService
from liga_bot.ui.roles import ConfirmarRolButton, TicketView

# ---------------------------------------------------------------------------
# Helpers y Mocks Auxiliares para Discord
# ---------------------------------------------------------------------------


def make_mock_user(user_id: int = 123456789, name: str = "TestUser") -> MagicMock:
    """Crea un mock de discord.Member/discord.User."""
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.name = name
    user.display_name = name
    user.mention = f"<@{user_id}>"
    user.roles = []
    user.guild_permissions = MagicMock()
    user.guild_permissions.administrator = False
    user.guild_permissions.manage_guild = False
    return user


def make_mock_channel(channel_id: int = 987654321, name: str = "rol-testuser") -> MagicMock:
    """Crea un mock de discord.TextChannel."""
    chan = MagicMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    chan.delete = AsyncMock()
    return chan


def make_mock_guild() -> MagicMock:
    """Crea un mock básico de discord.Guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1547725310508667010
    guild.name = "RCL Test Server"
    guild.roles = []
    guild.get_member = MagicMock(return_value=None)
    guild.get_role = MagicMock(return_value=None)
    guild.get_channel = MagicMock(return_value=None)
    return guild


def make_mock_role_service() -> MagicMock:
    """Crea un mock de RoleService para pruebas de UI."""
    service = MagicMock()
    service.confirm_role_request = AsyncMock(
        return_value=(True, "Rol Vanguard Gaming confirmado para TestUser.")
    )
    service.deny_role_request = AsyncMock(return_value=(True, "Solicitud de rol denegada."))
    return service


def make_mock_interaction(
    user: MagicMock | None = None,
    guild: MagicMock | None = None,
    channel: MagicMock | None = None,
    role_service: MagicMock | None = None,
    message_author: MagicMock | None = None,
) -> MagicMock:
    """Crea un mock de discord.Interaction."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user or make_mock_user()
    interaction.guild = guild or make_mock_guild()
    interaction.channel = channel or make_mock_channel()
    interaction.channel_id = interaction.channel.id

    # El mensaje del ticket pertenece al bot, no al solicitante
    bot_author = message_author or make_mock_user(user_id=999999999, name="LigaBot")
    mock_message = MagicMock(spec=discord.Message)
    mock_message.author = bot_author
    interaction.message = mock_message

    client = MagicMock()
    client.role_service = role_service or make_mock_role_service()
    interaction.client = client

    response = MagicMock(spec=discord.InteractionResponse)
    response.is_done = MagicMock(return_value=False)
    response.send_message = AsyncMock()
    response.defer = AsyncMock()
    interaction.response = response

    return interaction


# ---------------------------------------------------------------------------
# Fixtures de Persistencia y Servicio
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_settings() -> Settings:
    """Configuración predecible para pruebas de RoleService."""
    return Settings(
        guild_id=1547725310508667010,
        staff_role_id=1547729760384319518,
        admin_role_id=1548795786110967919,
        ceo_role_id=1548795782360993842,
        sin_verificar_role_id=1550000000000000001,
        ticket_rol_category_id=1550000000000000002,
        free_role_name="Libre",
    )


@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Sesión limpia con truncado de role_requests al finalizar."""
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()
        await session.execute(text("TRUNCATE TABLE role_requests CASCADE;"))
        await session.commit()


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factoría de sesiones asíncronas para pruebas de persistencia."""
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest.fixture
def role_service(
    session_factory: async_sessionmaker[AsyncSession], clean_settings: Settings
) -> RoleService:
    """Instancia de RoleService configurada con factoría de BD real."""
    return RoleService(session_factory=session_factory, settings=clean_settings)


def create_mock_member(
    user_id: int,
    name: str = "Player",
    guild: MagicMock | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.Member con métodos de gestión de roles."""
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.roles = []
    member.guild = guild
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.edit = AsyncMock()
    return member


# ===========================================================================
# 1. Pruebas de ConfirmarRolButton (Capa UI)
# ===========================================================================


class TestConfirmarRolButtonAntiSelfApproval:
    """Pruebas de anti-auto-aprobación en ConfirmarRolButton."""

    @pytest.mark.asyncio
    async def test_callback_rejected_when_applicant_clicks_own_button(self) -> None:
        """
        Valida que si el usuario que pulsa el botón es el mismo que solicitó el rol
        (interaction.user.id == self.user_id), se rechaza inmediatamente con un
        mensaje efímero de advertencia sin llamar al servicio ni programar el borrado.
        """
        applicant_id = 777123456
        btn = ConfirmarRolButton(user_id=applicant_id, equipo="Vanguard Gaming")

        acting_user = make_mock_user(user_id=applicant_id, name="SelfRequester")
        service = make_mock_role_service()
        interaction = make_mock_interaction(user=acting_user, role_service=service)

        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(btn, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await btn.callback(interaction)

            # Debe enviar el mensaje efímero exacto requerido
            interaction.response.send_message.assert_awaited_once_with(
                "❌ No puedes confirmar ni denegar tu propia solicitud de rol.",
                ephemeral=True,
            )
            # No debe llamar al servicio ni programar la eliminación del canal
            service.confirm_role_request.assert_not_called()
            mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_allowed_for_distinct_staff_member(self) -> None:
        """
        Valida que un miembro de staff distinto al solicitante
        (interaction.user.id != self.user_id) pueda confirmar el rol con éxito.
        """
        applicant_id = 777123456
        staff_id = 999888777
        btn = ConfirmarRolButton(user_id=applicant_id, equipo="Vanguard Gaming")

        acting_staff = make_mock_user(user_id=staff_id, name="StaffApprover")
        service = make_mock_role_service()
        service.confirm_role_request = AsyncMock(
            return_value=(True, "Rol Vanguard Gaming confirmado para SelfRequester.")
        )
        interaction = make_mock_interaction(user=acting_staff, role_service=service)

        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(btn, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await btn.callback(interaction)

            service.confirm_role_request.assert_awaited_once_with(
                interaction.guild, interaction.channel_id, acting_staff
            )
            interaction.response.send_message.assert_awaited_once()
            msg = interaction.response.send_message.call_args[0][0]
            assert "Rol Vanguard Gaming confirmado" in msg
            assert "5 segundos" in msg
            mock_schedule.assert_called_once_with(interaction.channel)

    @pytest.mark.asyncio
    async def test_callback_does_not_inspect_message_author(self) -> None:
        """
        Garantiza que la validación no depende del autor del mensaje
        (el autor del mensaje es el bot, mientras que la validación
        contrasta estrictamente interaction.user.id contra self.user_id).
        """
        applicant_id = 777123456
        bot_id = 111222333
        btn = ConfirmarRolButton(user_id=applicant_id, equipo="Vanguard Gaming")

        acting_user = make_mock_user(user_id=applicant_id, name="SelfRequester")
        bot_user = make_mock_user(user_id=bot_id, name="LigaBot")
        service = make_mock_role_service()
        interaction = make_mock_interaction(
            user=acting_user, role_service=service, message_author=bot_user
        )

        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(btn, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await btn.callback(interaction)

            interaction.response.send_message.assert_awaited_once_with(
                "❌ No puedes confirmar ni denegar tu propia solicitud de rol.",
                ephemeral=True,
            )
            service.confirm_role_request.assert_not_called()
            mock_schedule.assert_not_called()


# ===========================================================================
# 2. Pruebas de TicketView (Capa UI)
# ===========================================================================


class TestTicketViewAntiSelfApproval:
    """Pruebas de anti-auto-denegación en TicketView."""

    @pytest.mark.asyncio
    async def test_denegar_rol_rejected_when_applicant_clicks_deny_in_bound_view(self) -> None:
        """
        Valida que si TicketView fue creada con user_id (bound view) y el solicitante
        intenta pulsar 'Denegar Rol', la acción sea rechazada de forma inmediata
        con mensaje efímero y sin llamar al servicio.
        """
        applicant_id = 777123456
        view = TicketView(user_id=applicant_id, equipo="Aegis Club")

        acting_user = make_mock_user(user_id=applicant_id, name="SelfRequester")
        service = make_mock_role_service()
        interaction = make_mock_interaction(user=acting_user, role_service=service)

        deny_button = view.children[0]
        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(view, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await deny_button.callback(interaction)

            interaction.response.send_message.assert_awaited_once_with(
                "❌ No puedes confirmar ni denegar tu propia solicitud de rol.",
                ephemeral=True,
            )
            service.deny_role_request.assert_not_called()
            mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_denegar_rol_rejected_when_unbound_view_but_service_detects_self_denial(
        self,
    ) -> None:
        """
        Valida que tras un reinicio del bot (donde TicketView se restaura sin user_id),
        si el servicio detecta auto-denegación y devuelve el mensaje de auto-aprobación,
        la vista lo muestre formateado con el prefijo '❌ ' de forma efímera.
        """
        view = TicketView(user_id=None, equipo=None)  # Vista no vinculada (bot restart)

        acting_user = make_mock_user(user_id=777123456, name="SelfRequester")
        service = make_mock_role_service()
        service.deny_role_request = AsyncMock(
            return_value=(False, "No puedes confirmar ni denegar tu propia solicitud de rol.")
        )
        interaction = make_mock_interaction(user=acting_user, role_service=service)

        deny_button = view.children[0]
        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(view, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await deny_button.callback(interaction)

            service.deny_role_request.assert_awaited_once_with(
                interaction.guild, interaction.channel_id, acting_user
            )
            interaction.response.send_message.assert_awaited_once_with(
                "❌ No puedes confirmar ni denegar tu propia solicitud de rol.",
                ephemeral=True,
            )
            mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_denegar_rol_allowed_for_distinct_staff_member(self) -> None:
        """
        Valida que un miembro de staff distinto al solicitante pueda denegar el rol con éxito.
        """
        applicant_id = 777123456
        staff_id = 999888777
        view = TicketView(user_id=applicant_id, equipo="Aegis Club")

        acting_staff = make_mock_user(user_id=staff_id, name="StaffDenier")
        service = make_mock_role_service()
        service.deny_role_request = AsyncMock(return_value=(True, "Solicitud de rol denegada."))
        interaction = make_mock_interaction(user=acting_staff, role_service=service)

        deny_button = view.children[0]
        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(view, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await deny_button.callback(interaction)

            service.deny_role_request.assert_awaited_once_with(
                interaction.guild, interaction.channel_id, acting_staff
            )
            interaction.response.send_message.assert_awaited_once()
            msg = interaction.response.send_message.call_args[0][0]
            assert "Solicitud de rol denegada." in msg
            assert "5 segundos" in msg
            mock_schedule.assert_called_once_with(interaction.channel)

    @pytest.mark.asyncio
    async def test_denegar_rol_preserves_generic_error_formatting(self) -> None:
        """
        Valida que los errores de servicio genéricos sigan formateándose como
        'Error: {msg}' para mantener compatibilidad con las suites existentes.
        """
        view = TicketView(user_id=None, equipo=None)
        acting_staff = make_mock_user(user_id=999888777, name="StaffMember")
        service = make_mock_role_service()
        service.deny_role_request = AsyncMock(
            return_value=(False, "No hay solicitud pendiente asociada a este canal.")
        )
        interaction = make_mock_interaction(user=acting_staff, role_service=service)

        deny_button = view.children[0]
        with (
            patch("liga_bot.cogs.permissions.is_staff", new_callable=AsyncMock) as mock_is_staff,
            patch.object(view, "_schedule_deletion") as mock_schedule,
        ):
            mock_is_staff.return_value = True

            await deny_button.callback(interaction)

            interaction.response.send_message.assert_awaited_once_with(
                "Error: No hay solicitud pendiente asociada a este canal.",
                ephemeral=True,
            )
            mock_schedule.assert_not_called()


# ===========================================================================
# 3. Pruebas de RoleService.confirm_role_request (Capa de Servicio)
# ===========================================================================


class TestRoleServiceConfirmAntiSelfApproval:
    """Pruebas de anti-auto-aprobación a nivel de servicio en confirm_role_request."""

    @pytest.mark.asyncio
    async def test_confirm_role_request_fails_when_staff_is_applicant(
        self,
        role_service: RoleService,
        clean_settings: Settings,
        db_session: AsyncSession,
    ) -> None:
        """
        Valida que confirm_role_request falle si staff_member.id == req.user_id.
        Verifica que el estado en base de datos permanezca PENDING, staff_id sea None y
        no se invoquen llamadas a la API de Discord (add_roles, remove_roles, edit).
        """
        applicant_id = 444001
        channel_id = 777444

        # Crear solicitud en estado PENDING
        repo = RoleRequestRepository(db_session)
        req = await repo.create_request(
            user_id=applicant_id,
            nombre_lol="SelfPromoter",
            riot_tag="EUW",
            equipo="Vanguard Gaming",
            canal_id=channel_id,
        )
        req_id = req.id

        guild = MagicMock(spec=discord.Guild)
        guild.id = clean_settings.guild_id
        guild.name = "RCL Test Server"

        # El miembro de staff es el propio solicitante
        staff_applicant = create_mock_member(user_id=applicant_id, name="SelfPromoter", guild=guild)
        guild.get_member = MagicMock(return_value=staff_applicant)

        ok, msg = await role_service.confirm_role_request(
            guild=guild,
            channel_id=channel_id,
            staff_member=staff_applicant,
        )

        assert ok is False
        assert msg == "No puedes confirmar ni denegar tu propia solicitud de rol."

        # Verificar integridad en base de datos: el registro sigue PENDING
        refreshed_req = await repo.get_by_id(req_id)
        assert refreshed_req is not None
        assert refreshed_req.estado == RoleRequestStatus.PENDING
        assert refreshed_req.staff_id is None

        # Verificar que no se realizaron llamadas de Discord a add_roles, remove_roles o edit
        staff_applicant.add_roles.assert_not_called()
        staff_applicant.remove_roles.assert_not_called()
        staff_applicant.edit.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_role_request_succeeds_when_staff_is_different_user(
        self,
        role_service: RoleService,
        clean_settings: Settings,
        db_session: AsyncSession,
    ) -> None:
        """
        Valida que confirm_role_request proceda si staff_member.id != req.user_id.
        """
        applicant_id = 444002
        staff_id = 999002
        channel_id = 777445

        repo = RoleRequestRepository(db_session)
        req = await repo.create_request(
            user_id=applicant_id,
            nombre_lol="GoodPlayer",
            riot_tag="KR",
            equipo="Vanguard Gaming",
            canal_id=channel_id,
        )
        req_id = req.id

        guild = MagicMock(spec=discord.Guild)
        guild.id = clean_settings.guild_id
        guild.name = "RCL Test Server"

        applicant_member = create_mock_member(user_id=applicant_id, name="GoodPlayer", guild=guild)
        staff_member = create_mock_member(user_id=staff_id, name="OfficialStaff", guild=guild)
        guild.get_member = MagicMock(return_value=applicant_member)

        mock_role = MagicMock(spec=discord.Role)
        mock_role.id = 1548000000000000001
        mock_role.name = "Vanguard Gaming"
        guild.roles = [mock_role]
        guild.get_role = MagicMock(return_value=mock_role)

        ok, msg = await role_service.confirm_role_request(
            guild=guild,
            channel_id=channel_id,
            staff_member=staff_member,
        )

        assert ok is True
        assert "confirmado" in msg

        refreshed_req = await repo.get_by_id(req_id)
        assert refreshed_req is not None
        assert refreshed_req.estado == RoleRequestStatus.APPROVED
        assert refreshed_req.staff_id == staff_id


# ===========================================================================
# 4. Pruebas de RoleService.deny_role_request (Capa de Servicio)
# ===========================================================================


class TestRoleServiceDenyAntiSelfApproval:
    """Pruebas de anti-auto-denegación a nivel de servicio en deny_role_request."""

    @pytest.mark.asyncio
    async def test_deny_role_request_fails_when_staff_is_applicant(
        self,
        role_service: RoleService,
        clean_settings: Settings,
        db_session: AsyncSession,
    ) -> None:
        """
        Valida que deny_role_request falle y retorne (False, ...) si staff_member.id == req.user_id.
        Verifica que el estado en base de datos permanezca PENDING y staff_id sea None.
        """
        applicant_id = 555001
        channel_id = 888555

        repo = RoleRequestRepository(db_session)
        req = await repo.create_request(
            user_id=applicant_id,
            nombre_lol="SelfDenier",
            riot_tag="NA",
            equipo="Vanguard Gaming",
            canal_id=channel_id,
        )
        req_id = req.id

        guild = MagicMock(spec=discord.Guild)
        guild.id = clean_settings.guild_id
        guild.name = "RCL Test Server"

        staff_applicant = create_mock_member(user_id=applicant_id, name="SelfDenier", guild=guild)

        ok, msg = await role_service.deny_role_request(
            guild=guild,
            channel_id=channel_id,
            staff_member=staff_applicant,
        )

        assert ok is False
        assert msg == "No puedes confirmar ni denegar tu propia solicitud de rol."

        # Estado en BD debe permanecer PENDING sin registrar staff_id
        refreshed_req = await repo.get_by_id(req_id)
        assert refreshed_req is not None
        assert refreshed_req.estado == RoleRequestStatus.PENDING
        assert refreshed_req.staff_id is None

    @pytest.mark.asyncio
    async def test_deny_role_request_succeeds_when_staff_is_different_user(
        self,
        role_service: RoleService,
        clean_settings: Settings,
        db_session: AsyncSession,
    ) -> None:
        """
        Valida que deny_role_request proceda con normalidad cuando staff_member.id != req.user_id.
        """
        applicant_id = 555002
        staff_id = 999003
        channel_id = 888556

        repo = RoleRequestRepository(db_session)
        req = await repo.create_request(
            user_id=applicant_id,
            nombre_lol="DeniedPlayer",
            riot_tag="LAN",
            equipo="Vanguard Gaming",
            canal_id=channel_id,
        )
        req_id = req.id

        guild = MagicMock(spec=discord.Guild)
        guild.id = clean_settings.guild_id
        guild.name = "RCL Test Server"

        staff_member = create_mock_member(user_id=staff_id, name="OfficialStaffDenier", guild=guild)

        ok, msg = await role_service.deny_role_request(
            guild=guild,
            channel_id=channel_id,
            staff_member=staff_member,
        )

        assert ok is True
        assert msg == "Solicitud de rol denegada."

        refreshed_req = await repo.get_by_id(req_id)
        assert refreshed_req is not None
        assert refreshed_req.estado == RoleRequestStatus.DENIED
        assert refreshed_req.staff_id == staff_id
