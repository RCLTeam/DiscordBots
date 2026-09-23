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
│    Tablas Compartidas (6)    │    Tablas Propietarias (3)   │
│   Gobernanza: RCL-Next       │  Gobernanza: DiscordBots     │
│   Herramienta: Drizzle ORM   │     Herramienta: Alembic     │
├──────────────────────────────┼──────────────────────────────┤
│ • teams                      │ • matches                    │
│ • team_memberships           │ • ticket_notices             │
│ • discord_users              │ • role_requests              │
│ • players                    │                              │
│ • roster_movements           │                              │
│ • audit_logs                 │                              │
└──────────────────────────────┴──────────────────────────────┘
```

### 2.1 Tablas Compartidas (Gobernadas por RCL-Next)
- **Autoridad:** El esquema DDL, las migraciones e índices de las 6 tablas compartidas (`teams`, `team_memberships`, `discord_users`, `players`, `roster_movements`, `audit_logs`) son propiedad exclusiva de la aplicación web **RCL-Next**, gestionadas mediante **Drizzle ORM** (`drizzle-kit`).
- **Uso en DiscordBots:** Los modelos declarativos en `src/liga_bot/models/roster.py` replican exactamente estas tablas para posibilitar consultas ORM tipadas en tiempo de ejecución y la creación de esquemas efímeros en tests unitarios (`Base.metadata.create_all`).
- **Filtro de Migraciones en Alembic (`include_object`):** Para evitar que Alembic intente alterar o borrar estas tablas en producción, la función `include_object` en `alembic/env.py` intercepta el autogenerate e ignora de forma estricta cualquier tabla, índice o restricción perteneciente a este conjunto.

### 2.2 Tablas Propietarias (Gobernadas por DiscordBots)
- **Autoridad:** Las 3 tablas operativas propias del bot (`matches`, `ticket_notices`, `role_requests`) son gestionadas directamente por el pipeline de migraciones de **Alembic** en `alembic/versions/` (`001_initial_schema.py`, `002_role_requests.py`).
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
