"""
Pruebas exhaustivas (unitarias y de integración) para RoleRequestRepository
en src/liga_bot/repositories/role_request_repo.py.
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.models.enums import RoleRequestStatus
from liga_bot.models.role_request import RoleRequest
from liga_bot.repositories.role_request_repo import RoleRequestRepository


# ---------------------------------------------------------------------------
# Fixtures de persistencia para pruebas de integración con PGlite
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provee una sesión limpia con truncado total de role_requests tras cada test."""
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()
        await session.execute(text("TRUNCATE TABLE role_requests CASCADE;"))
        await session.commit()


@pytest.fixture
def role_request_repo(db_session: AsyncSession) -> RoleRequestRepository:
    """Instancia de RoleRequestRepository conectada a la sesión de BD de prueba."""
    return RoleRequestRepository(db_session)


# ===========================================================================
# 1. Pruebas Unitarias con Mock AsyncSession
# ===========================================================================
class TestRoleRequestRepositoryUnit:
    """Pruebas unitarias de aislamiento con sesión simulada (AsyncMock)."""

    def test_init_calls_super_with_session_and_model_cls(self):
        """Verifica que el constructor invoque a super con (session, RoleRequest)."""
        mock_session = MagicMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        assert repo.session is mock_session
        assert repo.model_cls is RoleRequest

    @pytest.mark.asyncio
    async def test_create_request_unit(self):
        """Verifica que create_request instancie RoleRequest y ejecute add/flush/refresh."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        req = await repo.create_request(
            user_id=123456789,
            nombre_lol="Faker",
            riot_tag="KR1",
            equipo="Planar Shock Pingus",
            canal_id=987654321,
        )

        assert isinstance(req, RoleRequest)
        assert req.user_id == 123456789
        assert req.nombre_lol == "Faker"
        assert req.riot_tag == "KR1"
        assert req.equipo == "Planar Shock Pingus"
        assert req.canal_id == 987654321
        assert req.estado == RoleRequestStatus.PENDING

        mock_session.add.assert_called_once_with(req)
        mock_session.flush.assert_awaited_once()
        mock_session.refresh.assert_awaited_once_with(req)

    @pytest.mark.asyncio
    async def test_create_request_with_none_canal_id_unit(self):
        """Verifica que create_request soporte canal_id opcional (None)."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        req = await repo.create_request(
            user_id=111,
            nombre_lol="Chovy",
            riot_tag="KR2",
            equipo="Fnix Esports",
            canal_id=None,
        )

        assert req.canal_id is None
        mock_session.add.assert_called_once_with(req)
        mock_session.flush.assert_awaited_once()
        mock_session.refresh.assert_awaited_once_with(req)

    @pytest.mark.asyncio
    async def test_get_by_channel_id_found_unit(self):
        """Verifica que get_by_channel_id retorne la solicitud cuando existe."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        dummy_req = RoleRequest(
            id=1,
            user_id=123,
            nombre_lol="Caps",
            riot_tag="EUW",
            equipo="Team One",
            canal_id=555,
            estado=RoleRequestStatus.PENDING,
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = dummy_req
        mock_session.execute.return_value = mock_result

        found = await repo.get_by_channel_id(555)
        assert found is dummy_req
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_by_channel_id_not_found_unit(self):
        """Verifica que get_by_channel_id retorne None cuando no hay coincidencias."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = mock_result

        found = await repo.get_by_channel_id(999)
        assert found is None
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_active_by_user_found_unit(self):
        """Verifica que get_active_by_user retorne la solicitud pendiente del usuario."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        dummy_req = RoleRequest(
            id=2,
            user_id=456,
            nombre_lol="Ruler",
            riot_tag="KR3",
            equipo="Team Two",
            estado=RoleRequestStatus.PENDING,
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = dummy_req
        mock_session.execute.return_value = mock_result

        found = await repo.get_active_by_user(456)
        assert found is dummy_req
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_active_by_user_not_found_unit(self):
        """Verifica que get_active_by_user retorne None si no hay solicitud activa."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = mock_result

        found = await repo.get_active_by_user(999)
        assert found is None
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_status_found_with_staff_id_unit(self):
        """Verifica que update_status actualice estado y staff_id cuando la solicitud existe."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        dummy_req = RoleRequest(
            id=10,
            user_id=789,
            nombre_lol="ShowMaker",
            riot_tag="KR4",
            equipo="Team DK",
            estado=RoleRequestStatus.PENDING,
            staff_id=None,
        )
        mock_session.get.return_value = dummy_req

        result = await repo.update_status(10, RoleRequestStatus.APPROVED, staff_id=112233)
        assert result is dummy_req
        assert dummy_req.estado == RoleRequestStatus.APPROVED
        assert dummy_req.staff_id == 112233
        mock_session.flush.assert_awaited_once()
        mock_session.refresh.assert_awaited_once_with(dummy_req)

    @pytest.mark.asyncio
    async def test_update_status_found_without_staff_id_unit(self):
        """Verifica que update_status mantenga staff_id intacto si staff_id es None."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        dummy_req = RoleRequest(
            id=11,
            user_id=789,
            nombre_lol="ShowMaker",
            riot_tag="KR4",
            equipo="Team DK",
            estado=RoleRequestStatus.PENDING,
            staff_id=None,
        )
        mock_session.get.return_value = dummy_req

        result = await repo.update_status(11, RoleRequestStatus.DENIED)
        assert result is dummy_req
        assert dummy_req.estado == RoleRequestStatus.DENIED
        assert dummy_req.staff_id is None
        mock_session.flush.assert_awaited_once()
        mock_session.refresh.assert_awaited_once_with(dummy_req)

    @pytest.mark.asyncio
    async def test_update_status_not_found_unit(self):
        """Verifica que update_status retorne None si no existe la entidad sin llamar a flush."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        mock_session.get.return_value = None

        result = await repo.update_status(999, RoleRequestStatus.APPROVED)
        assert result is None
        mock_session.flush.assert_not_called()
        mock_session.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_status_string_conversion_unit(self):
        """Verifica que update_status acepte cadenas compatibles con RoleRequestStatus."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = RoleRequestRepository(mock_session)

        dummy_req = RoleRequest(
            id=12,
            user_id=789,
            nombre_lol="Canyon",
            riot_tag="KR5",
            equipo="Team GEN",
            estado=RoleRequestStatus.PENDING,
        )
        mock_session.get.return_value = dummy_req

        result = await repo.update_status(12, "APPROVED", staff_id=555)  # type: ignore[arg-type]
        assert result is dummy_req
        assert dummy_req.estado == RoleRequestStatus.APPROVED
        assert dummy_req.staff_id == 555


# ===========================================================================
# 2. Pruebas de Integración con Base de Datos PGlite
# ===========================================================================
class TestRoleRequestRepositoryIntegration:
    """Pruebas de integración sobre motor asíncrono PGlite con migraciones aplicadas."""

    @pytest.mark.asyncio
    async def test_create_request_lifecycle(self, role_request_repo: RoleRequestRepository):
        """Verifica creación y persistencia real en BD con generación de ID y marcas temporales."""
        req = await role_request_repo.create_request(
            user_id=1547725310508667010,
            nombre_lol="Faker",
            riot_tag="T1WIN",
            equipo="Planar Shock Pingus",
            canal_id=1550476204014969022,
        )

        assert req.id is not None
        assert isinstance(req.id, int)
        assert req.id > 0
        assert req.user_id == 1547725310508667010
        assert req.nombre_lol == "Faker"
        assert req.riot_tag == "T1WIN"
        assert req.equipo == "Planar Shock Pingus"
        assert req.canal_id == 1550476204014969022
        assert req.estado == RoleRequestStatus.PENDING
        assert req.staff_id is None
        assert req.created_at is not None
        assert req.updated_at is not None

    @pytest.mark.asyncio
    async def test_create_request_with_none_canal_id(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica creación de solicitud con canal_id nulo."""
        req = await role_request_repo.create_request(
            user_id=200000000000000001,
            nombre_lol="Deft",
            riot_tag="DRX",
            equipo="Libre",
            canal_id=None,
        )

        assert req.id is not None
        assert req.canal_id is None
        assert req.estado == RoleRequestStatus.PENDING

    @pytest.mark.asyncio
    async def test_create_request_with_large_snowflake_ids(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica que los IDs de 64 bits de Discord se almacenen y recuperen sin truncamiento."""
        large_user_id = 9223372036854775807  # Max signed 64-bit int
        large_canal_id = 9223372036854775806

        req = await role_request_repo.create_request(
            user_id=large_user_id,
            nombre_lol="SnowflakeKing",
            riot_tag="TAG64",
            equipo="Mega Team",
            canal_id=large_canal_id,
        )
        assert req.id is not None
        assert req.id > 0

        fetched = await role_request_repo.get_by_channel_id(large_canal_id)
        assert fetched is not None
        assert fetched.user_id == large_user_id
        assert fetched.canal_id == large_canal_id

    @pytest.mark.asyncio
    async def test_get_by_channel_id_match_and_none(self, role_request_repo: RoleRequestRepository):
        """Verifica búsqueda por ID de canal de ticket existente e inexistente."""
        canal_id = 112233445566778899
        created = await role_request_repo.create_request(
            user_id=300000000000000001,
            nombre_lol="BeryL",
            riot_tag="GEN",
            equipo="Crown Esports",
            canal_id=canal_id,
        )

        fetched = await role_request_repo.get_by_channel_id(canal_id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.user_id == created.user_id
        assert fetched.nombre_lol == "BeryL"

        none_result = await role_request_repo.get_by_channel_id(999888777666555)
        assert none_result is None

    @pytest.mark.asyncio
    async def test_get_active_by_user_lifecycle(self, role_request_repo: RoleRequestRepository):
        """
        Verifica que get_active_by_user únicamente devuelva la solicitud si está en estado PENDING,
        ignorando solicitudes previas en estado APPROVED o DENIED.
        """
        user_id = 400000000000000001

        # 1. No existe solicitud previa
        assert await role_request_repo.get_active_by_user(user_id) is None

        # 2. Se crea una solicitud -> debe retornar como activa
        req = await role_request_repo.create_request(
            user_id=user_id,
            nombre_lol="Keria",
            riot_tag="KR",
            equipo="Planar Shock Pingus",
            canal_id=500000000000000001,
        )
        active = await role_request_repo.get_active_by_user(user_id)
        assert active is not None
        assert active.id == req.id
        assert active.estado == RoleRequestStatus.PENDING

        # 3. Se aprueba la solicitud -> get_active_by_user debe retornar None
        await role_request_repo.update_status(
            req.id, RoleRequestStatus.APPROVED, staff_id=900000000000000001
        )
        assert await role_request_repo.get_active_by_user(user_id) is None

        # 4. Se crea una nueva solicitud tras la anterior -> debe retornar la nueva como activa
        new_req = await role_request_repo.create_request(
            user_id=user_id,
            nombre_lol="Keria",
            riot_tag="KR",
            equipo="Fnix Esports",
            canal_id=500000000000000002,
        )
        active_new = await role_request_repo.get_active_by_user(user_id)
        assert active_new is not None
        assert active_new.id == new_req.id
        assert active_new.estado == RoleRequestStatus.PENDING

        # 5. Se deniega la nueva solicitud -> get_active_by_user debe retornar None
        await role_request_repo.update_status(
            new_req.id, RoleRequestStatus.DENIED, staff_id=900000000000000001
        )
        assert await role_request_repo.get_active_by_user(user_id) is None

    @pytest.mark.asyncio
    async def test_get_active_by_user_returns_latest_pending(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica que si existen múltiples registros PENDING, retorne el más reciente."""
        user_id = 450000000000000001

        req1 = await role_request_repo.create_request(
            user_id=user_id,
            nombre_lol="OldTag",
            riot_tag="1",
            equipo="Team Old",
            canal_id=600000000000000001,
        )
        req2 = await role_request_repo.create_request(
            user_id=user_id,
            nombre_lol="NewTag",
            riot_tag="2",
            equipo="Team New",
            canal_id=600000000000000002,
        )

        active = await role_request_repo.get_active_by_user(user_id)
        assert active is not None
        assert active.id == req2.id
        assert active.id > req1.id

    @pytest.mark.asyncio
    async def test_update_status_approved_with_staff_id(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica actualización a APPROVED registrando el ID del staff revisor."""
        req = await role_request_repo.create_request(
            user_id=500000000000000001,
            nombre_lol="Scout",
            riot_tag="LNG",
            equipo="Team Test",
            canal_id=700000000000000001,
        )
        staff_id = 999111222333444555

        updated = await role_request_repo.update_status(
            req.id, RoleRequestStatus.APPROVED, staff_id=staff_id
        )
        assert updated is not None
        assert updated.estado == RoleRequestStatus.APPROVED
        assert updated.staff_id == staff_id

        # Verificar persistencia recuperando directamente de la BD
        persisted = await role_request_repo.get_by_id(req.id)
        assert persisted is not None
        assert persisted.estado == RoleRequestStatus.APPROVED
        assert persisted.staff_id == staff_id

    @pytest.mark.asyncio
    async def test_update_status_approved_without_staff_id(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica actualización a APPROVED sin especificar staff_id (mantiene None)."""
        req = await role_request_repo.create_request(
            user_id=500000000000000002,
            nombre_lol="Gala",
            riot_tag="LNG",
            equipo="Team Test",
            canal_id=700000000000000002,
        )

        updated = await role_request_repo.update_status(req.id, RoleRequestStatus.APPROVED)
        assert updated is not None
        assert updated.estado == RoleRequestStatus.APPROVED
        assert updated.staff_id is None

        persisted = await role_request_repo.get_by_id(req.id)
        assert persisted is not None
        assert persisted.estado == RoleRequestStatus.APPROVED
        assert persisted.staff_id is None

    @pytest.mark.asyncio
    async def test_update_status_denied_with_staff_id(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica actualización a DENIED registrando el ID del staff revisor."""
        req = await role_request_repo.create_request(
            user_id=500000000000000003,
            nombre_lol="Tarzan",
            riot_tag="WBG",
            equipo="Team Test",
            canal_id=700000000000000003,
        )
        staff_id = 888111222333444555

        updated = await role_request_repo.update_status(
            req.id, RoleRequestStatus.DENIED, staff_id=staff_id
        )
        assert updated is not None
        assert updated.estado == RoleRequestStatus.DENIED
        assert updated.staff_id == staff_id

        persisted = await role_request_repo.get_by_id(req.id)
        assert persisted is not None
        assert persisted.estado == RoleRequestStatus.DENIED
        assert persisted.staff_id == staff_id

    @pytest.mark.asyncio
    async def test_update_status_denied_without_staff_id(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica actualización a DENIED sin especificar staff_id."""
        req = await role_request_repo.create_request(
            user_id=500000000000000004,
            nombre_lol="Crisp",
            riot_tag="WBG",
            equipo="Team Test",
            canal_id=700000000000000004,
        )

        updated = await role_request_repo.update_status(req.id, RoleRequestStatus.DENIED)
        assert updated is not None
        assert updated.estado == RoleRequestStatus.DENIED
        assert updated.staff_id is None

        persisted = await role_request_repo.get_by_id(req.id)
        assert persisted is not None
        assert persisted.estado == RoleRequestStatus.DENIED
        assert persisted.staff_id is None

    @pytest.mark.asyncio
    async def test_update_status_nonexistent_returns_none(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica que update_status sobre un ID inexistente devuelva None."""
        result = await role_request_repo.update_status(999999999, RoleRequestStatus.APPROVED)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_status_string_coercion_in_db(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica que pasar el estado como string ('APPROVED') sea coercionado y persistido."""
        req = await role_request_repo.create_request(
            user_id=500000000000000005,
            nombre_lol="TheShy",
            riot_tag="IG",
            equipo="Team Test",
            canal_id=700000000000000005,
        )

        updated = await role_request_repo.update_status(
            req.id,
            "APPROVED",  # type: ignore[arg-type]
        )
        assert updated is not None
        assert updated.estado == RoleRequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_inherited_base_repo_methods(self, role_request_repo: RoleRequestRepository):
        """Verifica métodos heredados de BaseRepository (get_by_id, list_all, count, delete)."""
        # Inicialmente conteo 0
        assert await role_request_repo.count() == 0
        assert list(await role_request_repo.list_all()) == []

        # Crear 3 solicitudes
        r1 = await role_request_repo.create_request(101, "P1", "T1", "Eq1", 1001)
        r2 = await role_request_repo.create_request(102, "P2", "T2", "Eq2", 1002)
        r3 = await role_request_repo.create_request(103, "P3", "T3", "Eq3", 1003)

        assert await role_request_repo.count() == 3
        all_reqs = await role_request_repo.list_all()
        assert len(all_reqs) == 3
        ids = {r.id for r in all_reqs}
        assert ids == {r1.id, r2.id, r3.id}

        # get_by_id
        fetched_r1 = await role_request_repo.get_by_id(r1.id)
        assert fetched_r1 is not None
        assert fetched_r1.id == r1.id
        assert await role_request_repo.get_by_id(999999) is None

        # delete por instancia
        await role_request_repo.delete(r2)
        assert await role_request_repo.count() == 2
        assert await role_request_repo.get_by_id(r2.id) is None

        # delete_by_id existente e inexistente
        deleted = await role_request_repo.delete_by_id(r3.id)
        assert deleted is True
        assert await role_request_repo.count() == 1

        deleted_again = await role_request_repo.delete_by_id(r3.id)
        assert deleted_again is False

    @pytest.mark.asyncio
    async def test_inherited_base_repo_create_and_update(
        self, role_request_repo: RoleRequestRepository
    ):
        """Verifica los métodos genéricos create y update heredados de BaseRepository."""
        created = await role_request_repo.create(
            user_id=201,
            nombre_lol="Rookie",
            riot_tag="V5",
            equipo="Victory Five",
            canal_id=2001,
        )
        assert created.id is not None
        assert created.user_id == 201

        updated = await role_request_repo.update(created, equipo="Ninjas in Pyjamas")
        assert updated.equipo == "Ninjas in Pyjamas"

        persisted = await role_request_repo.get_by_id(created.id)
        assert persisted is not None
        assert persisted.equipo == "Ninjas in Pyjamas"
