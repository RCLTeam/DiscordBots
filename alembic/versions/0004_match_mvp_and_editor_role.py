"""match_mvp_and_editor_role

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27 11:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.sql.elements import conv

from alembic import op

# Identificadores de revisión utilizados por Alembic
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Sincroniza de forma defensiva el rol 'editor' y los campos MVP de matches.

    En producción los crean las migraciones de RCL-Next (0002_match_mvp.sql,
    0003_match_mvp_checks.sql y 0004_editor_role.sql). Cada operación comprueba
    primero si el objeto ya existe; si ya fue aplicado por RCL-Next, se omite sin error.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Valor 'editor' en el ENUM nativo app_role (PostgreSQL / PGlite)
    if bind.dialect.name == "postgresql":
        has_app_role = bind.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = 'app_role'")
        ).scalar()
        if has_app_role:
            has_editor = bind.execute(
                sa.text(
                    "SELECT 1 "
                    "FROM pg_enum e "
                    "JOIN pg_type t ON e.enumtypid = t.oid "
                    "WHERE t.typname = 'app_role' AND e.enumlabel = 'editor'"
                )
            ).scalar()
            if not has_editor:
                op.execute("ALTER TYPE app_role ADD VALUE IF NOT EXISTS 'editor' BEFORE 'admin'")

    existing_tables = set(inspector.get_table_names())
    if "matches" not in existing_tables:
        return

    # 2. Columnas mvp_player_id y mvp_score en matches
    existing_columns = {col["name"] for col in inspector.get_columns("matches")}
    if "mvp_player_id" not in existing_columns:
        op.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS mvp_player_id uuid")
    if "mvp_score" not in existing_columns:
        op.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS mvp_score numeric(5, 1)")

    # 3. Clave foránea matches_mvp_player_id_fkey hacia players(id) ON DELETE SET NULL
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys("matches") if fk.get("name")}
    if "matches_mvp_player_id_fkey" not in existing_fks and "players" in existing_tables:
        op.create_foreign_key(
            "matches_mvp_player_id_fkey",
            "matches",
            "players",
            ["mvp_player_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 4. Restricciones CHECK de MVP en matches
    existing_checks = {
        ck["name"] for ck in inspector.get_check_constraints("matches") if ck.get("name")
    }
    if "matches_mvp_score_nonnegative_check" not in existing_checks:
        op.create_check_constraint(
            conv("matches_mvp_score_nonnegative_check"),
            "matches",
            "mvp_score IS NULL OR mvp_score >= 0",
        )
    if "matches_mvp_player_score_check" not in existing_checks:
        op.create_check_constraint(
            conv("matches_mvp_player_score_check"),
            "matches",
            "mvp_player_id IS NULL OR mvp_score IS NOT NULL",
        )

    # 5. Índice parcial matches_round_mvp_score_idx
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("matches") if idx.get("name")}
    if "matches_round_mvp_score_idx" not in existing_indexes:
        op.execute(
            "CREATE INDEX IF NOT EXISTS matches_round_mvp_score_idx "
            "ON matches USING btree (id_season_division, id_round, mvp_score DESC NULLS LAST) "
            "WHERE status = 'completed' AND mvp_player_id IS NOT NULL"
        )


def downgrade() -> None:
    """No elimina objetos del esquema compartido gobernado por RCL-Next."""
