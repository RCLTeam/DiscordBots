"""Empirical adversarial test suite for Milestone 2: CasterRepository and CasterService.

Adversarially challenging:
1. High-concurrency race condition: simultaneous calls to assign_caster for streamer slot.
2. Interleaved race condition forcing DB-level IntegrityError past Python check.
3. High-concurrency casters + streamer coexistence.
4. Concurrent double-click by same user.
5. Rapid role toggling and slot release / contention across multiple users.
6. Chaotic concurrent toggling stress test.
7. Edge inputs: negative user IDs, zero, empty/whitespace strings, malformed types, BIGINT limits.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DataError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from liga_bot.database import _engine_locks
from liga_bot.models import (
    CasterRole,
    Division,
    Match,
    MatchCaster,
    Team,
)
from liga_bot.repositories.caster_repo import CasterRepository
from liga_bot.services.caster_service import (
    CasterAssignmentResult,
    CasterService,
)


@pytest.fixture(autouse=True)
def reset_engine_locks():
    """Limpia los locks de motor ligados a event loops anteriores."""
    _engine_locks.clear()
    yield
    _engine_locks.clear()


@pytest_asyncio.fixture
async def db_session(migrated_db: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provee una sesión limpia truncando tablas de casters y partidos."""
    session_factory = async_sessionmaker(bind=migrated_db, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(
            text("TRUNCATE TABLE match_casters, match_caster_cards, matches, teams CASCADE;")
        )
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(
            text("TRUNCATE TABLE match_casters, match_caster_cards, matches, teams CASCADE;")
        )
        await session.commit()


@pytest.fixture
def session_factory(migrated_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factoría de sesiones asíncronas para pruebas concurrentes."""
    return async_sessionmaker(bind=migrated_db, expire_on_commit=False)


@pytest.fixture
def caster_service(session_factory: async_sessionmaker[AsyncSession]) -> CasterService:
    """Instancia del servicio de casters."""
    return CasterService(session_factory=session_factory)


@pytest.fixture
async def sample_match(db_session: AsyncSession) -> Match:
    """Crea equipos y un partido de prueba."""
    u = uuid.uuid4().hex[:6]
    t1 = Team(
        name=f"Team A {u}",
        tag=f"TA{u[:2].upper()}",
        slug=f"team-a-{u}",
        division=Division.PREMIER,
        discord_role_id=int(f"101{int(u[:4], 16):012d}"),
    )
    t2 = Team(
        name=f"Team B {u}",
        tag=f"TB{u[2:4].upper()}",
        slug=f"team-b-{u}",
        division=Division.PREMIER,
        discord_role_id=int(f"102{int(u[2:6], 16):012d}"),
    )
    db_session.add_all([t1, t2])
    await db_session.flush()

    match = Match(
        jornada=1,
        division=Division.PREMIER,
        team1_id=t1.id,
        team2_id=t2.id,
        scheduled_at=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
    )
    db_session.add(match)
    await db_session.commit()
    return match


# ===========================================================================
# 1. High-Concurrency Race Conditions
# ===========================================================================


class TestHighConcurrencyRaceConditions:
    """Challenge 1: Concurrency and Race Conditions."""

    @pytest.mark.asyncio
    async def test_concurrent_two_streamers_exact_same_time(
        self,
        caster_service: CasterService,
        sample_match: Match,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Dos tareas concurrentes intentan registrarse como STREAMER al mismo milisegundo."""
        u1 = 100000000000000001
        u2 = 100000000000000002

        results = await asyncio.gather(
            caster_service.assign_caster(sample_match.id, u1, CasterRole.STREAMER),
            caster_service.assign_caster(sample_match.id, u2, CasterRole.STREAMER),
        )

        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
        assert len(failures) == 1, f"Expected exactly 1 failure, got {len(failures)}"

        winner = successes[0]
        loser = failures[0]

        assert winner.error is None
        assert winner.data is not None
        assert winner.data.has_streamer is True

        assert loser.error == "Ya hay una persona asignada a la retransmisión de este partido."
        assert loser.data is not None
        assert loser.data.has_streamer is True
        assert loser.data.streamer is not None
        assert loser.data.streamer.discord_user_id == winner.data.streamer.discord_user_id

        # Verificar BD
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count(MatchCaster.id)).where(MatchCaster.match_id == sample_match.id)
            )
            assert count == 1

    @pytest.mark.asyncio
    async def test_high_concurrency_ten_streamers_race(
        self,
        caster_service: CasterService,
        sample_match: Match,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """10 tareas concurrentes compiten por la única ranura de STREAMER."""
        user_ids = [200000000000000000 + i for i in range(10)]

        tasks = [
            caster_service.assign_caster(sample_match.id, uid, CasterRole.STREAMER)
            for uid in user_ids
        ]
        results = await asyncio.gather(*tasks)

        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        assert len(successes) == 1, f"Expected 1 winner, got {len(successes)}"
        assert len(failures) == 9, f"Expected 9 failures, got {len(failures)}"

        winner_id = successes[0].data.streamer.discord_user_id
        for f in failures:
            assert f.error == "Ya hay una persona asignada a la retransmisión de este partido."
            assert f.data.has_streamer is True
            assert f.data.streamer.discord_user_id == winner_id

        async with session_factory() as session:
            count = await session.scalar(
                select(func.count(MatchCaster.id)).where(MatchCaster.match_id == sample_match.id)
            )
            assert count == 1

    @pytest.mark.asyncio
    async def test_concurrent_streamer_vs_both_race(
        self,
        caster_service: CasterService,
        sample_match: Match,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """5 usuarios intentan STREAMER y 5 intentan BOTH simultáneamente."""
        streamer_uids = [300000000000000000 + i for i in range(5)]
        both_uids = [310000000000000000 + i for i in range(5)]

        tasks = [
            caster_service.assign_caster(sample_match.id, uid, CasterRole.STREAMER)
            for uid in streamer_uids
        ] + [
            caster_service.assign_caster(sample_match.id, uid, CasterRole.BOTH) for uid in both_uids
        ]

        results = await asyncio.gather(*tasks)
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        assert len(successes) == 1
        assert len(failures) == 9

        winner = successes[0]
        if winner.data.streamer.discord_user_id in both_uids:
            assert winner.data.streamer.caster_role == CasterRole.BOTH
            assert len(winner.data.casters) == 1
        else:
            assert winner.data.streamer.caster_role == CasterRole.STREAMER
            assert len(winner.data.casters) == 0

        async with session_factory() as session:
            count = await session.scalar(
                select(func.count(MatchCaster.id)).where(MatchCaster.match_id == sample_match.id)
            )
            assert count == 1

    @pytest.mark.asyncio
    async def test_concurrent_casters_and_streamers(
        self,
        caster_service: CasterService,
        sample_match: Match,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """10 casters y 5 streamers compiten en paralelo.
        Los 10 casters deben entrar; solo 1 streamer debe entrar.
        """
        caster_uids = [400000000000000000 + i for i in range(10)]
        streamer_uids = [410000000000000000 + i for i in range(5)]

        tasks = [
            caster_service.assign_caster(sample_match.id, uid, CasterRole.CASTER)
            for uid in caster_uids
        ] + [
            caster_service.assign_caster(sample_match.id, uid, CasterRole.STREAMER)
            for uid in streamer_uids
        ]

        results = await asyncio.gather(*tasks)
        caster_results = results[:10]
        streamer_results = results[10:]

        assert all(r.success for r in caster_results), "All 10 casters must succeed"
        streamer_successes = [r for r in streamer_results if r.success]
        streamer_failures = [r for r in streamer_results if not r.success]

        assert len(streamer_successes) == 1, "Exactly 1 streamer must succeed"
        assert len(streamer_failures) == 4, "4 streamers must fail"

        async with session_factory() as session:
            casters_count = await session.scalar(
                select(func.count(MatchCaster.id)).where(
                    MatchCaster.match_id == sample_match.id,
                    MatchCaster.caster_role == CasterRole.CASTER,
                )
            )
            streamers_count = await session.scalar(
                select(func.count(MatchCaster.id)).where(
                    MatchCaster.match_id == sample_match.id,
                    MatchCaster.caster_role == CasterRole.STREAMER,
                )
            )
            assert casters_count == 10
            assert streamers_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_same_user_double_click(
        self,
        caster_service: CasterService,
        sample_match: Match,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Un mismo usuario envía 2 clics concurrentes idénticos (double click)."""
        uid = 500000000000000001
        results = await asyncio.gather(
            caster_service.assign_caster(sample_match.id, uid, CasterRole.STREAMER),
            caster_service.assign_caster(sample_match.id, uid, CasterRole.STREAMER),
        )

        # Al menos una llamada tiene éxito y ninguna debe lanzar excepción no manejada
        assert any(r.success for r in results)
        for r in results:
            if r.success:
                assert r.data.has_streamer is True
                assert r.data.streamer.discord_user_id == uid

        async with session_factory() as session:
            count = await session.scalar(
                select(func.count(MatchCaster.id)).where(
                    MatchCaster.match_id == sample_match.id,
                    MatchCaster.discord_user_id == uid,
                )
            )
            assert count == 1

    @pytest.mark.asyncio
    async def test_simulated_db_level_race_past_python_check(
        self,
        caster_service: CasterService,
        sample_match: Match,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Fuerza que dos tareas pasen la comprobación Python (existing_streamer is None)
        mediante un retardo artificial antes de repo.assign, probando la defensa de BD.
        """
        original_assign = CasterRepository.assign

        async def delayed_assign(self, match_id, user_id, role):
            # Introduce una pausa para que ambas corrutinas hayan leído None en el select
            await asyncio.sleep(0.05)
            return await original_assign(self, match_id, user_id, role)

        monkeypatch.setattr(CasterRepository, "assign", delayed_assign)

        u1 = 600000000000000001
        u2 = 600000000000000002

        results = await asyncio.gather(
            caster_service.assign_caster(sample_match.id, u1, CasterRole.STREAMER),
            caster_service.assign_caster(sample_match.id, u2, CasterRole.STREAMER),
        )

        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        assert len(successes) == 1, f"Expected 1 winner, got {len(successes)}"
        assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"
        assert (
            failures[0].error == "Ya hay una persona asignada a la retransmisión de este partido."
        )
        assert failures[0].data.has_streamer is True

        async with session_factory() as session:
            count = await session.scalar(
                select(func.count(MatchCaster.id)).where(MatchCaster.match_id == sample_match.id)
            )
            assert count == 1


# ===========================================================================
# 2. Rapid Toggling & State Invariants
# ===========================================================================


class TestRapidTogglingAndStateInvariants:
    """Challenge 2: Rapid toggling between roles and removal."""

    @pytest.mark.asyncio
    async def test_single_user_sequential_rapid_toggle(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Ciclo rápido:
        CASTER -> STREAMER -> BOTH -> remove -> BOTH -> CASTER -> STREAMER -> remove.
        """
        uid = 700000000000000001

        # 1. CASTER
        r1 = await caster_service.assign_caster(sample_match.id, uid, CasterRole.CASTER)
        assert r1.success is True
        assert r1.data.has_streamer is False
        assert len(r1.data.casters) == 1

        # 2. Upgrade to STREAMER
        r2 = await caster_service.assign_caster(sample_match.id, uid, CasterRole.STREAMER)
        assert r2.success is True
        assert r2.data.has_streamer is True
        assert r2.data.streamer.discord_user_id == uid
        assert len(r2.data.casters) == 0

        # 3. Upgrade to BOTH
        r3 = await caster_service.assign_caster(sample_match.id, uid, CasterRole.BOTH)
        assert r3.success is True
        assert r3.data.has_streamer is True
        assert r3.data.streamer.discord_user_id == uid
        assert len(r3.data.casters) == 1
        assert r3.data.casters[0].discord_user_id == uid

        # 4. Remove
        r4 = await caster_service.remove_caster(sample_match.id, uid)
        assert r4.success is True
        assert r4.data.has_streamer is False
        assert r4.data.streamer is None
        assert len(r4.data.casters) == 0

        # 5. Re-assign BOTH
        r5 = await caster_service.assign_caster(sample_match.id, uid, CasterRole.BOTH)
        assert r5.success is True
        assert r5.data.has_streamer is True
        assert len(r5.data.casters) == 1

        # 6. Downgrade to CASTER (slot must be freed!)
        r6 = await caster_service.assign_caster(sample_match.id, uid, CasterRole.CASTER)
        assert r6.success is True
        assert r6.data.has_streamer is False
        assert r6.data.streamer is None
        assert len(r6.data.casters) == 1

        # 7. Upgrade to STREAMER
        r7 = await caster_service.assign_caster(sample_match.id, uid, CasterRole.STREAMER)
        assert r7.success is True
        assert r7.data.has_streamer is True
        assert len(r7.data.casters) == 0

        # 8. Remove
        r8 = await caster_service.remove_caster(sample_match.id, uid)
        assert r8.success is True
        assert r8.data.has_streamer is False

    @pytest.mark.asyncio
    async def test_multi_user_slot_contention_and_handoff(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Traspaso y contienda de ranura entre dos usuarios."""
        u1, u2 = 710000000000000001, 710000000000000002

        # U1 se asigna como STREAMER
        r1 = await caster_service.assign_caster(sample_match.id, u1, CasterRole.STREAMER)
        assert r1.success is True

        # U2 intenta STREAMER -> debe ser rechazado
        r2 = await caster_service.assign_caster(sample_match.id, u2, CasterRole.STREAMER)
        assert r2.success is False
        assert r2.error == "Ya hay una persona asignada a la retransmisión de este partido."

        # U2 intenta BOTH -> debe ser rechazado
        r3 = await caster_service.assign_caster(sample_match.id, u2, CasterRole.BOTH)
        assert r3.success is False

        # U2 entra como CASTER -> debe tener éxito
        r4 = await caster_service.assign_caster(sample_match.id, u2, CasterRole.CASTER)
        assert r4.success is True
        assert r4.data.has_streamer is True
        assert r4.data.streamer.discord_user_id == u1
        assert len(r4.data.casters) == 1
        assert r4.data.casters[0].discord_user_id == u2

        # U1 baja a CASTER -> libera la ranura de streamer
        r5 = await caster_service.assign_caster(sample_match.id, u1, CasterRole.CASTER)
        assert r5.success is True
        assert r5.data.has_streamer is False
        assert len(r5.data.casters) == 2

        # Ahora U2 se sube a BOTH -> tiene éxito y ocupa la ranura
        r6 = await caster_service.assign_caster(sample_match.id, u2, CasterRole.BOTH)
        assert r6.success is True
        assert r6.data.has_streamer is True
        assert r6.data.streamer.discord_user_id == u2
        assert len(r6.data.casters) == 2

        # U1 intenta STREAMER -> rechazado porque U2 lo tiene
        r7 = await caster_service.assign_caster(sample_match.id, u1, CasterRole.STREAMER)
        assert r7.success is False

        # U2 se desapunta
        r8 = await caster_service.remove_caster(sample_match.id, u2)
        assert r8.success is True
        assert r8.data.has_streamer is False
        assert len(r8.data.casters) == 1
        assert r8.data.casters[0].discord_user_id == u1

    @pytest.mark.asyncio
    async def test_chaotic_concurrent_toggling(
        self,
        caster_service: CasterService,
        sample_match: Match,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Tres usuarios ejecutando 15 operaciones aleatorias/alternadas concurrentemente.
        El invariante de como máximo 1 streamer nunca debe romperse.
        """
        users = [800000000000000001, 800000000000000002, 800000000000000003]
        roles = [CasterRole.CASTER, CasterRole.STREAMER, CasterRole.BOTH, "remove"]

        async def worker_loop(uid: int):
            for _ in range(15):
                action = random.choice(roles)
                if action == "remove":
                    res = await caster_service.remove_caster(sample_match.id, uid)
                else:
                    res = await caster_service.assign_caster(sample_match.id, uid, action)

                if res.data is not None:
                    # Invariante de coherencia de datos
                    if res.data.has_streamer:
                        assert res.data.streamer is not None
                        assert res.data.streamer.caster_role in (
                            CasterRole.STREAMER,
                            CasterRole.BOTH,
                        )
                    else:
                        assert res.data.streamer is None

        await asyncio.gather(*(worker_loop(u) for u in users))

        # Verificación final en BD
        async with session_factory() as session:
            streamers = (
                (
                    await session.execute(
                        select(MatchCaster).where(
                            MatchCaster.match_id == sample_match.id,
                            MatchCaster.caster_role.in_([CasterRole.STREAMER, CasterRole.BOTH]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(streamers) <= 1, (
                f"Invariant violated: found {len(streamers)} streamers in DB!"
            )


# ===========================================================================
# 3. Edge Inputs & Boundary Testing
# ===========================================================================


class TestEdgeInputsAndBoundaries:
    """Challenge 3: Edge inputs, negative IDs, zero, empty/whitespace strings, bigints."""

    @pytest.mark.asyncio
    async def test_negative_user_id(self, caster_service: CasterService, sample_match: Match):
        """ID de usuario negativo es rechazado limpiamente."""
        neg_uid = -12345
        res = await caster_service.assign_caster(sample_match.id, neg_uid, CasterRole.CASTER)
        assert res.success is False
        assert res.error == "ID de usuario de Discord inválido."
        rem_res = await caster_service.remove_caster(sample_match.id, neg_uid)
        assert rem_res.success is False
        assert rem_res.error == "ID de usuario de Discord inválido."

    @pytest.mark.asyncio
    async def test_zero_user_id(self, caster_service: CasterService, sample_match: Match):
        """ID de usuario cero es rechazado limpiamente."""
        res = await caster_service.assign_caster(sample_match.id, 0, CasterRole.STREAMER)
        assert res.success is False
        assert res.error == "ID de usuario de Discord inválido."

    @pytest.mark.asyncio
    async def test_empty_string_match_id(self, caster_service: CasterService):
        """match_id como string vacío retorna error limpio sin excepción de BD."""
        res = await caster_service.assign_caster("", 123, CasterRole.CASTER)
        assert res.success is False
        assert res.error == "Identificador de partido inválido."

        rem = await caster_service.remove_caster("", 123)
        assert rem.success is False
        assert rem.error == "Identificador de partido inválido."

    @pytest.mark.asyncio
    async def test_whitespace_match_id(self, caster_service: CasterService):
        """match_id con solo espacios en blanco retorna error limpio."""
        res = await caster_service.assign_caster("   \t\n  ", 123, CasterRole.CASTER)
        assert res.success is False
        assert res.error == "Identificador de partido inválido."

    @pytest.mark.asyncio
    async def test_sql_injection_match_id(self, caster_service: CasterService):
        """Inyección SQL en match_id no se evalúa y es rechazada limpiamente."""
        res = await caster_service.assign_caster(
            "'; DROP TABLE match_casters; --", 123, CasterRole.CASTER
        )
        assert res.success is False
        assert res.error == "Identificador de partido inválido."

    @pytest.mark.asyncio
    async def test_empty_or_whitespace_role(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Rol como string vacío o espacios en blanco es rechazado limpiamente."""
        res1 = await caster_service.assign_caster(sample_match.id, 123, "")
        assert res1.success is False
        assert "Rol de casteo inválido" in res1.error

        res2 = await caster_service.assign_caster(sample_match.id, 123, "   ")
        assert res2.success is False
        assert "Rol de casteo inválido" in res2.error

    @pytest.mark.asyncio
    async def test_invalid_role_name(self, caster_service: CasterService, sample_match: Match):
        """Rol inválido (ej. 'ADMIN', 'CO_STREAMER') es rechazado limpiamente."""
        res = await caster_service.assign_caster(sample_match.id, 123, "CO_STREAMER")
        assert res.success is False
        assert "Rol de casteo inválido" in res.error

    @pytest.mark.asyncio
    async def test_max_bigint_user_id(self, caster_service: CasterService, sample_match: Match):
        """Límite superior de PostgreSQL BIGINT (2^63 - 1 = 9223372036854775807)."""
        max_bigint = 9223372036854775807
        res = await caster_service.assign_caster(sample_match.id, max_bigint, CasterRole.STREAMER)
        assert res.success is True
        assert res.data.streamer.discord_user_id == max_bigint

    @pytest.mark.asyncio
    async def test_overflow_bigint_user_id(
        self, caster_service: CasterService, sample_match: Match
    ):
        """Desbordamiento de BIGINT (2^63 = 9223372036854775808): cómo reacciona."""
        overflow_val = 9223372036854775808
        try:
            await caster_service.assign_caster(sample_match.id, overflow_val, CasterRole.CASTER)
        except (DataError, DBAPIError):
            assert True

    @pytest.mark.asyncio
    async def test_empty_string_user_id(self, caster_service: CasterService, sample_match: Match):
        """user_id como string vacío es rechazado limpiamente."""
        res: CasterAssignmentResult = await caster_service.assign_caster(
            sample_match.id,
            "",
            CasterRole.CASTER,  # type: ignore[arg-type]
        )
        assert res.success is False
        assert res.error == "ID de usuario de Discord inválido."

    @pytest.mark.asyncio
    async def test_whitespace_string_user_id(
        self, caster_service: CasterService, sample_match: Match
    ):
        """user_id como whitespace string es rechazado limpiamente."""
        res: CasterAssignmentResult = await caster_service.assign_caster(
            sample_match.id,
            "   ",
            CasterRole.CASTER,  # type: ignore[arg-type]
        )
        assert res.success is False
        assert res.error == "ID de usuario de Discord inválido."
