# Base de Datos y Modelos Relacionales

[⬅️ Volver a Características](../README.md) | [Siguiente: WebSocket Bridge ➡️](../websocket-bridge/README.md)

---

## 1. Resumen Ejecutivo

El subsistema de base de datos y modelos relacionales de `DiscordBots` proporciona la capa de persistencia asíncrona de alto rendimiento para todas las operaciones del bot de Discord y su integración con el ecosistema competitivo de la liga.

Construido sobre **SQLAlchemy 2.0** con soporte asíncrono nativo (`AsyncSession`, `asyncpg`), este módulo gestiona la identidad de los usuarios, las cuentas de League of Legends, las plantillas de los clubes, el calendario de partidos, las solicitudes de roles y las trazas transaccionales de auditoría, garantizando integridad referencial estricta y aislamiento concurrente sin bloqueos mutuos.

---

## 2. Arquitectura de Doble Gobernanza de Datos

La base de datos PostgreSQL de la plataforma opera bajo un modelo de gobernanza híbrido y explícito que delimita con claridad la autoridad sobre el esquema relacional:

```
┌─────────────────────────────────────────────────────────────┐
│                 Base de Datos PostgreSQL                    │
├──────────────────────────────┬──────────────────────────────┤
│   Tablas Compartidas (10)    │    Tablas Propietarias (4)   │
│   Gobernanza: RCL-Next       │  Gobernanza: DiscordBots     │
│   Herramienta: Drizzle ORM   │     Herramienta: Alembic     │
├──────────────────────────────┼──────────────────────────────┤
│ • seasons                    │ • ticket_notices             │
│ • divisions                  │ • role_requests              │
│ • seasons_divisions          │ • match_casters              │
│ • discord_users              │ • match_caster_cards         │
│ • players                    │                              │
│ • teams                      │                              │
│ • team_memberships           │                              │
│ • roster_movements           │                              │
│ • audit_logs                 │                              │
│ • matches                    │                              │
└──────────────────────────────┴──────────────────────────────┘
```

### 2.1 Tablas Compartidas (Gobernadas por RCL-Next)
- **Autoridad:** El esquema DDL, las migraciones e índices de las tablas compartidas (`seasons`, `divisions`, `seasons_divisions`, `teams`, `matches`, `team_memberships`, `discord_users`, `players`, `roster_movements`, `audit_logs`; conjunto `SHARED_TABLES` de `src/liga_bot/models/alembic_filter.py`) son propiedad exclusiva de la aplicación web **RCL-Next**, gestionadas mediante **Drizzle ORM** (`drizzle-kit`).
- **Uso en DiscordBots:** Los modelos declarativos (`src/liga_bot/models/roster.py`, `team.py` y `match.py`) describen estas tablas con las mismas columnas, tipos y nulabilidad que la migración `0000_initial_shared_tables.py` (que las crea en las bases de datos limpias locales y de tests) y las migraciones defensivas `0002_match_round.py` y `0004_match_mvp_and_editor_role.py` (que aseguran de forma idempotente `id_round`, `mvp_player_id`, `mvp_score`, sus restricciones/índice parcial y el rol `'editor'` en `app_role`). `tests/test_shared_schema_alignment.py` compara cada modelo con la tabla migrada y verifica la coexistencia de migraciones; ver [alineación con las tablas compartidas](./models.md#14-alineación-con-las-tablas-compartidas).
- **Filtro de Migraciones en Alembic (`include_object`):** Para evitar que Alembic intente alterar o borrar estas tablas en producción, la función `include_object` de `src/liga_bot/models/alembic_filter.py`, que usa `alembic/env.py`, intercepta el autogenerate e ignora de forma estricta cualquier tabla, índice o restricción perteneciente a este conjunto.

### 2.2 Tablas Exclusivas de la Web
- **Tablas:** `crystal_ball_league_campaigns` y `crystal_ball_league_entries` (conjunto `WEB_ONLY_TABLES`), creadas por `0005_crystal_ball.sql` de RCL-Next.
- **Uso en DiscordBots:** El bot no las lee ni las escribe, por lo que no tienen modelo ni migración de Alembic. `include_object` las excluye porque, sin modelo, el autogenerate propondría borrarlas. Otras tablas exclusivas de la web anteriores a estas, como `rounds` o `predictions`, no están en el conjunto: una base con el esquema web completo las mostraría como borrables en el autogenerate.
- **Cascadas:** Referencian tablas que usa el bot. Borrar una fila de `seasons_divisions` elimina su campaña y sus boletos; borrar un `discord_users` elimina sus boletos y deja `resolved_by` en `NULL`.

### 2.3 Tablas Propietarias (Gobernadas por DiscordBots)
- **Autoridad:** Las tablas operativas propias del bot (`ticket_notices`, `role_requests`, `match_casters`, `match_caster_cards`) son gestionadas directamente por el pipeline de migraciones de **Alembic** en `alembic/versions/` (`0001_bot_tables.py`, `0003_match_casters.py`).
- Cualquier modificación estructural en estas entidades requiere la generación y aplicación de una migración versionada de Alembic.

---

## 3. Estructura y Tabla de Contenidos

La documentación de este módulo se descompone en los siguientes documentos atómicos de alta cohesión:

| Documento | Descripción |
|---|---|
| [**`models.md`**](./models.md) | Catálogo completo de los 9 modelos declarativos SQLAlchemy 2.0, columnas, tipos de datos, claves primarias y foráneas. |
| [**`enums.md`**](./enums.md) | Enumeraciones de dominio (Division, MatchStatus, RoleRequestStatus, AppRole, RosterRole, RosterMovementAction) y reglas de negocio asociadas. |
| [**`relationships.md`**](./relationships.md) | Grafo de relaciones, políticas de cascada (ON DELETE CASCADE / SET NULL, passive_deletes=True), carga asíncrona segura (`selectin`) y restricciones DDL avanzadas. |
| [**`persistence.md`**](./persistence.md) | Patrón repositorio base genérico (`BaseRepository[ModelT]`), detección temprana de PK UUID, validación defensiva en espacio de usuario contra DataError y semántica transaccional estricta. |
