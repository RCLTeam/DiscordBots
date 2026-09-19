"""
Pruebas exhaustivas (unitarias y de integración) para RoleService
en src/liga_bot/services/role_service.py y su integración con LigaBot.
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.bot import LigaBot
from liga_bot.config import Settings
from liga_bot.models.enums import RoleRequestStatus
from liga_bot.repositories.role_request_repo import RoleRequestRepository
from liga_bot.services.role_service import RoleService


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


def create_mock_category(category_id: int, name: str) -> MagicMock:
    """Crea un mock de discord.CategoryChannel."""
    cat = MagicMock(spec=discord.CategoryChannel)
    cat.id = category_id
    cat.name = name
    cat.channels = []
    return cat


def create_mock_channel(
    channel_id: int,
    name: str,
    category: MagicMock | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.TextChannel."""
    chan = AsyncMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.name = name
    chan.category = category
    chan.guild = guild
    chan.mention = f"<#{channel_id}>"
    chan.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    chan.delete = AsyncMock()
    return chan


def create_mock_member(
    user_id: int,
    name: str = "Player",
    display_name: str | None = None,
    roles: list[MagicMock] | None = None,
    guild: MagicMock | None = None,
) -> AsyncMock:
    """Crea un mock asíncrono de discord.Member con métodos de gestión de roles y DMs."""
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
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
    channels: list[MagicMock] | None = None,
) -> MagicMock:
    """Crea un mock completo de discord.Guild con roles, canales y miembros simulados."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = settings.guild_id
    guild.name = "RCL League Server"

    # Default role (@everyone)
    default_role = create_mock_role(settings.guild_id, "@everyone")
    guild.default_role = default_role

    # Bot Member
    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999999999999
    bot_member.name = "LigaBot"
    guild.me = bot_member

    # Roles configurados
    staff_role = (
        create_mock_role(settings.staff_role_id, "Staff") if settings.staff_role_id else None
    )
    ceo_role = create_mock_role(settings.ceo_role_id, "CEO") if settings.ceo_role_id else None
    sin_verificar_role = (
        create_mock_role(settings.sin_verificar_role_id, "Sin Verificar")
        if settings.sin_verificar_role_id
        else None
    )
    free_role = create_mock_role(888001, settings.free_role_name)

    all_roles = [default_role]
    if staff_role:
        all_roles.append(staff_role)
    if ceo_role:
        all_roles.append(ceo_role)
    if sin_verificar_role:
        all_roles.append(sin_verificar_role)
    all_roles.append(free_role)
    if roles:
        all_roles.extend(roles)

    role_map = {r.id: r for r in all_roles}
    guild.roles = all_roles
    guild.get_role.side_effect = lambda rid: role_map.get(rid)

    # Canales
    channel_list = list(channels or [])
    channel_map = {c.id: c for c in channel_list}
    guild.channels = channel_list
    guild.get_channel.side_effect = lambda cid: channel_map.get(cid)

    # Miembros en caché y API
    members_map: dict[int, AsyncMock] = {}
    guild._members_map = members_map
    guild.get_member.side_effect = lambda uid: members_map.get(uid)

    async def mock_fetch_member(uid: int):
        if uid in members_map:
            return members_map[uid]
        mock_resp = MagicMock(status=404, reason="Not Found")
        raise discord.NotFound(mock_resp, f"Member {uid} not found")

    guild.fetch_member = AsyncMock(side_effect=mock_fetch_member)

    # Canal creation
    channel_seq = [5000]

    async def mock_create_text_channel(name: str, overwrites=None, category=None):
        chan = create_mock_channel(
            channel_id=channel_seq[0], name=name, category=category, guild=guild
        )
        chan.overwrites = overwrites
        channel_seq[0] += 1
        guild.channels.append(chan)
        channel_map[chan.id] = chan
        return chan

    guild.create_text_channel = AsyncMock(side_effect=mock_create_text_channel)
    return guild


# ---------------------------------------------------------------------------
# Fixtures de configuración y persistencia
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
    """Sesión limpia conectada a PGlite con truncado de role_requests al finalizar."""
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
    """Instancia de RoleService configurada con factoría de BD real y settings limpios."""
    return RoleService(session_factory=session_factory, settings=clean_settings)


# ===========================================================================
# 1. Pruebas de Inicialización e Inyección en LigaBot
# ===========================================================================
class TestRoleServiceInitialization:
    """Pruebas para inicialización de RoleService e inyección en el bot."""

    def test_init_with_explicit_dependencies(
        self, session_factory: async_sessionmaker[AsyncSession], clean_settings: Settings
    ):
        """Verifica inicialización con dependencias explícitas."""
        bot_mock = MagicMock()
        service = RoleService(
            session_factory=session_factory, settings=clean_settings, bot=bot_mock
        )

        assert service.session_factory is session_factory
        assert service.settings is clean_settings
        assert service.bot is bot_mock

    def test_init_with_defaults(self):
        """Verifica inicialización con fallbacks por defecto."""
        with (
            patch("liga_bot.services.role_service.get_session_factory") as mock_get_sf,
            patch("liga_bot.services.role_service.get_settings") as mock_get_settings,
        ):
            mock_sf = MagicMock()
            mock_settings = MagicMock()
            mock_get_sf.return_value = mock_sf
            mock_get_settings.return_value = mock_settings

            service = RoleService()

            assert service.session_factory is mock_sf
            assert service.settings is mock_settings
            assert service.bot is None

    @pytest.mark.asyncio
    async def test_bot_injects_role_service(self):
        """Verifica que LigaBot inicializa self.role_service en setup_hook."""
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

            assert bot.role_service is None
            await bot.setup_hook()

            assert isinstance(bot.role_service, RoleService)
            assert bot.role_service.session_factory is mock_session_factory
            assert bot.role_service.settings is bot.settings
            assert bot.role_service.bot is bot

            await bot.close()


# ===========================================================================
# 2. Pruebas de handle_member_join
# ===========================================================================
class TestHandleMemberJoin:
    """Pruebas unitarias para el evento de bienvenida y asignación de rol inicial."""

    @pytest.mark.asyncio
    async def test_handle_member_join_adds_role_and_sends_dm(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Asigna el rol sin verificar configurado y envía DM de bienvenida al miembro."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(111222, name="Newbie", guild=guild)

        success = await role_service.handle_member_join(member)

        assert success is True
        role = guild.get_role(clean_settings.sin_verificar_role_id)
        member.add_roles.assert_awaited_once_with(role)
        member.send.assert_awaited_once()
        sent_message = member.send.await_args.args[0]
        assert "Bienvenido" in sent_message

    @pytest.mark.asyncio
    async def test_handle_member_join_no_role_configured(self, session_factory: async_sessionmaker):
        """Si sin_verificar_role_id es 0, no asigna rol pero sí envía DM y retorna True."""
        settings = Settings(sin_verificar_role_id=0)
        service = RoleService(session_factory=session_factory, settings=settings)
        guild = create_mock_guild(settings)
        member = create_mock_member(111223, name="Guest", guild=guild)

        success = await service.handle_member_join(member)

        assert success is True
        member.add_roles.assert_not_called()
        member.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_member_join_dm_forbidden_is_handled_cleanly(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si el usuario tiene los DMs bloqueados (Forbidden), se ignora y retorna True."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(111224, name="DMsBlocked", guild=guild)
        mock_resp = MagicMock(status=403, reason="Forbidden")
        member.send.side_effect = discord.Forbidden(mock_resp, "Cannot send messages to this user")

        success = await role_service.handle_member_join(member)

        assert success is True
        role = guild.get_role(clean_settings.sin_verificar_role_id)
        member.add_roles.assert_awaited_once_with(role)
        member.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_member_join_dm_http_exception_is_handled(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si falla el envío de DM por HTTPException, se ignora y retorna True."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(111225, name="HttpErrorUser", guild=guild)
        mock_resp = MagicMock(status=500, reason="Internal Server Error")
        member.send.side_effect = discord.HTTPException(mock_resp, "Server error")

        success = await role_service.handle_member_join(member)

        assert success is True
        role = guild.get_role(clean_settings.sin_verificar_role_id)
        member.add_roles.assert_awaited_once_with(role)

    @pytest.mark.asyncio
    async def test_handle_member_join_role_not_found_in_guild(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si sin_verificar_role_id está configurado pero no existe en el guild, retorna False."""
        guild = create_mock_guild(clean_settings)
        # Vaciar roles para simular rol ausente
        guild.roles = []
        guild.get_role.side_effect = lambda rid: None
        member = create_mock_member(111226, name="OrphanUser", guild=guild)

        success = await role_service.handle_member_join(member)

        assert success is False
        member.add_roles.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_member_join_add_roles_forbidden_returns_false(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si add_roles falla por discord.Forbidden, retorna False."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(111227, name="ForbiddenMember", guild=guild)
        mock_resp = MagicMock(status=403, reason="Missing Permissions")
        member.add_roles.side_effect = discord.Forbidden(mock_resp, "Missing Permissions")

        success = await role_service.handle_member_join(member)

        assert success is False

    @pytest.mark.asyncio
    async def test_handle_member_join_add_roles_generic_exception_returns_false(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si add_roles lanza una excepción inesperada, retorna False."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(111228, name="CrashMember", guild=guild)
        member.add_roles.side_effect = RuntimeError("Fatal socket error")

        success = await role_service.handle_member_join(member)

        assert success is False


# ===========================================================================
# 3. Pruebas de assign_free_role
# ===========================================================================
class TestAssignFreeRole:
    """Pruebas unitarias y de integración para la asignación de rol de agente libre."""

    @pytest.mark.asyncio
    async def test_assign_free_role_success(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Verifica asignación exitosa de rol Libre, remoción de sin verificar, nick y DB."""
        guild = create_mock_guild(clean_settings)
        sin_verificar_role = guild.get_role(clean_settings.sin_verificar_role_id)
        member = create_mock_member(
            user_id=200001,
            name="AgenteUno",
            roles=[sin_verificar_role],
            guild=guild,
        )

        ok, msg = await role_service.assign_free_role(
            member=member,
            nombre_lol="Faker",
            riot_tag="KR1",
        )

        assert ok is True
        assert msg == "Rol Libre asignado correctamente."

        # Verificaciones en Discord
        free_role = discord.utils.get(guild.roles, name=clean_settings.free_role_name)
        member.remove_roles.assert_awaited_once_with(sin_verificar_role)
        member.add_roles.assert_awaited_once_with(free_role)
        member.edit.assert_awaited_once_with(nick="Faker #KR1")

        # Verificación en Base de Datos
        repo = RoleRequestRepository(db_session)
        requests = await repo.list_all()
        created = next((r for r in requests if r.user_id == 200001), None)
        assert created is not None
        assert created.user_id == 200001
        assert created.nombre_lol == "Faker"
        assert created.riot_tag == "KR1"
        assert created.equipo == clean_settings.free_role_name
        assert created.estado == RoleRequestStatus.APPROVED
        assert created.canal_id is None

    @pytest.mark.asyncio
    async def test_assign_free_role_role_not_found(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si el rol libre no existe en el servidor, retorna error sin modificar al usuario."""
        guild = create_mock_guild(clean_settings)
        # Eliminar el rol libre de la lista de roles
        guild.roles = [r for r in guild.roles if r.name != clean_settings.free_role_name]
        member = create_mock_member(200002, name="AgenteDos", guild=guild)

        ok, msg = await role_service.assign_free_role(
            member=member,
            nombre_lol="Chovy",
            riot_tag="KR2",
        )

        assert ok is False
        assert f"El rol '{clean_settings.free_role_name}' no existe" in msg
        member.add_roles.assert_not_called()
        member.remove_roles.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_free_role_member_does_not_have_sin_verificar(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si el miembro no tiene el rol sin verificar, no intenta removerlo."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(200003, name="AgenteTres", roles=[], guild=guild)

        ok, msg = await role_service.assign_free_role(
            member=member,
            nombre_lol="Caps",
            riot_tag="EU1",
        )

        assert ok is True
        member.remove_roles.assert_not_called()
        free_role = discord.utils.get(guild.roles, name=clean_settings.free_role_name)
        member.add_roles.assert_awaited_once_with(free_role)

    @pytest.mark.asyncio
    async def test_assign_free_role_sin_verificar_not_configured(
        self, session_factory: async_sessionmaker
    ):
        """Si sin_verificar_role_id es 0, no intenta remover nada."""
        settings = Settings(sin_verificar_role_id=0, free_role_name="Libre")
        service = RoleService(session_factory=session_factory, settings=settings)
        guild = create_mock_guild(settings)
        member = create_mock_member(200004, name="AgenteCuatro", guild=guild)

        ok, msg = await service.assign_free_role(
            member=member,
            nombre_lol="Ruler",
            riot_tag="KR3",
        )

        assert ok is True
        member.remove_roles.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_free_role_nick_forbidden_does_not_abort(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si editar el apodo falla por permisos (Forbidden), continúa y aprueba en BD."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(200005, name="OwnerPlayer", guild=guild)
        mock_resp = MagicMock(status=403, reason="Forbidden")
        member.edit.side_effect = discord.Forbidden(mock_resp, "Cannot change nickname of owner")

        ok, msg = await role_service.assign_free_role(
            member=member,
            nombre_lol="ShowMaker",
            riot_tag="DK1",
        )

        assert ok is True
        assert msg == "Rol Libre asignado correctamente."

    @pytest.mark.asyncio
    async def test_assign_free_role_nickname_truncated_to_32_chars(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si el nombre y tag superan los 32 caracteres, el nick se trunca a 32."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(200006, name="LongNameUser", guild=guild)

        very_long_name = "SuperMegaUltraLongSummonerName"
        very_long_tag = "Tag12345"

        ok, _ = await role_service.assign_free_role(
            member=member,
            nombre_lol=very_long_name,
            riot_tag=very_long_tag,
        )

        assert ok is True
        expected_nick = f"{very_long_name} #{very_long_tag}"[:32]
        assert len(expected_nick) == 32
        member.edit.assert_awaited_once_with(nick=expected_nick)

    @pytest.mark.asyncio
    async def test_assign_free_role_add_roles_forbidden(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si add_roles lanza Forbidden, retorna tupla de error."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(200007, name="ForbiddenUser", guild=guild)
        mock_resp = MagicMock(status=403, reason="Missing Permissions")
        member.add_roles.side_effect = discord.Forbidden(mock_resp, "Missing Permissions")

        ok, msg = await role_service.assign_free_role(
            member=member,
            nombre_lol="Scout",
            riot_tag="LNG",
        )

        assert ok is False
        assert "Permisos insuficientes" in msg

    @pytest.mark.asyncio
    async def test_assign_free_role_add_roles_http_exception(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si add_roles lanza HTTPException, retorna tupla de error."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(200008, name="HttpErrorMember", guild=guild)
        mock_resp = MagicMock(status=500, reason="Discord Error")
        member.add_roles.side_effect = discord.HTTPException(mock_resp, "Discord Error")

        ok, msg = await role_service.assign_free_role(
            member=member,
            nombre_lol="Gala",
            riot_tag="LNG",
        )

        assert ok is False
        assert "Error al asignar rol" in msg

    @pytest.mark.asyncio
    async def test_assign_free_role_db_error_returns_false(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si la transacción en base de datos falla, retorna tupla de error."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(200009, name="DbFailMember", guild=guild)

        with patch(
            "liga_bot.services.role_service.transactional_session",
            side_effect=Exception("DB down"),
        ):
            ok, msg = await role_service.assign_free_role(
                member=member,
                nombre_lol="TheShy",
                riot_tag="IG",
            )

            assert ok is False
            assert "Error al registrar la asignación" in msg


# ===========================================================================
# 4. Pruebas de create_role_request_ticket
# ===========================================================================
class TestCreateRoleRequestTicket:
    """Pruebas unitarias y de integración para la creación de canales de ticket de rol."""

    @pytest.mark.asyncio
    async def test_create_ticket_duplicate_active_pending_rejected(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Si el usuario ya tiene una solicitud PENDING activa, rechaza la creación."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(300001, name="DuplicateUser", guild=guild)

        # Crear solicitud previa activa en BD
        repo = RoleRequestRepository(db_session)
        await repo.create_request(
            user_id=300001,
            nombre_lol="PrevName",
            riot_tag="PrevTag",
            equipo="Vanguard Gaming",
            canal_id=12345,
        )

        ok, msg, chan = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="NewName",
            riot_tag="NewTag",
            equipo="Nexus Esports",
        )

        assert ok is False
        assert msg == "Ya tienes una solicitud de rol pendiente."
        assert chan is None
        guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ticket_success_with_permissions_and_db(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Crea el canal con sobrescrituras estrictas, categoría y registra en BD."""
        category = create_mock_category(clean_settings.ticket_rol_category_id, "TICKETS-ROLES")
        guild = create_mock_guild(clean_settings, channels=[category])
        member = create_mock_member(300002, name="TicketRequester", guild=guild)

        ok, msg, chan = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Faker",
            riot_tag="KR1",
            equipo="Vanguard Gaming",
        )

        assert ok is True
        assert msg == "Canal de solicitud creado correctamente."
        assert chan is not None
        assert chan.name == "rol-ticketrequester"

        # Verificar llamadas a create_text_channel
        guild.create_text_channel.assert_awaited_once()
        call_kwargs = guild.create_text_channel.await_args.kwargs
        assert call_kwargs["name"] == "rol-ticketrequester"
        assert call_kwargs["category"] is category

        # Inspeccionar permisos
        overwrites = call_kwargs["overwrites"]
        assert guild.default_role in overwrites
        assert overwrites[guild.default_role].read_messages is False
        assert overwrites[guild.default_role].send_messages is False

        assert member in overwrites
        assert overwrites[member].read_messages is True
        assert overwrites[member].send_messages is True
        assert overwrites[member].attach_files is True

        assert guild.me in overwrites
        assert overwrites[guild.me].read_messages is True
        assert overwrites[guild.me].send_messages is True
        assert overwrites[guild.me].manage_channels is True

        staff_role = guild.get_role(clean_settings.staff_role_id)
        assert staff_role in overwrites
        assert overwrites[staff_role].read_messages is True
        assert overwrites[staff_role].send_messages is True

        ceo_role = guild.get_role(clean_settings.ceo_role_id)
        assert ceo_role in overwrites
        assert overwrites[ceo_role].read_messages is True
        assert overwrites[ceo_role].send_messages is True

        # Verificar registro en BD
        repo = RoleRequestRepository(db_session)
        req = await repo.get_by_channel_id(chan.id)
        assert req is not None
        assert req.user_id == 300002
        assert req.nombre_lol == "Faker"
        assert req.riot_tag == "KR1"
        assert req.equipo == "Vanguard Gaming"
        assert req.estado == RoleRequestStatus.PENDING

    @pytest.mark.asyncio
    async def test_create_ticket_without_optional_roles_or_category(
        self, session_factory: async_sessionmaker
    ):
        """Si staff_role_id=0, ceo_role_id=0 y category=0, omite roles opcionales."""
        settings = Settings(
            staff_role_id=0,
            ceo_role_id=0,
            ticket_rol_category_id=0,
        )
        service = RoleService(session_factory=session_factory, settings=settings)
        guild = create_mock_guild(settings)
        member = create_mock_member(300003, name="MinimalUser", guild=guild)

        ok, msg, chan = await service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Crisp",
            riot_tag="WBG",
            equipo="Nexus Esports",
        )

        assert ok is True
        assert chan is not None
        call_kwargs = guild.create_text_channel.await_args.kwargs
        assert call_kwargs["category"] is None
        overwrites = call_kwargs["overwrites"]
        assert len(overwrites) == 3  # default_role, member, guild.me

    @pytest.mark.asyncio
    async def test_create_ticket_discord_forbidden(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si create_text_channel lanza Forbidden, retorna tupla de error."""
        guild = create_mock_guild(clean_settings)
        mock_resp = MagicMock(status=403, reason="Missing Permissions")
        guild.create_text_channel.side_effect = discord.Forbidden(mock_resp, "Missing Permissions")
        member = create_mock_member(300004, name="ForbiddenCreator", guild=guild)

        ok, msg, chan = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Deft",
            riot_tag="DRX",
            equipo="Aegis Club",
        )

        assert ok is False
        assert "Permisos insuficientes" in msg
        assert chan is None

    @pytest.mark.asyncio
    async def test_create_ticket_discord_http_exception(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si create_text_channel lanza HTTPException, retorna tupla de error."""
        guild = create_mock_guild(clean_settings)
        mock_resp = MagicMock(status=500, reason="Internal error")
        guild.create_text_channel.side_effect = discord.HTTPException(mock_resp, "Discord 500")
        member = create_mock_member(300005, name="HttpErrorCreator", guild=guild)

        ok, msg, chan = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="BeryL",
            riot_tag="DRX",
            equipo="Aegis Club",
        )

        assert ok is False
        assert "Error al crear el canal de solicitud" in msg
        assert chan is None

    @pytest.mark.asyncio
    async def test_create_ticket_db_failure_cleans_up_orphan_channel(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si falla la persistencia en BD tras crear el canal, lo elimina para evitar huérfanos."""
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(300006, name="OrphanTester", guild=guild)

        call_count = [0]
        orig_transactional = role_service.session_factory

        # Simular que la verificación pasa pero la inserción falla
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mock_transactional_session(factory):
            call_count[0] += 1
            if call_count[0] == 1:
                # Primera llamada: active request check -> session real
                async with orig_transactional() as session:
                    yield session
            else:
                # Segunda llamada: inserción tras crear canal -> falla
                raise RuntimeError("DB connection dropped")

        with patch(
            "liga_bot.services.role_service.transactional_session",
            side_effect=mock_transactional_session,
        ):
            ok, msg, chan = await role_service.create_role_request_ticket(
                guild=guild,
                member=member,
                nombre_lol="Keria",
                riot_tag="T1",
                equipo="Aegis Club",
            )

            assert ok is False
            assert "Error al registrar la solicitud en base de datos." in msg
            assert chan is None

            # Verificar que create_text_channel fue llamado y luego channel.delete() fue ejecutado
            guild.create_text_channel.assert_awaited_once()
            created_chan = guild.channels[-1]
            created_chan.delete.assert_awaited_once()


# ===========================================================================
# 5. Pruebas de confirm_role_request
# ===========================================================================
class TestConfirmRoleRequest:
    """Pruebas para confirmación y aprobación de roles solicitados."""

    @pytest.mark.asyncio
    async def test_confirm_request_channel_not_found(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si no existe solicitud para el canal_id, retorna error descriptivo."""
        guild = create_mock_guild(clean_settings)
        staff = create_mock_member(900001, name="StaffMember", guild=guild)

        ok, msg = await role_service.confirm_role_request(
            guild=guild, channel_id=99999999, staff_member=staff
        )

        assert ok is False
        assert msg == "No hay solicitud pendiente asociada a este canal."

    @pytest.mark.asyncio
    async def test_confirm_request_already_resolved(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Si la solicitud ya fue aprobada o denegada, no permite confirmarla nuevamente."""
        guild = create_mock_guild(clean_settings)
        staff = create_mock_member(900001, name="StaffMember", guild=guild)

        repo = RoleRequestRepository(db_session)
        req = await repo.create_request(
            user_id=400001,
            nombre_lol="Peanut",
            riot_tag="HLE",
            equipo="Vanguard Gaming",
            canal_id=777001,
        )
        await repo.update_status(req.id, RoleRequestStatus.APPROVED, staff_id=900001)

        ok, msg = await role_service.confirm_role_request(
            guild=guild, channel_id=777001, staff_member=staff
        )

        assert ok is False
        assert msg == "No hay solicitud pendiente asociada a este canal."

    @pytest.mark.asyncio
    async def test_confirm_request_member_not_in_guild(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Si el usuario solicitante no está en la caché ni se encuentra vía API, retorna error."""
        guild = create_mock_guild(clean_settings)
        staff = create_mock_member(900001, name="StaffMember", guild=guild)

        repo = RoleRequestRepository(db_session)
        await repo.create_request(
            user_id=400002,
            nombre_lol="Zeka",
            riot_tag="HLE",
            equipo="Vanguard Gaming",
            canal_id=777002,
        )

        ok, msg = await role_service.confirm_role_request(
            guild=guild, channel_id=777002, staff_member=staff
        )

        assert ok is False
        assert msg == "El usuario solicitante no se encuentra en el servidor."

    @pytest.mark.asyncio
    async def test_confirm_request_success_cached_member(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Flujo exitoso: miembro en caché, asigna rol, quita sin verificar, apodo y BD."""
        team_role = create_mock_role(888101, "Vanguard Gaming")
        guild = create_mock_guild(clean_settings, roles=[team_role])
        staff = create_mock_member(900001, name="StaffBoss", guild=guild)

        sin_verificar_role = guild.get_role(clean_settings.sin_verificar_role_id)
        member = create_mock_member(
            user_id=400003,
            name="Viper",
            display_name="Viper_ADC",
            roles=[sin_verificar_role],
            guild=guild,
        )
        guild._members_map[400003] = member

        repo = RoleRequestRepository(db_session)
        await repo.create_request(
            user_id=400003,
            nombre_lol="ViperLoL",
            riot_tag="KR9",
            equipo="Vanguard Gaming",
            canal_id=777003,
        )

        ok, msg = await role_service.confirm_role_request(
            guild=guild, channel_id=777003, staff_member=staff
        )

        assert ok is True
        assert msg == "Rol Vanguard Gaming confirmado para Viper_ADC."

        # Discord
        member.add_roles.assert_awaited_once_with(team_role)
        member.remove_roles.assert_awaited_once_with(sin_verificar_role)
        member.edit.assert_awaited_once_with(nick="ViperLoL #KR9")

        # Base de Datos
        updated_req = await repo.get_by_channel_id(777003)
        assert updated_req is not None
        assert updated_req.estado == RoleRequestStatus.APPROVED
        assert updated_req.staff_id == 900001

    @pytest.mark.asyncio
    async def test_confirm_request_success_fetched_member(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Flujo exitoso cuando el miembro no está en la caché local pero se recupera vía API."""
        team_role = create_mock_role(888102, "Nexus Esports")
        guild = create_mock_guild(clean_settings, roles=[team_role])
        staff = create_mock_member(900001, name="StaffBoss", guild=guild)

        member = create_mock_member(
            user_id=400004,
            name="Delight",
            display_name="Delight_SUP",
            roles=[],
            guild=guild,
        )
        # No está en get_member, pero fetch_member lo resuelve
        guild.get_member.side_effect = lambda uid: None
        guild.fetch_member = AsyncMock(return_value=member)

        repo = RoleRequestRepository(db_session)
        await repo.create_request(
            user_id=400004,
            nombre_lol="DelightLoL",
            riot_tag="KR0",
            equipo="Nexus Esports",
            canal_id=777004,
        )

        ok, msg = await role_service.confirm_role_request(
            guild=guild, channel_id=777004, staff_member=staff
        )

        assert ok is True
        assert "Nexus Esports confirmado para Delight_SUP" in msg
        guild.fetch_member.assert_awaited_once_with(400004)
        member.add_roles.assert_awaited_once_with(team_role)

    @pytest.mark.asyncio
    async def test_confirm_request_team_role_missing_in_guild(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Si el rol de equipo no existe en el servidor, advierte pero completa apodo y BD."""
        guild = create_mock_guild(clean_settings)
        staff = create_mock_member(900001, name="StaffBoss", guild=guild)
        member = create_mock_member(user_id=400005, name="Doran", guild=guild)
        guild._members_map[400005] = member

        repo = RoleRequestRepository(db_session)
        await repo.create_request(
            user_id=400005,
            nombre_lol="Doran",
            riot_tag="TOP",
            equipo="NonExistentTeam",
            canal_id=777005,
        )

        ok, msg = await role_service.confirm_role_request(
            guild=guild, channel_id=777005, staff_member=staff
        )

        assert ok is True
        assert "Rol NonExistentTeam confirmado para Doran." in msg
        member.add_roles.assert_not_called()
        member.edit.assert_awaited_once_with(nick="Doran #TOP")

    @pytest.mark.asyncio
    async def test_confirm_request_member_edit_forbidden_does_not_abort(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Si editar el apodo falla por permisos, no interrumpe la aprobación."""
        team_role = create_mock_role(888103, "Aegis Club")
        guild = create_mock_guild(clean_settings, roles=[team_role])
        staff = create_mock_member(900001, name="StaffBoss", guild=guild)
        member = create_mock_member(user_id=400006, name="OwnerPlayer", guild=guild)
        mock_resp = MagicMock(status=403, reason="Forbidden")
        member.edit.side_effect = discord.Forbidden(mock_resp, "Cannot edit nick")
        guild._members_map[400006] = member

        repo = RoleRequestRepository(db_session)
        await repo.create_request(
            user_id=400006,
            nombre_lol="Oner",
            riot_tag="T1",
            equipo="Aegis Club",
            canal_id=777006,
        )

        ok, msg = await role_service.confirm_role_request(
            guild=guild, channel_id=777006, staff_member=staff
        )

        assert ok is True
        assert "Aegis Club confirmado para OwnerPlayer." in msg


# ===========================================================================
# 6. Pruebas de deny_role_request
# ===========================================================================
class TestDenyRoleRequest:
    """Pruebas para denegación de solicitudes de rol asociadas a canales."""

    @pytest.mark.asyncio
    async def test_deny_request_channel_not_found(
        self, role_service: RoleService, clean_settings: Settings
    ):
        """Si no existe solicitud para el channel_id, retorna error descriptivo."""
        guild = create_mock_guild(clean_settings)
        staff = create_mock_member(900001, name="StaffMember", guild=guild)

        ok, msg = await role_service.deny_role_request(
            guild=guild, channel_id=88888888, staff_member=staff
        )

        assert ok is False
        assert msg == "No hay solicitud pendiente asociada a este canal."

    @pytest.mark.asyncio
    async def test_deny_request_already_resolved(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Si la solicitud ya fue denegada o aprobada, retorna error."""
        guild = create_mock_guild(clean_settings)
        staff = create_mock_member(900001, name="StaffMember", guild=guild)

        repo = RoleRequestRepository(db_session)
        req = await repo.create_request(
            user_id=500001,
            nombre_lol="Gumayusi",
            riot_tag="T1",
            equipo="Vanguard Gaming",
            canal_id=888001,
        )
        await repo.update_status(req.id, RoleRequestStatus.DENIED, staff_id=900001)

        ok, msg = await role_service.deny_role_request(
            guild=guild, channel_id=888001, staff_member=staff
        )

        assert ok is False
        assert msg == "No hay solicitud pendiente asociada a este canal."

    @pytest.mark.asyncio
    async def test_deny_request_success(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """Deniega la solicitud pendiente actualizando el estado a DENIED con staff_id en BD."""
        guild = create_mock_guild(clean_settings)
        staff = create_mock_member(900002, name="StaffRefuser", guild=guild)

        repo = RoleRequestRepository(db_session)
        req = await repo.create_request(
            user_id=500002,
            nombre_lol="Zeus",
            riot_tag="HLE",
            equipo="Vanguard Gaming",
            canal_id=888002,
        )

        ok, msg = await role_service.deny_role_request(
            guild=guild, channel_id=888002, staff_member=staff
        )

        assert ok is True
        assert msg == "Solicitud de rol denegada."

        # Verificar en BD
        db_session.expire_all()
        updated_req = await repo.get_by_channel_id(888002)
        assert updated_req is not None
        assert updated_req.id == req.id
        assert updated_req.estado == RoleRequestStatus.DENIED
        assert updated_req.staff_id == 900002


# ===========================================================================
# 7. Pruebas de Integración y Ciclo de Vida Completo con PGlite
# ===========================================================================
class TestRoleServiceLifecycleIntegration:
    """Pruebas de integración de ciclo de vida completo sobre base de datos real PGlite."""

    @pytest.mark.asyncio
    async def test_full_ticket_approval_lifecycle(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """
        Ciclo completo de ticket:
        1. create_role_request_ticket -> Crea canal, guarda en BD (PENDING).
        2. confirm_role_request -> Aprueba rol, actualiza a APPROVED y registra staff_id.
        3. Repo verifica persistencia y marcas de tiempo actualizadas.
        """
        team_role = create_mock_role(999111, "Storm Legion")
        guild = create_mock_guild(clean_settings, roles=[team_role])
        member = create_mock_member(600001, name="ChovyT", display_name="Chovy_MID", guild=guild)
        staff = create_mock_member(900001, name="ChiefAdmin", guild=guild)
        guild._members_map[600001] = member

        # 1. Crear ticket
        ok_create, _, channel = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Chovy",
            riot_tag="GEN",
            equipo="Storm Legion",
        )
        assert ok_create is True
        assert channel is not None

        repo = RoleRequestRepository(db_session)
        created_req = await repo.get_by_channel_id(channel.id)
        assert created_req is not None
        assert created_req.estado == RoleRequestStatus.PENDING
        assert created_req.staff_id is None

        # 2. Confirmar solicitud
        ok_confirm, confirm_msg = await role_service.confirm_role_request(
            guild=guild, channel_id=channel.id, staff_member=staff
        )
        assert ok_confirm is True
        assert "Storm Legion confirmado para Chovy_MID." in confirm_msg

        # 3. Validar estado final
        db_session.expire_all()
        approved_req = await repo.get_by_channel_id(channel.id)
        assert approved_req is not None
        assert approved_req.estado == RoleRequestStatus.APPROVED
        assert approved_req.staff_id == 900001
        assert approved_req.updated_at >= created_req.created_at

    @pytest.mark.asyncio
    async def test_full_ticket_denial_lifecycle(
        self, role_service: RoleService, clean_settings: Settings, db_session: AsyncSession
    ):
        """
        Ciclo completo de ticket denegado:
        1. create_role_request_ticket -> PENDING.
        2. deny_role_request -> DENIED.
        3. Usuario queda habilitado para enviar una nueva solicitud posteriormente.
        """
        guild = create_mock_guild(clean_settings)
        member = create_mock_member(600002, name="Kanavi", guild=guild)
        staff = create_mock_member(900001, name="ChiefAdmin", guild=guild)
        guild._members_map[600002] = member

        # 1. Crear primer ticket
        ok_create1, _, chan1 = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Kanavi",
            riot_tag="JDG",
            equipo="Titan Gaming",
        )
        assert ok_create1 is True
        assert chan1 is not None

        # Intento de segundo ticket bloqueado por duplicado
        ok_dup, dup_msg, _ = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Kanavi",
            riot_tag="JDG",
            equipo="Titan Gaming",
        )
        assert ok_dup is False
        assert "Ya tienes una solicitud de rol pendiente." in dup_msg

        # 2. Denegar primer ticket
        ok_deny, deny_msg = await role_service.deny_role_request(
            guild=guild, channel_id=chan1.id, staff_member=staff
        )
        assert ok_deny is True
        assert deny_msg == "Solicitud de rol denegada."

        # 3. Usuario vuelve a poder solicitar rol tras la denegación
        ok_create2, _, chan2 = await role_service.create_role_request_ticket(
            guild=guild,
            member=member,
            nombre_lol="Kanavi",
            riot_tag="TES",
            equipo="Titan Gaming",
        )
        assert ok_create2 is True
        assert chan2 is not None
        assert chan2.id != chan1.id
