"""
Pruebas unitarias y de persistencia para el modelo declarativo RoleRequest
y el enum RoleRequestStatus en src/liga_bot/models/.
"""

from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from alembic import command
from liga_bot.models.enums import RoleRequestStatus
from liga_bot.models.role_request import RoleRequest


@pytest_asyncio.fixture
async def session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Proporciona una AsyncSession aislada por test con rollback automático."""
    async with migrated_db.connect() as conn:
        trans = await conn.begin()
        async_session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            await trans.rollback()


class TestRoleRequestStatusEnum:
    """Pruebas para el enum RoleRequestStatus."""

    def test_enum_members_and_values(self):
        """Verifica los miembros canónicos del enum y sus valores de cadena."""
        assert RoleRequestStatus.PENDING.name == "PENDING"
        assert RoleRequestStatus.PENDING.value == "PENDING"

        assert RoleRequestStatus.APPROVED.name == "APPROVED"
        assert RoleRequestStatus.APPROVED.value == "APPROVED"

        assert RoleRequestStatus.DENIED.name == "DENIED"
        assert RoleRequestStatus.DENIED.value == "DENIED"

    def test_enum_str_comparison(self):
        """Verifica que los miembros se comparen directamente como strings."""
        assert RoleRequestStatus.PENDING == "PENDING"
        assert RoleRequestStatus.APPROVED == "APPROVED"
        assert RoleRequestStatus.DENIED == "DENIED"

    def test_enum_invalid_value_raises(self):
        """Verifica que valores desconocidos lancen ValueError."""
        with pytest.raises(ValueError):
            RoleRequestStatus("INVALID_STATUS")

        with pytest.raises(ValueError):
            RoleRequestStatus("REJECTED")

    def test_enum_count(self):
        """Verifica que el enum contenga exactamente 3 estados."""
        assert len(RoleRequestStatus) == 3
        expected = {RoleRequestStatus.PENDING, RoleRequestStatus.APPROVED, RoleRequestStatus.DENIED}
        assert set(RoleRequestStatus) == expected


class TestRoleRequestModelUnit:
    """Pruebas unitarias en memoria para la entidad RoleRequest."""

    def test_role_request_initialization_explicit(self):
        """Verifica la instanciación de RoleRequest con todos los campos explícitos."""
        req = RoleRequest(
            user_id=1547725310508667010,
            nombre_lol="Faker",
            riot_tag="KR1",
            equipo="Planar Shock Pingus",
            canal_id=1550476204014969022,
            estado=RoleRequestStatus.PENDING,
            staff_id=1548795781174009977,
        )
        assert req.user_id == 1547725310508667010
        assert req.nombre_lol == "Faker"
        assert req.riot_tag == "KR1"
        assert req.equipo == "Planar Shock Pingus"
        assert req.canal_id == 1550476204014969022
        assert req.estado == RoleRequestStatus.PENDING
        assert req.staff_id == 1548795781174009977

    def test_role_request_field_defaults(self):
        """Verifica los valores por defecto al instanciar RoleRequest."""
        req = RoleRequest(
            user_id=112233445566778899,
            nombre_lol="Caps",
            riot_tag="EUW",
            equipo="Fnix Esports",
        )
        assert req.user_id == 112233445566778899
        assert req.nombre_lol == "Caps"
        assert req.riot_tag == "EUW"
        assert req.equipo == "Fnix Esports"
        assert req.estado == RoleRequestStatus.PENDING
        assert req.canal_id is None
        assert req.staff_id is None

    def test_role_request_repr_async_safe(self):
        """Verifica que el __repr__ sea seguro en contextos asíncronos y legible."""
        req = RoleRequest(
            user_id=112233445566778899,
            nombre_lol="Elyoya",
            riot_tag="MAD",
            equipo="Tilt Masters",
        )
        rep = repr(req)
        assert "RoleRequest" in rep
        assert "user_id=112233445566778899" in rep
        assert "nombre_lol='Elyoya'" in rep
        assert "equipo='Tilt Masters'" in rep

    def test_role_request_status_transitions(self):
        """Verifica las transiciones de estado en la instancia."""
        req = RoleRequest(
            user_id=112233445566778899,
            nombre_lol="Jojopyun",
            riot_tag="NA1",
            equipo="Troncos",
        )
        assert req.estado == RoleRequestStatus.PENDING

        # Transición a APPROVED
        req.estado = RoleRequestStatus.APPROVED
        req.staff_id = 998877665544332211
        assert req.estado == RoleRequestStatus.APPROVED
        assert req.staff_id == 998877665544332211

        # Transición a DENIED
        req.estado = RoleRequestStatus.DENIED
        assert req.estado == RoleRequestStatus.DENIED


class TestRoleRequestModelDatabaseRoundtrip:
    """Pruebas de persistencia y consultas relacionales sobre base de datos migrada."""

    @pytest.mark.asyncio
    async def test_db_persist_and_roundtrip(self, session: AsyncSession):
        """Verifica la persistencia en base de datos, autogeneración de ID y marcas temporales."""
        req = RoleRequest(
            user_id=1547725310508667010,
            nombre_lol="Rekkles",
            riot_tag="EUW",
            equipo="Akelarre",
        )
        session.add(req)
        await session.flush()

        # Validar generación de ID y campos automáticos
        assert req.id is not None
        assert req.user_id == 1547725310508667010
        assert req.nombre_lol == "Rekkles"
        assert req.riot_tag == "EUW"
        assert req.equipo == "Akelarre"
        assert req.estado == RoleRequestStatus.PENDING
        assert req.canal_id is None
        assert req.staff_id is None
        assert isinstance(req.created_at, datetime)
        assert isinstance(req.updated_at, datetime)

        # Recuperar desde la base de datos por ID
        retrieved = await session.get(RoleRequest, req.id)
        assert retrieved is not None
        assert retrieved.id == req.id
        assert retrieved.nombre_lol == "Rekkles"

    @pytest.mark.asyncio
    async def test_db_update_status_and_timestamps(self, session: AsyncSession):
        """Verifica la actualización de estado y asignación de staff_id en BD."""
        req = RoleRequest(
            user_id=1547725310508667011,
            nombre_lol="Chovy",
            riot_tag="KR2",
            equipo="Draconis Aeterni",
            canal_id=1550476204014969033,
        )
        session.add(req)
        await session.flush()

        req_id = req.id
        req.estado = RoleRequestStatus.APPROVED
        req.staff_id = 1548795781174009977
        await session.flush()
        await session.refresh(req)

        assert req.id == req_id
        assert req.estado == RoleRequestStatus.APPROVED
        assert req.staff_id == 1548795781174009977
        assert req.canal_id == 1550476204014969033
        assert req.created_at is not None
        assert req.updated_at is not None

    @pytest.mark.asyncio
    async def test_db_nullability_constraints(self, session: AsyncSession):
        """Verifica que la omisión de campos requeridos lance IntegrityError."""
        # 1. user_id nulo
        bad_req1 = RoleRequest(
            user_id=None,  # type: ignore
            nombre_lol="Player",
            riot_tag="TAG",
            equipo="Lotus",
        )
        session.add(bad_req1)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

        # 2. nombre_lol nulo
        bad_req2 = RoleRequest(
            user_id=123456789,
            nombre_lol=None,  # type: ignore
            riot_tag="TAG",
            equipo="Lotus",
        )
        session.add(bad_req2)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

        # 3. equipo nulo
        bad_req3 = RoleRequest(
            user_id=123456789,
            nombre_lol="Player",
            riot_tag="TAG",
            equipo=None,  # type: ignore
        )
        session.add(bad_req3)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_db_discord_snowflake_bigint_safety(self, session: AsyncSession):
        """Valida que snowflakes de 64 bits se persistan sin desbordar BigInteger."""
        snowflake_user = 1547725310508667010
        snowflake_canal = 1550476204014969022
        snowflake_staff = 1548795781174009977

        # Superan el rango máximo de 32-bit con signo (2.147.483.647)
        assert snowflake_user > 2_147_483_647
        assert snowflake_canal > 2_147_483_647
        assert snowflake_staff > 2_147_483_647

        req = RoleRequest(
            user_id=snowflake_user,
            nombre_lol="SnowflakeUser",
            riot_tag="TAG",
            equipo="Ramitas",
            canal_id=snowflake_canal,
            staff_id=snowflake_staff,
        )
        session.add(req)
        await session.flush()

        stmt = select(RoleRequest).where(RoleRequest.id == req.id)
        result = await session.execute(stmt)
        persisted = result.scalar_one()

        assert persisted.user_id == snowflake_user
        assert persisted.canal_id == snowflake_canal
        assert persisted.staff_id == snowflake_staff

    @pytest.mark.asyncio
    async def test_db_nullable_canal_id_multiple_nulls(self, session: AsyncSession):
        """Verifica que se permitan múltiples solicitudes con canal_id=None."""
        req1 = RoleRequest(
            user_id=100000000000000001,
            nombre_lol="UserOne",
            riot_tag="TAG1",
            equipo="Palitos",
            canal_id=None,
        )
        req2 = RoleRequest(
            user_id=100000000000000002,
            nombre_lol="UserTwo",
            riot_tag="TAG2",
            equipo="Palitos",
            canal_id=None,
        )
        session.add_all([req1, req2])
        await session.flush()

        assert req1.id is not None
        assert req2.id is not None
        assert req1.id != req2.id
        assert req1.canal_id is None
        assert req2.canal_id is None

    @pytest.mark.asyncio
    async def test_db_multiple_requests_per_user(self, session: AsyncSession):
        """Verifica que un usuario pueda tener un historial de solicitudes (auditoría)."""
        uid = 100000000000000003
        req_denied = RoleRequest(
            user_id=uid,
            nombre_lol="Historico",
            riot_tag="TAG",
            equipo="Rift Pups",
            estado=RoleRequestStatus.DENIED,
            staff_id=1548795781174009977,
        )
        req_new = RoleRequest(
            user_id=uid,
            nombre_lol="Historico",
            riot_tag="TAG",
            equipo="Pingus Academy",
            estado=RoleRequestStatus.PENDING,
        )
        session.add_all([req_denied, req_new])
        await session.flush()

        stmt = select(RoleRequest).where(RoleRequest.user_id == uid).order_by(RoleRequest.id)
        result = await session.execute(stmt)
        requests = result.scalars().all()

        assert len(requests) == 2
        assert requests[0].estado == RoleRequestStatus.DENIED
        assert requests[1].estado == RoleRequestStatus.PENDING

    @pytest.mark.asyncio
    async def test_db_query_filters_by_indexes(self, session: AsyncSession):
        """Verifica consultas selectivas sobre columnas indexadas (user_id, canal_id, estado)."""
        test_channel = 999111222333444555
        req = RoleRequest(
            user_id=777888999000111222,
            nombre_lol="IndexedPlayer",
            riot_tag="IX1",
            equipo="Nova+",
            canal_id=test_channel,
            estado=RoleRequestStatus.PENDING,
        )
        session.add(req)
        await session.flush()

        # Filtrar por canal_id
        stmt = select(RoleRequest).where(RoleRequest.canal_id == test_channel)
        res = await session.execute(stmt)
        found = res.scalar_one_or_none()
        assert found is not None
        assert found.id == req.id

        # Filtrar por user_id y estado PENDING
        stmt_pending = select(RoleRequest).where(
            RoleRequest.user_id == 777888999000111222,
            RoleRequest.estado == RoleRequestStatus.PENDING,
        )
        res_pending = await session.execute(stmt_pending)
        found_pending = res_pending.scalar_one_or_none()
        assert found_pending is not None
        assert found_pending.nombre_lol == "IndexedPlayer"

    @pytest.mark.asyncio
    async def test_alembic_002_downgrade_and_reupgrade(self, migrated_db: AsyncEngine):
        """Verifica la reversibilidad limpia de la migración 002 (downgrade y re-upgrade)."""
        cfg = Config("alembic.ini")
        async with migrated_db.connect() as conn:

            def do_migration_cycle(sync_conn):
                cfg.attributes["connection"] = sync_conn
                # Revertir migración 002 a 001
                command.downgrade(cfg, "001")
                tables_after_downgrade = inspect(sync_conn).get_table_names()
                assert "role_requests" not in tables_after_downgrade

                # Re-aplicar migración 002 hasta head
                command.upgrade(cfg, "head")
                tables_after_upgrade = inspect(sync_conn).get_table_names()
                assert "role_requests" in tables_after_upgrade

            await conn.run_sync(do_migration_cycle)
            await conn.commit()
