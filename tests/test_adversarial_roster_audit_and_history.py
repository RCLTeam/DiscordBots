"""
Adversarial Empirical Challenge Suite for Milestone 2:
- AuditLogRepository.log JSONB serialization safety & roundtrip fidelity on PGlite.
- RosterMovementRepository chronological integrity & ordering invariants on PGlite.
"""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liga_bot.models.enums import Division, RosterMovementAction, RosterRole
from liga_bot.models.roster import AuditLog, DiscordUser, RosterMovement, Team
from liga_bot.repositories.roster_repo import (
    AuditLogRepository,
    RosterMovementRepository,
    _to_json_safe,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_log_repo(session: AsyncSession) -> AuditLogRepository:
    return AuditLogRepository(session)


@pytest.fixture
def roster_movement_repo(session: AsyncSession) -> RosterMovementRepository:
    return RosterMovementRepository(session)


@pytest.fixture
async def seed_users(session: AsyncSession) -> list[DiscordUser]:
    """Crea una colección de usuarios Discord para pruebas de auditoría y movimientos."""
    users = [
        DiscordUser(
            discord_id=f"9990001112223330{i}",
            username=f"challenger_user_{i}",
            global_name=f"Challenger User {i}",
        )
        for i in range(1, 6)
    ]
    session.add_all(users)
    await session.flush()
    return users


@pytest.fixture
async def seed_teams(session: AsyncSession) -> tuple[Team, Team]:
    """Crea dos equipos canónicos para verificar aislamiento y cronología."""
    t1 = Team(
        name="Adversarial Alpha",
        tag="ALP",
        slug="adversarial-alpha",
        division=Division.PREMIER,
        discord_role_id=888001,
    )
    t2 = Team(
        name="Adversarial Beta",
        tag="BET",
        slug="adversarial-beta",
        division=Division.ASCEND,
        discord_role_id=888002,
    )
    session.add_all([t1, t2])
    await session.flush()
    return t1, t2


# ---------------------------------------------------------------------------
# Challenge 1: AuditLogRepository.log JSONB Serialization & Roundtrip
# ---------------------------------------------------------------------------


class TestAuditLogJsonbSerializationAdversarial:
    """Pruebas adversariales de serialización JSONB en AuditLogRepository.log contra PGlite."""

    async def test_complex_domain_payload_serialization_and_db_roundtrip(
        self,
        session: AsyncSession,
        audit_log_repo: AuditLogRepository,
        seed_users: list[DiscordUser],
    ) -> None:
        """
        Verifica que un diccionario de dominio complejo que contiene:
        - UUID objects
        - datetime.now(timezone.utc)
        - datetime ingenuo (naive) y date
        - RosterRole y RosterMovementAction (Enums)
        - Decimal
        - Listas anidadas y diccionarios multinivel
        - Valores None
        - Cadenas con caracteres Unicode complejos, tildes y emojis
        - Sets y tuplas
        se serialice sin TypeError y se recupere idénticamente tras roundtrip en PostgreSQL JSONB.
        """
        actor = seed_users[0]
        actor_id = actor.discord_id
        test_uuid_1 = uuid.uuid4()
        test_uuid_2 = uuid.uuid4()
        entity_uuid = uuid.uuid4()
        utc_dt = datetime(2026, 9, 19, 13, 50, 22, 987654, tzinfo=timezone.utc)
        naive_dt = datetime(2026, 5, 10, 15, 30, 0)
        sample_date = date(2026, 12, 31)

        complex_before: dict[str, Any] = {
            "entity_uuid": test_uuid_1,
            "recorded_at": utc_dt,
            "naive_timestamp": naive_dt,
            "target_date": sample_date,
            "previous_role": RosterRole.ADC,
            "action_trigger": RosterMovementAction.ROLE_CHANGED,
            "salary_cap": Decimal("15000.75"),
            "is_active": True,
            "nullable_slot": None,
            "tags": {"starter", "mvp"},
            "coordinates": (10, 25),
            "unicode_text": "Jugador añorado con roles 🌟 & ñandú — 日本語 🔥",
            "nested_list": [
                test_uuid_2,
                RosterRole.SUPPORT,
                RosterMovementAction.PROMOTED_TO_CAPTAIN,
                [None, utc_dt, {"deep_role": RosterRole.TOP}],
            ],
            "deeply_nested_dict": {
                "l1": {
                    "l2": {
                        "l3_uuid": test_uuid_1,
                        "l3_enum": RosterRole.MID,
                        "l3_action": RosterMovementAction.JOINED,
                        "l3_none": None,
                        "l3_empty_list": [],
                        "l3_empty_dict": {},
                    }
                }
            },
        }

        complex_after: dict[str, Any] = {
            "entity_uuid": test_uuid_1,
            "updated_at": utc_dt,
            "new_role": RosterRole.COACH,
            "action_trigger": RosterMovementAction.DEMOTED_FROM_CAPTAIN,
            "status_note": "Cambio de titular a cuerpo técnico 📋",
            "metadata": {
                "audit_actor": actor_id,
                "history_chain": [RosterRole.ADC, RosterRole.COACH],
                "numeric_metric": 42,
                "ratio": 3.14159,
            },
        }

        # 1. Ejecutar persistencia en AuditLogRepository
        entry = await audit_log_repo.log(
            actor_discord_user_id=actor_id,
            action="roster.adversarial_challenge",
            entity_type="team_membership",
            entity_id=entity_uuid,
            before=complex_before,
            after=complex_after,
        )

        assert entry.id is not None
        assert isinstance(entry.id, uuid.UUID)
        entry_id = entry.id

        # 2. Desasociar del identity map para garantizar que la consulta vaya físicamente a PGlite
        session.expunge_all()

        # 3. Leer directamente de la base de datos PGlite
        fresh_stmt = select(AuditLog).where(AuditLog.id == entry_id)
        fresh_entry = (await session.execute(fresh_stmt)).scalar_one()

        assert fresh_entry.before is not None
        assert fresh_entry.after is not None

        # 4. Validar deserialización exacta de tipos en `before`
        b = fresh_entry.before
        assert b["entity_uuid"] == str(test_uuid_1)
        assert b["recorded_at"] == utc_dt.isoformat()
        assert b["naive_timestamp"] == naive_dt.isoformat()
        assert b["target_date"] == sample_date.isoformat()
        assert b["previous_role"] == RosterRole.ADC.value
        assert b["action_trigger"] == RosterMovementAction.ROLE_CHANGED.value
        assert b["salary_cap"] == 15000.75
        assert b["is_active"] is True
        assert b["nullable_slot"] is None
        assert sorted(b["tags"]) == ["mvp", "starter"]
        assert b["coordinates"] == [10, 25]
        assert b["unicode_text"] == "Jugador añorado con roles 🌟 & ñandú — 日本語 🔥"

        # Validar estructuras anidadas
        assert b["nested_list"][0] == str(test_uuid_2)
        assert b["nested_list"][1] == RosterRole.SUPPORT.value
        assert b["nested_list"][2] == RosterMovementAction.PROMOTED_TO_CAPTAIN.value
        assert b["nested_list"][3][0] is None
        assert b["nested_list"][3][1] == utc_dt.isoformat()
        assert b["nested_list"][3][2]["deep_role"] == RosterRole.TOP.value

        l3 = b["deeply_nested_dict"]["l1"]["l2"]
        assert l3["l3_uuid"] == str(test_uuid_1)
        assert l3["l3_enum"] == RosterRole.MID.value
        assert l3["l3_action"] == RosterMovementAction.JOINED.value
        assert l3["l3_none"] is None
        assert l3["l3_empty_list"] == []
        assert l3["l3_empty_dict"] == {}

        # 5. Validar deserialización exacta de tipos en `after`
        a = fresh_entry.after
        assert a["entity_uuid"] == str(test_uuid_1)
        assert a["new_role"] == RosterRole.COACH.value
        assert a["action_trigger"] == RosterMovementAction.DEMOTED_FROM_CAPTAIN.value
        assert a["status_note"] == "Cambio de titular a cuerpo técnico 📋"
        assert a["metadata"]["audit_actor"] == actor_id
        assert a["metadata"]["history_chain"] == [RosterRole.ADC.value, RosterRole.COACH.value]
        assert a["metadata"]["numeric_metric"] == 42
        assert a["metadata"]["ratio"] == pytest.approx(3.14159)

    async def test_non_string_dictionary_keys_and_unusual_primitives(
        self,
        session: AsyncSession,
        audit_log_repo: AuditLogRepository,
        seed_users: list[DiscordUser],
    ) -> None:
        """
        Verifica la normalización segura cuando las claves del diccionario no son cadenas
        (UUIDs, enteros, Enums) y valores primitivos variados.
        """
        actor = seed_users[0]
        actor_id = actor.discord_id
        k_uuid = uuid.uuid4()
        payload = {
            100: "integer_key",
            k_uuid: "uuid_key",
            RosterRole.JUNGLE: "enum_key",
            "nested": {
                500: RosterRole.SUBSTITUTE,
            },
        }

        entry = await audit_log_repo.log(
            actor_discord_user_id=actor_id,
            action="roster.key_normalization_test",
            entity_type="membership",
            entity_id=k_uuid,
            before=payload,
        )
        entry_id = entry.id

        session.expunge_all()
        fresh = (
            await session.execute(select(AuditLog).where(AuditLog.id == entry_id))
        ).scalar_one()

        assert fresh.before is not None
        assert fresh.before["100"] == "integer_key"
        assert fresh.before[str(k_uuid)] == "uuid_key"
        assert (
            "jungle" in fresh.before
            or "RosterRole.JUNGLE" in fresh.before
            or str(RosterRole.JUNGLE) in fresh.before
        )
        assert fresh.before["nested"]["500"] == RosterRole.SUBSTITUTE.value

    async def test_null_empty_and_minimal_audit_logs(
        self,
        session: AsyncSession,
        audit_log_repo: AuditLogRepository,
    ) -> None:
        """
        Verifica que entradas con None o diccionarios vacíos en before/after
        persistan limpiamente.
        """
        # Sin actor, sin entity_id, before=None, after=None
        entry1 = await audit_log_repo.log(
            actor_discord_user_id=None,
            action="system.auto_purge",
            entity_type="system",
            entity_id=None,
            before=None,
            after=None,
        )
        assert entry1.id is not None
        assert entry1.actor_discord_user_id is None
        assert entry1.entity_id is None
        assert entry1.before is None
        assert entry1.after is None

        # Diccionarios explícitamente vacíos {}
        entry2 = await audit_log_repo.log(
            actor_discord_user_id=None,
            action="system.noop",
            entity_type="system",
            before={},
            after={},
        )
        assert entry2.before == {}
        assert entry2.after == {}

    async def test_invalid_entity_id_raises_value_error_without_db_corruption(
        self,
        audit_log_repo: AuditLogRepository,
    ) -> None:
        """Verifica que pasar una cadena que no sea UUID en entity_id lance ValueError defensivo."""
        with pytest.raises(ValueError, match="entity_id must be a valid UUID"):
            await audit_log_repo.log(
                actor_discord_user_id="123",
                action="test.fail",
                entity_type="team",
                entity_id="invalid-not-a-uuid-format",
            )

    async def test_deeply_nested_and_large_jsonb_structures(
        self,
        session: AsyncSession,
        audit_log_repo: AuditLogRepository,
    ) -> None:
        """Verifica que estructuras con alta profundidad y volumen persistan en JSONB sin fallo."""
        # Crear estructura de 25 niveles de anidación
        current: dict[str, Any] = {"leaf": "deepest_value", "role": RosterRole.PARTNERS}
        for level in range(25, 0, -1):
            current = {f"level_{level}": current, "depth": level}

        # Crear estructura ancha con 200 claves
        wide_dict: dict[str, Any] = {
            f"key_{i}": {
                "id": uuid.uuid4(),
                "role": random.choice(list(RosterRole)),
                "idx": i,
            }
            for i in range(200)
        }

        entry = await audit_log_repo.log(
            actor_discord_user_id=None,
            action="stress.deep_and_wide",
            entity_type="stress_test",
            before=current,
            after=wide_dict,
        )
        entry_id = entry.id

        session.expunge_all()
        fresh = (
            await session.execute(select(AuditLog).where(AuditLog.id == entry_id))
        ).scalar_one()

        assert fresh.before is not None
        assert fresh.after is not None
        assert len(fresh.after) == 200
        assert fresh.after["key_0"]["idx"] == 0
        assert isinstance(fresh.after["key_0"]["id"], str)

        # Verificar navegación en los 25 niveles
        node = fresh.before
        for level in range(1, 26):
            assert node["depth"] == level
            node = node[f"level_{level}"]
        assert node["leaf"] == "deepest_value"
        assert node["role"] == RosterRole.PARTNERS.value


# ---------------------------------------------------------------------------
# Challenge 2: RosterMovementRepository Chronological Integrity
# ---------------------------------------------------------------------------


class TestRosterMovementChronologicalIntegrityAdversarial:
    """
    Pruebas de integridad cronológica e invariante de orden descendente
    en RosterMovementRepository.
    """

    async def test_20_staggered_movements_strict_descending_order(
        self,
        session: AsyncSession,
        roster_movement_repo: RosterMovementRepository,
        seed_teams: tuple[Team, Team],
        seed_users: list[DiscordUser],
    ) -> None:
        """
        Inserta 20 movimientos con marcas temporales escalonadas (staggered)
        y desordenadas deliberadamente a través de dos equipos y múltiples usuarios.
        Verifica que list_by_team y list_by_user devuelvan los registros en orden
        estrictamente cronológico descendente (created_at.desc()).
        """
        team_a, team_b = seed_teams
        team_a_id = team_a.id
        team_b_id = team_b.id

        user_1, user_2, user_3, user_4 = seed_users[:4]
        user_1_id = user_1.discord_id
        user_2_id = user_2.discord_id
        user_3_id = user_3.discord_id
        user_4_id = user_4.discord_id

        base_time = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Diseñar 20 movimientos con offsets temporales deterministas en minutos
        # Distribución:
        # Team A: 12 movimientos (con user_1, user_2, user_3)
        # Team B: 8 movimientos (con user_2, user_4)
        records_spec: list[tuple[int, uuid.UUID, str, RosterMovementAction, RosterRole | None]] = [
            # Offset (minutos), Team ID, User ID, Action, Role
            (10, team_a_id, user_1_id, RosterMovementAction.JOINED, RosterRole.TOP),
            (25, team_a_id, user_1_id, RosterMovementAction.PROMOTED_TO_CAPTAIN, RosterRole.TOP),
            (40, team_a_id, user_2_id, RosterMovementAction.JOINED, RosterRole.JUNGLE),
            (60, team_b_id, user_2_id, RosterMovementAction.JOINED, RosterRole.COACH),
            (75, team_a_id, user_3_id, RosterMovementAction.JOINED, RosterRole.MID),
            (90, team_b_id, user_4_id, RosterMovementAction.JOINED, RosterRole.ADC),
            (110, team_a_id, user_1_id, RosterMovementAction.DEMOTED_FROM_CAPTAIN, RosterRole.TOP),
            (130, team_b_id, user_4_id, RosterMovementAction.PROMOTED_TO_CAPTAIN, RosterRole.ADC),
            (150, team_a_id, user_2_id, RosterMovementAction.ROLE_CHANGED, RosterRole.SUBSTITUTE),
            (180, team_b_id, user_2_id, RosterMovementAction.ROLE_CHANGED, RosterRole.STAFF),
            (200, team_a_id, user_3_id, RosterMovementAction.LEFT, None),
            (220, team_b_id, user_4_id, RosterMovementAction.ROLE_CHANGED, RosterRole.SUPPORT),
            (250, team_a_id, user_1_id, RosterMovementAction.ROLE_CHANGED, RosterRole.ADC),
            (270, team_a_id, user_2_id, RosterMovementAction.LEFT, None),
            (
                300,
                team_b_id,
                user_4_id,
                RosterMovementAction.DEMOTED_FROM_CAPTAIN,
                RosterRole.SUPPORT,
            ),
            (320, team_a_id, user_1_id, RosterMovementAction.PROMOTED_TO_CAPTAIN, RosterRole.ADC),
            (350, team_b_id, user_2_id, RosterMovementAction.LEFT, None),
            (380, team_a_id, user_1_id, RosterMovementAction.LEFT, None),
            (400, team_b_id, user_4_id, RosterMovementAction.LEFT, None),
            (450, team_a_id, user_1_id, RosterMovementAction.JOINED, RosterRole.COACH),
        ]

        # Insertar los registros deliberadamente en orden aleatorio desordenado
        # para demostrar que el ordenamiento no depende del orden físico de inserción en disco
        shuffled_specs = list(records_spec)
        random.seed(42)
        random.shuffle(shuffled_specs)

        inserted_movements: list[RosterMovement] = []
        for offset_minutes, t_id, u_id, action, role in shuffled_specs:
            mov_time = base_time + timedelta(minutes=offset_minutes)
            mov = await roster_movement_repo.record_movement(
                team_id=t_id,
                discord_user_id=u_id,
                action=action,
                role=role,
                actor_id=user_1_id,
                created_at=mov_time,
            )
            inserted_movements.append(mov)

        assert len(inserted_movements) == 20

        # Desasociar del identity map para forzar lecturas SQL directas
        session.expunge_all()

        # -------------------------------------------------------------------
        # Verificación 1: list_by_team(team_a_id)
        # -------------------------------------------------------------------
        team_a_movements = await roster_movement_repo.list_by_team(team_a_id)
        assert len(team_a_movements) == 12

        # Verificar orden cronológico estrictamente descendente
        for idx in range(len(team_a_movements) - 1):
            current_dt = team_a_movements[idx].created_at
            next_dt = team_a_movements[idx + 1].created_at
            assert current_dt > next_dt, (
                f"Infracción cronológica en list_by_team(Team A) índice {idx}: "
                f"{current_dt} no es mayor que {next_dt}"
            )

        # El más reciente en Team A debe ser el de offset 450 (JOINED como COACH)
        assert team_a_movements[0].action == RosterMovementAction.JOINED
        assert team_a_movements[0].role == RosterRole.COACH
        assert team_a_movements[0].created_at == base_time + timedelta(minutes=450)

        # El más antiguo en Team A debe ser el de offset 10 (JOINED como TOP)
        assert team_a_movements[-1].action == RosterMovementAction.JOINED
        assert team_a_movements[-1].role == RosterRole.TOP
        assert team_a_movements[-1].created_at == base_time + timedelta(minutes=10)

        # -------------------------------------------------------------------
        # Verificación 2: list_by_team(team_b_id)
        # -------------------------------------------------------------------
        team_b_movements = await roster_movement_repo.list_by_team(team_b_id)
        assert len(team_b_movements) == 8

        for idx in range(len(team_b_movements) - 1):
            assert team_b_movements[idx].created_at > team_b_movements[idx + 1].created_at, (
                f"Infracción cronológica en list_by_team(Team B) índice {idx}"
            )

        # El más reciente en Team B es offset 400 (user_4 LEFT)
        assert team_b_movements[0].created_at == base_time + timedelta(minutes=400)
        assert team_b_movements[0].action == RosterMovementAction.LEFT

        # El más antiguo en Team B es offset 60 (user_2 JOINED como COACH)
        assert team_b_movements[-1].created_at == base_time + timedelta(minutes=60)

        # -------------------------------------------------------------------
        # Verificación 3: list_by_user(user_1_id)
        # User 1 tiene 7 movimientos en total (todos en Team A)
        # -------------------------------------------------------------------
        user_1_movements = await roster_movement_repo.list_by_user(user_1_id)
        assert len(user_1_movements) == 7

        for idx in range(len(user_1_movements) - 1):
            assert user_1_movements[idx].created_at > user_1_movements[idx + 1].created_at, (
                f"Infracción cronológica en list_by_user(User 1) índice {idx}"
            )

        # -------------------------------------------------------------------
        # Verificación 4: list_by_user(user_2_id)
        # User 2 tiene 6 movimientos repartidos entre Team A y Team B
        # -------------------------------------------------------------------
        user_2_movements = await roster_movement_repo.list_by_user(user_2_id)
        assert len(user_2_movements) == 6

        for idx in range(len(user_2_movements) - 1):
            assert user_2_movements[idx].created_at > user_2_movements[idx + 1].created_at, (
                f"Infracción cronológica en list_by_user(User 2) índice {idx}"
            )

        # -------------------------------------------------------------------
        # Verificación 5: Aislamiento estricto de clubes y usuarios
        # -------------------------------------------------------------------
        for m in team_a_movements:
            assert m.team_id == team_a_id
        for m in team_b_movements:
            assert m.team_id == team_b_id
        for m in user_1_movements:
            assert m.discord_user_id == user_1_id

    async def test_pagination_and_limits_chronological_integrity(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_teams: tuple[Team, Team],
        seed_users: list[DiscordUser],
    ) -> None:
        """
        Verifica que los parámetros `limit` devuelvan exactamente el subconjunto superior
        más reciente sin alterar el orden descendente.
        """
        team_a = seed_teams[0]
        team_a_id = team_a.id
        user = seed_users[0]
        user_id = user.discord_id
        base_time = datetime(2026, 9, 10, 8, 0, 0, tzinfo=timezone.utc)

        # Registrar 10 movimientos
        for i in range(10):
            await roster_movement_repo.record_movement(
                team_id=team_a_id,
                discord_user_id=user_id,
                action=RosterMovementAction.ROLE_CHANGED,
                role=RosterRole.SUBSTITUTE,
                created_at=base_time + timedelta(hours=i),
            )

        # Limit 1: Devuelve únicamente el más reciente (hora 9)
        res_1 = await roster_movement_repo.list_by_team(team_a_id, limit=1)
        assert len(res_1) == 1
        assert res_1[0].created_at == base_time + timedelta(hours=9)

        # Limit 3: Devuelve las 3 horas más recientes (9, 8, 7)
        res_3 = await roster_movement_repo.list_by_team(team_a_id, limit=3)
        assert len(res_3) == 3
        expected_times = [
            base_time + timedelta(hours=9),
            base_time + timedelta(hours=8),
            base_time + timedelta(hours=7),
        ]
        assert [m.created_at for m in res_3] == expected_times

        # Limit grande (50) devuelve los 10 sin error
        res_all = await roster_movement_repo.list_by_team(team_a_id, limit=50)
        assert len(res_all) == 10

        # Limit <= 0 ignora el limit y retorna todo
        res_zero = await roster_movement_repo.list_by_team(team_a_id, limit=0)
        assert len(res_zero) == 10

    async def test_identical_timestamps_ordering_stability(
        self,
        roster_movement_repo: RosterMovementRepository,
        seed_teams: tuple[Team, Team],
        seed_users: list[DiscordUser],
    ) -> None:
        """
        Verifica el comportamiento cuando múltiples movimientos comparten exactamente
        la misma marca temporal (mismo microsegundo).
        """
        team_a = seed_teams[0]
        team_a_id = team_a.id
        user = seed_users[0]
        user_id = user.discord_id
        fixed_dt = datetime(2026, 9, 15, 12, 0, 0, 555555, tzinfo=timezone.utc)

        for role in [RosterRole.TOP, RosterRole.JUNGLE, RosterRole.MID, RosterRole.ADC]:
            await roster_movement_repo.record_movement(
                team_id=team_a_id,
                discord_user_id=user_id,
                action=RosterMovementAction.ROLE_CHANGED,
                role=role,
                created_at=fixed_dt,
            )

        records = await roster_movement_repo.list_by_team(team_a_id)
        assert len(records) == 4
        # Todos comparten el mismo timestamp sin provocar errores en el order_by
        for r in records:
            assert r.created_at == fixed_dt

    async def test_defensive_lookups_and_boundary_conditions(
        self,
        roster_movement_repo: RosterMovementRepository,
    ) -> None:
        """
        Verifica que consultas con identificadores malformados o vacíos retornen
        listas vacías de forma segura sin abortar la transacción en PostgreSQL.
        """
        # team_id no válido
        assert await roster_movement_repo.list_by_team("not-a-valid-uuid") == []
        assert await roster_movement_repo.list_by_team("   ") == []
        assert await roster_movement_repo.list_by_team("123-abc") == []

        # discord_user_id no válido
        assert await roster_movement_repo.list_by_user("") == []
        assert await roster_movement_repo.list_by_user("   ") == []
        assert await roster_movement_repo.list_by_user(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Challenge 3: Helper function _to_json_safe direct unit verification
# ---------------------------------------------------------------------------


class TestToJsonSafeHelperAdversarial:
    """Pruebas unitarias de resistencia para _to_json_safe."""

    def test_to_json_safe_primitives_and_collections(self) -> None:
        test_uuid = uuid.uuid4()
        now = datetime.now(timezone.utc)
        today = date.today()

        payload = {
            "uuid": test_uuid,
            "dt": now,
            "d": today,
            "enum_role": RosterRole.ADC,
            "enum_act": RosterMovementAction.ROLE_CHANGED,
            "dec": Decimal("123.45"),
            "tuple_val": (1, 2, 3),
            "set_val": {"x", "y"},
            "none_val": None,
            "bool_val": False,
            "int_val": 42,
            "float_val": 3.14,
            "str_val": "hola",
        }

        safe = _to_json_safe(payload)

        assert safe["uuid"] == str(test_uuid)
        assert safe["dt"] == now.isoformat()
        assert safe["d"] == today.isoformat()
        assert safe["enum_role"] == "adc" or safe["enum_role"] == RosterRole.ADC
        assert (
            safe["enum_act"] == "role_changed"
            or safe["enum_act"] == RosterMovementAction.ROLE_CHANGED
        )
        assert safe["dec"] == 123.45
        assert safe["tuple_val"] == [1, 2, 3]
        assert isinstance(safe["set_val"], list)
        assert sorted(safe["set_val"]) == ["x", "y"]
        assert safe["none_val"] is None
        assert safe["bool_val"] is False
        assert safe["int_val"] == 42
        assert safe["float_val"] == 3.14
        assert safe["str_val"] == "hola"
