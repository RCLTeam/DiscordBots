"""
Suite de pruebas de estrés adversarial y condiciones límite para Milestone 1:
- Identificadores Snowflake extremos (2^63 - 1, 0, desbordamiento, negativos).
- Cadenas Unicode complejas (emojis multibyte, caracteres RTL, scripts internacionales).
- Longitud extrema de campos (fronteras exactas 20/100, desbordamientos 21/101 y masivos).
- Inyecciones SQL y payloads XSS almacenados textualmente.
- Rechazo de bytes nulos y strings malformados.
- Transiciones y asignaciones de enum inválidas (sensibilidad a mayúsculas, valores espurios).
- Resistencia a NULL (fallback de inserción vs violación de integridad en UPDATE y SQL crudo).
- Resiliencia e idempotencia de migraciones Alembic bajo ciclos repetidos.
- Estabilidad en operaciones por lotes (bulk) e indexación.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, StatementError
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


class TestSnowflakeBoundaries:
    """Estrés de límites numéricos en identificadores tipo Discord Snowflake (BigInteger)."""

    @pytest.mark.asyncio
    async def test_max_signed_64bit_integer(self, session: AsyncSession):
        """Valida que el valor límite exacto (2^63 - 1 = 9223372036854775807) sea soportado."""
        max_int64 = (1 << 63) - 1  # 9223372036854775807

        req = RoleRequest(
            user_id=max_int64,
            nombre_lol="MaxSnowflake",
            riot_tag="MAX",
            equipo="Boundary Team",
            canal_id=max_int64,
            staff_id=max_int64,
        )
        session.add(req)
        await session.flush()

        stmt = select(RoleRequest).where(RoleRequest.id == req.id)
        result = await session.execute(stmt)
        persisted = result.scalar_one()

        assert persisted.user_id == max_int64
        assert persisted.canal_id == max_int64
        assert persisted.staff_id == max_int64

    @pytest.mark.asyncio
    async def test_snowflake_zero_and_negative(self, session: AsyncSession):
        """Verifica el comportamiento con snowflake 0 y valores negativos."""
        req = RoleRequest(
            user_id=0,
            nombre_lol="ZeroUser",
            riot_tag="ZERO",
            equipo="Zero Team",
            canal_id=0,
            staff_id=-1,
        )
        session.add(req)
        await session.flush()

        stmt = select(RoleRequest).where(RoleRequest.id == req.id)
        result = await session.execute(stmt)
        persisted = result.scalar_one()

        assert persisted.user_id == 0
        assert persisted.canal_id == 0
        assert persisted.staff_id == -1

    @pytest.mark.asyncio
    async def test_snowflake_overflow_signed_64bit_fails(self, session: AsyncSession):
        """Valida que valores mayores a 2^63 - 1 (desbordamiento BigInteger) fallen limpiamente."""
        overflow_val = 1 << 63  # 9223372036854775808 (excede BIGINT en PostgreSQL)

        req = RoleRequest(
            user_id=overflow_val,
            nombre_lol="OverflowUser",
            riot_tag="OVR",
            equipo="Overflow Team",
        )
        session.add(req)
        with pytest.raises((DataError, DBAPIError, OverflowError)):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_snowflake_uint64_max_fails(self, session: AsyncSession):
        """Valida que el máximo entero sin signo de 64 bits (2^64 - 1) sea rechazado."""
        uint64_max = (1 << 64) - 1

        req = RoleRequest(
            user_id=uint64_max,
            nombre_lol="Uint64Max",
            riot_tag="MAX",
            equipo="Uint64 Team",
        )
        session.add(req)
        with pytest.raises((DataError, DBAPIError, OverflowError)):
            await session.flush()
        await session.rollback()


class TestStringLengthsAndBoundaries:
    """Pruebas de límites de tamaño y desbordamiento en columnas VARCHAR."""

    @pytest.mark.asyncio
    async def test_nombre_lol_boundary_100_and_101(self, session: AsyncSession):
        """Verifica que nombre_lol acepte exactamente 100 caracteres y rechace 101."""
        # Exactamente 100 caracteres
        valid_name = "N" * 100
        req = RoleRequest(
            user_id=123456789,
            nombre_lol=valid_name,
            riot_tag="TAG",
            equipo="Team",
        )
        session.add(req)
        await session.flush()
        assert len(req.nombre_lol) == 100

        # 101 caracteres: debe ser rechazado por PostgreSQL
        invalid_name = "N" * 101
        req_too_long = RoleRequest(
            user_id=123456780,
            nombre_lol=invalid_name,
            riot_tag="TAG",
            equipo="Team",
        )
        session.add(req_too_long)
        with pytest.raises((DataError, DBAPIError)):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_riot_tag_boundary_20_and_21(self, session: AsyncSession):
        """Verifica que riot_tag acepte exactamente 20 caracteres y rechace 21."""
        # Exactamente 20 caracteres
        valid_tag = "T" * 20
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="Player",
            riot_tag=valid_tag,
            equipo="Team",
        )
        session.add(req)
        await session.flush()
        assert len(req.riot_tag) == 20

        # 21 caracteres: debe ser rechazado
        invalid_tag = "T" * 21
        req_too_long = RoleRequest(
            user_id=123456780,
            nombre_lol="Player",
            riot_tag=invalid_tag,
            equipo="Team",
        )
        session.add(req_too_long)
        with pytest.raises((DataError, DBAPIError)):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_equipo_boundary_100_and_101(self, session: AsyncSession):
        """Verifica que equipo acepte exactamente 100 caracteres y rechace 101."""
        # Exactamente 100 caracteres
        valid_equipo = "E" * 100
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="Player",
            riot_tag="TAG",
            equipo=valid_equipo,
        )
        session.add(req)
        await session.flush()
        assert len(req.equipo) == 100

        # 101 caracteres: debe ser rechazado
        invalid_equipo = "E" * 101
        req_too_long = RoleRequest(
            user_id=123456780,
            nombre_lol="Player",
            riot_tag="TAG",
            equipo=invalid_equipo,
        )
        session.add(req_too_long)
        with pytest.raises((DataError, DBAPIError)):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_massive_string_overflow(self, session: AsyncSession):
        """Valida que una cadena masiva de 10.000 caracteres sea rechazada limpiamente."""
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="A" * 10000,
            riot_tag="TAG",
            equipo="Team",
        )
        session.add(req)
        with pytest.raises((DataError, DBAPIError)):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_empty_strings_handled(self, session: AsyncSession):
        """Verifica la persistencia de cadenas vacías sin violar restricciones."""
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="",
            riot_tag="",
            equipo="",
        )
        session.add(req)
        await session.flush()
        assert req.nombre_lol == ""
        assert req.riot_tag == ""
        assert req.equipo == ""


class TestUnicodeAndSpecialPayloads:
    """Pruebas de codificación UTF-8, inyecciones de seguridad y caracteres especiales."""

    @pytest.mark.asyncio
    async def test_multibyte_unicode_emojis_and_length(self, session: AsyncSession):
        """Valida emojis multibyte. En PostgreSQL VARCHAR(N) cuenta codepoints unicode."""
        # 20 emojis en riot_tag (20 caracteres, 80 bytes UTF-8)
        emoji_tag = "👑" * 20
        # 50 emojis en nombre_lol (50 caracteres, 200 bytes UTF-8)
        emoji_name = "🧙‍♂️🎮⚡🔥🛡️" * 10

        req = RoleRequest(
            user_id=123456789,
            nombre_lol=emoji_name,
            riot_tag=emoji_tag,
            equipo="Planar Shock Pingus 🐧",
        )
        session.add(req)
        await session.flush()

        stmt = select(RoleRequest).where(RoleRequest.id == req.id)
        result = await session.execute(stmt)
        persisted = result.scalar_one()

        assert persisted.riot_tag == emoji_tag
        assert persisted.equipo == "Planar Shock Pingus 🐧"

    @pytest.mark.asyncio
    async def test_international_scripts_roundtrip(self, session: AsyncSession):
        """Valida alfabetos no latinos (Cirílico, Árabe RTL, CJK) y caracteres con diacríticos."""
        req = RoleRequest(
            user_id=987654321,
            nombre_lol="李相赫 (Faker) / Данил / محمد",
            riot_tag="KR#1",
            equipo="T1 🏆 / 팀 / فريق",
        )
        session.add(req)
        await session.flush()

        stmt = select(RoleRequest).where(RoleRequest.id == req.id)
        result = await session.execute(stmt)
        persisted = result.scalar_one()

        assert persisted.nombre_lol == "李相赫 (Faker) / Данил / محمد"
        assert persisted.equipo == "T1 🏆 / 팀 / فريق"

    @pytest.mark.asyncio
    async def test_sql_injection_payload_preserved_verbatim(self, session: AsyncSession):
        """Verifica que intentos de inyección SQL sean parametrizados de forma segura."""
        sqli_name = "'; DROP TABLE role_requests; --"
        sqli_tag = "' OR '1'='1"
        sqli_equipo = '" UNION SELECT * FROM users; --'

        req = RoleRequest(
            user_id=123456789,
            nombre_lol=sqli_name,
            riot_tag=sqli_tag,
            equipo=sqli_equipo,
        )
        session.add(req)
        await session.flush()

        # Verificar que la tabla siga existiendo y que los datos se almacenaron verbatim
        stmt = select(RoleRequest).where(RoleRequest.id == req.id)
        result = await session.execute(stmt)
        persisted = result.scalar_one()

        assert persisted.nombre_lol == sqli_name
        assert persisted.riot_tag == sqli_tag
        assert persisted.equipo == sqli_equipo

        # Comprobar que la tabla sigue accesible
        count_stmt = select(RoleRequest)
        count_res = await session.execute(count_stmt)
        assert len(count_res.scalars().all()) >= 1

    @pytest.mark.asyncio
    async def test_xss_and_multiline_strings(self, session: AsyncSession):
        """Valida que cadenas con tags HTML/XSS y caracteres multilínea se almacenen sin truncar."""
        xss_name = "<script>alert('pwned')</script>\nLine2\tTabbed"
        req = RoleRequest(
            user_id=123456789,
            nombre_lol=xss_name,
            riot_tag="<XSS>",
            equipo="Team\r\nNewline",
        )
        session.add(req)
        await session.flush()

        persisted = await session.get(RoleRequest, req.id)
        assert persisted is not None
        assert persisted.nombre_lol == xss_name
        assert persisted.equipo == "Team\r\nNewline"

    @pytest.mark.asyncio
    async def test_null_byte_in_string_rejected(self, session: AsyncSession):
        """Valida que bytes nulos (\\x00), incompatibles con texto en Postgres, sean rechazados."""
        bad_name = "Bad\x00Player"
        req = RoleRequest(
            user_id=123456789,
            nombre_lol=bad_name,
            riot_tag="TAG",
            equipo="Team",
        )
        session.add(req)
        with pytest.raises((DataError, DBAPIError, StatementError, ValueError)):
            await session.flush()
        await session.rollback()


class TestEnumIntegrityAndTransitions:
    """Pruebas adversariales de integridad de tipos ENUM y transiciones de estado."""

    @pytest.mark.asyncio
    async def test_invalid_enum_string_rejected_at_db_flush(self, session: AsyncSession):
        """Valida que una cadena que no pertenece a RoleRequestStatus sea rechazada en flush."""
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="Player",
            riot_tag="TAG",
            equipo="Team",
        )
        # Asignación de valor inválido saltándose el type hint
        req.estado = "INVENTED_STATUS"  # type: ignore
        session.add(req)
        with pytest.raises((DBAPIError, StatementError, DataError)):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_lowercase_enum_rejected(self, session: AsyncSession):
        """Valida que PostgreSQL distinga mayúsculas y rechace 'pending' en vez de 'PENDING'."""
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="Player",
            riot_tag="TAG",
            equipo="Team",
        )
        req.estado = "pending"  # type: ignore
        session.add(req)
        with pytest.raises((DBAPIError, StatementError, DataError)):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_enum_insert_none_falls_back_to_default(self, session: AsyncSession):
        """Valida que al crear un objeto con estado=None, SQLAlchemy aplique el default PENDING."""
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="Player",
            riot_tag="TAG",
            equipo="Team",
        )
        req.estado = None  # type: ignore
        session.add(req)
        await session.flush()
        assert req.estado == RoleRequestStatus.PENDING

    @pytest.mark.asyncio
    async def test_enum_update_to_null_violates_not_null_constraint(self, session: AsyncSession):
        """Valida que intentar actualizar estado a NULL en la base de datos lance IntegrityError."""
        req = RoleRequest(
            user_id=123456789,
            nombre_lol="Player",
            riot_tag="TAG",
            equipo="Team",
        )
        session.add(req)
        await session.flush()

        req.estado = None  # type: ignore
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_raw_sql_null_enum_violates_not_null_constraint(self, session: AsyncSession):
        """Valida que un INSERT crudo con estado NULL sea rechazado con NotNullViolation."""
        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text(
                    "INSERT INTO role_requests "
                    "(user_id, nombre_lol, riot_tag, equipo, estado, created_at, updated_at) "
                    "VALUES (123, 'Direct', 'SQL', 'Team', NULL, NOW(), NOW())"
                )
            )
        assert "violates not-null constraint" in str(exc_info.value).lower()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_raw_sql_invalid_enum_rejected(self, session: AsyncSession):
        """Valida con SQL directo que el ENUM 'rolerequeststatus' rechace valores arbitrarios."""
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text(
                    "INSERT INTO role_requests "
                    "(user_id, nombre_lol, riot_tag, equipo, estado, created_at, updated_at) "
                    "VALUES (123, 'Direct', 'SQL', 'Team', 'MALICIOUS_STATUS', NOW(), NOW())"
                )
            )
        assert "invalid input value for enum rolerequeststatus" in str(exc_info.value).lower()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_all_canonical_enum_transitions(self, session: AsyncSession):
        """Valida que todos los estados legítimos persistan y actualicen correctamente."""
        req = RoleRequest(
            user_id=555666777,
            nombre_lol="StateUser",
            riot_tag="TAG",
            equipo="Team",
        )
        session.add(req)
        await session.flush()
        assert req.estado == RoleRequestStatus.PENDING

        # PENDING -> APPROVED
        req.estado = RoleRequestStatus.APPROVED
        req.staff_id = 999
        await session.flush()
        await session.refresh(req)
        assert req.estado == RoleRequestStatus.APPROVED
        assert req.staff_id == 999

        # APPROVED -> DENIED (ej. rectificación de moderación)
        req.estado = RoleRequestStatus.DENIED
        await session.flush()
        await session.refresh(req)
        assert req.estado == RoleRequestStatus.DENIED


class TestAlembicMigrationStress:
    """Estrés de reversibilidad e idempotencia repetida de la migración Alembic 002."""

    @pytest.mark.asyncio
    async def test_repeated_downgrade_upgrade_cycles(self, async_engine: AsyncEngine):
        """Verifica que múltiples downgrades y upgrades no dejen locks ni tipos corruptos."""
        cfg = Config("alembic.ini")
        async with async_engine.connect() as conn:

            def do_repeated_cycles(sync_conn):
                cfg.attributes["connection"] = sync_conn
                # Ciclo 1: 002 -> 001 -> head
                command.downgrade(cfg, "001")
                command.upgrade(cfg, "head")

                # Ciclo 2: 002 -> 001 -> head
                command.downgrade(cfg, "001")
                command.upgrade(cfg, "head")

            await conn.run_sync(do_repeated_cycles)

        # Comprobar que tras los ciclos, la tabla role_requests existe y funciona
        async with async_engine.connect() as conn:
            result = await conn.execute(text("SELECT count(*) FROM role_requests"))
            count = result.scalar()
            assert count == 0


class TestBulkAndConcurrencyStress:
    """Estrés de inserción y consulta masiva sobre RoleRequest."""

    @pytest.mark.asyncio
    async def test_chunked_bulk_insert_and_indexed_filter(self, session: AsyncSession):
        """Inserta 50 registros y verifica la rapidez y exactitud del filtrado por índice."""
        requests = [
            RoleRequest(
                user_id=200000000000000000 + i,
                nombre_lol=f"BulkPlayer_{i}",
                riot_tag=f"T{i % 10}",
                equipo="Bulk Team Alpha" if i % 2 == 0 else "Bulk Team Beta",
                canal_id=300000000000000000 + i if i % 3 == 0 else None,
                estado=RoleRequestStatus.PENDING if i % 4 != 0 else RoleRequestStatus.APPROVED,
            )
            for i in range(50)
        ]
        session.add_all(requests)
        await session.flush()

        # Filtrar por estado PENDING
        stmt_pending = select(RoleRequest).where(RoleRequest.estado == RoleRequestStatus.PENDING)
        res_pending = await session.execute(stmt_pending)
        pending_records = res_pending.scalars().all()
        # En 50 registros, múltiplos de 4 (0, 4, 8, ... 48) son 13 APPROVED, restan 37 PENDING
        assert len(pending_records) == 37

        # Filtrar por equipo
        stmt_team = select(RoleRequest).where(RoleRequest.equipo == "Bulk Team Alpha")
        res_team = await session.execute(stmt_team)
        team_records = res_team.scalars().all()
        assert len(team_records) == 25

        # Filtrar por canal_id IS NOT NULL
        stmt_channel = select(RoleRequest).where(RoleRequest.canal_id.is_not(None))
        res_channel = await session.execute(stmt_channel)
        channel_records = res_channel.scalars().all()
        assert len(channel_records) == 17
