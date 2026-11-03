"""Filtro de Alembic para las tablas que gobierna RCL-Next (Drizzle ORM)."""

import sqlalchemy as sa

# Tablas compartidas cuya gobernanza pertenece exclusivamente a RCL-Next (Drizzle ORM).
# Quedan estrictamente excluidas de las migraciones y autogenerate de Alembic en DiscordBots.
SHARED_TABLES: set[str] = {
    "teams",
    "matches",
    "seasons",
    "divisions",
    "seasons_divisions",
    "discord_users",
    "players",
    "team_memberships",
    "roster_movements",
    "audit_logs",
}

# Tablas exclusivas de la web: el bot no las usa ni las modela, pero sin excluirlas el
# autogenerate propondría borrarlas al no encontrarlas en los metadatos.
WEB_ONLY_TABLES: set[str] = {
    "crystal_ball_league_campaigns",
    "crystal_ball_league_entries",
}

EXCLUDED_TABLES = SHARED_TABLES | WEB_ONLY_TABLES


def include_object(
    object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to,
) -> bool:
    """Excluye tablas gobernadas por RCL-Next y objetos asociados de Alembic."""
    if type_ == "table" and name in EXCLUDED_TABLES:
        return False
    if isinstance(object, sa.Table) and getattr(object, "name", None) in EXCLUDED_TABLES:
        return False
    table = getattr(object, "table", None)
    if table is not None:
        table_name = getattr(table, "name", None)
        if table_name in EXCLUDED_TABLES:
            return False
    return True
