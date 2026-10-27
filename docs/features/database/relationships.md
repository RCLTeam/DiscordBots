# Relaciones, Cascadas y Restricciones de Integridad

[⬅️ Volver a Base de Datos](./README.md)

Este documento documenta la topología de relaciones entre los modelos relacionales de `DiscordBots`, las directivas de carga asíncrona, las políticas de borrado en cascada y las restricciones DDL de integridad referencial.

---

## 1. Topología del Grafo de Entidades

El siguiente esquema ilustra cómo interactúan las 9 entidades relacionales del sistema:

```
                  ┌──────────────┐
                  │  AuditLog    │
                  └──────┬───────┘
                         │ actor_discord_user_id (SET NULL)
                         ▼
┌──────────┐      ┌──────────────┐      ┌──────────────┐
│  Player  ├─────►│ DiscordUser  │◄─────┤ TeamMember-  │
└────▲─────┘      └──────┬───────┘      │    ship      │
     │ discord_user_id   │              └──────┬───────┘
     │ (SET NULL)        │                     │
     │                   │                     │ team_id
     │ mvp_player_id     ▼                     │ (CASCADE)
     │ (SET NULL) ┌──────────────┐             ▼
     │            │RosterMovement│◄─────┌──────────────┐
     │            └──────────────┘      │     Team     │
     │             actor_id (SET NULL)  └──────┬───────┘
     │             user_id (CASCADE)           │
     │                                         │ team1_id / team2_id
     │                                         │ (CASCADE)
     │            ┌──────────────┐             ▼
     │            │ TicketNotice │      ┌──────────────┐
     └────────────┼─────────────────────┤    Match     │
                  └──────────────┘      └──────────────┘
┌──────────────┐
│ RoleRequest  │
└──────────────┘
```

- **`DiscordUser` como Nodo Central:** Agrupa cuentas de Riot (`Player`), membresías de equipo (`TeamMembership`), movimientos históricos (`RosterMovement`) y trazas de auditoría (`AuditLog`).
- **`Team` como Núcleo Deportivo:** Vincula la plantilla activa (`TeamMembership`), el historial del club (`RosterMovement`) y los partidos programados (`Match`). A su vez, `Match` puede referenciar la cuenta de Riot del jugador MVP de la serie (`mvp_player_id -> Player.id`, `ON DELETE SET NULL`).
- **Entidades de Operación Directa:** `RoleRequest` y `TicketNotice` gestionan flujos operativos y de auditoría de canales en Discord, utilizando identificadores Snowflake de 64 bits (`BigInteger`) para su enlace funcional sin depender de claves foráneas rígidas.

---

## 2. Matriz Exhaustiva de Claves Foráneas y Cascadas DDL

| Tabla Origen | Columna FK | Tabla Destino | Regla DDL `ondelete` | Configuración ORM | Justificación Técnica |
|---|---|---|---|---|---|
| `matches` | `id_season_division` | `seasons_divisions.id` | `CASCADE` | `Match.season_division`: `foreign_keys=[id_season_division], lazy="joined"` (`fk_matches_id_season_division`) | Vincula el partido a la división y temporada en la que se disputa. |
| `matches` | `team1_id` | `teams.id` | `CASCADE` | `Team.home_matches`: `passive_deletes=True, lazy="select"`; `Match.team1`: `lazy="selectin"` | Si un equipo se da de baja, sus partidos como local se eliminan a nivel DDL. |
| `matches` | `team2_id` | `teams.id` | `CASCADE` | `Team.away_matches`: `passive_deletes=True, lazy="select"`; `Match.team2`: `lazy="selectin"` | Si un equipo se da de baja, sus partidos como visitante se eliminan a nivel DDL. |
| `matches` | `mvp_player_id` | `players.id` | `SET NULL` | Columna declarativa `Match.mvp_player_id` (`matches_mvp_player_id_fkey`) | Si se elimina la cuenta `Player`, el partido conserva su resultado con `mvp_player_id = NULL`. |
| `team_memberships` | `team_id` | `teams.id` | `CASCADE` | `Team.memberships`: `cascade="all, delete-orphan", passive_deletes=True` | Al suprimir un club, su plantilla queda purgada de forma atómica. |
| `team_memberships` | `discord_user_id` | `discord_users.discord_id` | `CASCADE` | `DiscordUser.memberships`: `cascade="all, delete-orphan", passive_deletes=True` | Si un usuario elimina su cuenta, sus cargos en clubes se suprimen. |
| `teams` | `(season_division_id, season_name)` | `seasons_divisions(id, season_name)` | `CASCADE` (`ON UPDATE CASCADE`) | Relación `Team.season_division` con `foreign_keys=[season_division_id]` para desambiguar | Ata el equipo a la temporada de su división: ninguna fila puede declarar una temporada distinta de la suya. El `ON UPDATE` propaga el renombrado de la temporada. |
| `team_memberships` | `(team_id, season_name)` | `teams(id, season_name)` | `CASCADE` (`ON UPDATE CASCADE`) | Relaciones `TeamMembership.team` y `Team.memberships` con `foreign_keys=[team_id]` | La membresía hereda la temporada de su equipo, que es la columna sobre la que se evalúa la exclusividad competitiva. |
| `players` | `discord_user_id` | `discord_users.discord_id` | `SET NULL` | `Player.discord_user`: `back_populates="players"` | Si el usuario de Discord se elimina, la cuenta Riot (`game_name#tag`) permanece en base de datos desvinculada (`NULL`) para conservar estadísticas. |
| `roster_movements` | `team_id` | `teams.id` | `CASCADE` | `Team.movements`: `cascade="all, delete-orphan", passive_deletes=True` | Historial purgado si el club es eliminado formalmente. |
| `roster_movements` | `discord_user_id` | `discord_users.discord_id` | `CASCADE` | `DiscordUser.movements`: `passive_deletes=True` | Movimientos del jugador purgados al eliminar su cuenta. |
| `roster_movements` | `actor_id` | `discord_users.discord_id` | `SET NULL` | `RosterMovement.actor`: `foreign_keys=[actor_id]` | Si el moderador o capitán que ejecutó el cambio es eliminado, el registro del movimiento preserva su validez (`actor_id = NULL`). |
| `audit_logs` | `actor_discord_user_id` | `discord_users.discord_id` | `SET NULL` | `AuditLog.actor`: `foreign_keys=[actor_discord_user_id], passive_deletes=True` | Preserva el historial transaccional de auditoría aunque el actor ya no exista en la plataforma. |

> `seasons_divisions.season_name` → `seasons.name` solo tiene `ON DELETE CASCADE`, **sin** `ON UPDATE`: renombrar una temporada que ya tenga divisiones falla en producción. La cascada documentada arriba aplica a partir de `seasons_divisions`, no desde `seasons`.

---

## 3. Directivas de Persistencia Asíncrona en ORM

### 3.1 Integridad Pasiva (`passive_deletes=True`)
Por defecto, cuando SQLAlchemy detecta una relación 1:N o 1:1 y se emite la eliminación de la entidad padre, el ORM intenta realizar un `SELECT` para cargar todos los hijos en memoria y eliminarlos fila a fila mediante sentencias `DELETE` individuales.

En una arquitectura asíncrona respaldada por PostgreSQL con reglas `ON DELETE CASCADE` a nivel DDL, este comportamiento genera:
1. Tráfico innecesario de consultas en la red.
2. Posibles fallos `MissingGreenlet` si los hijos no fueron precargados en la sesión activa.

**Solución Implementada:** Se declara `passive_deletes=True` en todas las relaciones dependientes. Esto le indica a SQLAlchemy que no emita consultas `SELECT` previas ni borrados manuales de hijos, dejando que el motor relacional de PostgreSQL ejecute las cascadas y asignaciones a `NULL` de manera nativa e instantánea en el servidor.

### 3.2 Carga Eager Asíncrona Segura (`lazy="selectin"`)
El acceso a relaciones mediante carga diferida síncrona tradicional (`lazy="select"`) sin precarga explícita está prohibido en SQLAlchemy asíncrono, ya que intentar acceder a una propiedad no cargada fuera de un bloque `await` genera:
```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; can't call await_only()
```

Para solventarlo, las relaciones críticas de lectura frecuente en `Match` (como `Match.team1`, `Match.team2`, `Match.casters` y `Match.caster_cards`, además de `Match.season_division` y `Team.season_division` con `lazy="joined"`) están configuradas con `lazy="selectin"`:
- **Mecánica:** Al consultar partidos, SQLAlchemy ejecuta una segunda consulta optimizada utilizando la cláusula `WHERE id IN (...)` para traer todas las entidades relacionadas en una sola operación asíncrona.
- **Ventaja frente a `joinedload` en colecciones:** Evita la duplicación cartesiana de columnas y filas que penaliza el rendimiento en joins relacionales complejos.

### 3.3 Acoplamiento Bidireccional Dinámico
Para prevenir ciclos de importación circular entre `src/liga_bot/models/team.py` y `src/liga_bot/models/roster.py`, las relaciones de `Team` con `TeamMembership` y `RosterMovement` se inyectan dinámicamente al finalizar la carga de `roster.py`:

```python
# src/liga_bot/models/roster.py:517-529
Team.memberships = relationship(
    "TeamMembership",
    back_populates="team",
    cascade="all, delete-orphan",
    passive_deletes=True,
    foreign_keys="[TeamMembership.team_id]",
)
Team.movements = relationship(
    "RosterMovement",
    back_populates="team",
    cascade="all, delete-orphan",
    passive_deletes=True,
)
```

---

## 4. Restricciones DDL e Índices de Integridad Avanzada

### 4.1 Restricción de Verificación de Capitanía
*Nombre DDL:* `team_memberships_captain_role_check`  
*Tabla:* `team_memberships`  
*Definición:*
```sql
CHECK (is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support'))
```
- **Propósito:** Garantiza a nivel de base de datos que ninguna posición no titular (`substitute`, `coach`, `staff`, `partners`) pueda ostentar la capitanía oficial de un equipo. Cualquier intento de persistir `is_captain = true` con un rol fuera de las 5 posiciones titulares resulta en un `IntegrityError` (`CheckViolation`).

### 4.2 Índice Único Parcial de Capitanía Exclusiva
*Nombre DDL:* `team_memberships_unique_captain`  
*Tabla:* `team_memberships`  
*Definición:*
```sql
CREATE UNIQUE INDEX team_memberships_unique_captain
ON team_memberships (team_id)
WHERE is_captain = true;
```
- **Propósito:** Implementa la regla deportiva de que un equipo puede tener **exactamente 0 o 1 capitán activo**. PostgreSQL valida la unicidad de `team_id` únicamente sobre las filas que cumplan la condición `is_captain = true`. Para transferir la capitanía a otro miembro, el servicio debe realizar la degradación del capitán actual y la promoción del nuevo dentro de la misma transacción atómica.

### 4.3 Restricción de Equipos Distintos en Partidos
*Nombre DDL:* `ck_matches_distinct_teams`  
*Tabla:* `matches`  
*Definición:*
```sql
CHECK (team1_id != team2_id)
```
- **Propósito:** Previene inconsistencias lógicas en el calendario deportivo, impidiendo que un club pueda figurar como local y visitante en el mismo partido.

### 4.4 Restricción de Enfrentamiento Único por Jornada
*Nombre DDL:* `uq_matches_jornada_teams`  
*Tabla:* `matches`  
*Definición:*
```sql
UNIQUE (jornada, team1_id, team2_id)
```
- **Propósito:** Asegura que dos equipos no puedan ser programados más de una vez en el mismo cruce dentro de una jornada determinada.

### 4.5 Unicidad Compuesta de Cuentas de Juego
*Nombre DDL:* `players_game_name_riot_tag_key`  
*Tabla:* `players`  
*Definición:*
```sql
UNIQUE (game_name, riot_tag)
```
- **Propósito:** Impide que una misma cuenta del cliente de Riot Games (`Nombre#TAG`) sea dada de alta duplicada en la base de datos.

### 4.6 Restricción de Puntuación MVP No Negativa
*Nombre DDL:* `matches_mvp_score_nonnegative_check`  
*Tabla:* `matches`  
*Definición:*
```sql
CHECK (mvp_score IS NULL OR mvp_score >= 0)
```
- **Propósito:** Impide almacenar puntuaciones de MVP negativas en un enfrentamiento (declarada con `conv("matches_mvp_score_nonnegative_check")` para evitar el prefijo automático `ck_matches_` de `NAMING_CONVENTION` y coincidir con la migración `0003_match_mvp_checks.sql` de RCL-Next).

### 4.7 Restricción de Coherencia entre Jugador MVP y Puntuación
*Nombre DDL:* `matches_mvp_player_score_check`  
*Tabla:* `matches`  
*Definición:*
```sql
CHECK (mvp_player_id IS NULL OR mvp_score IS NOT NULL)
```
- **Propósito:** Garantiza que ningún partido tenga un jugador MVP asignado (`mvp_player_id IS NOT NULL`) sin su correspondiente puntuación (`mvp_score IS NOT NULL`), permitiendo a la inversa que si el jugador se elimina (`ON DELETE SET NULL`), la puntuación histórica pueda conservarse con `mvp_player_id = NULL`.

### 4.8 Índice Parcial de Ranking MVP por Jornada
*Nombre DDL:* `matches_round_mvp_score_idx`  
*Tabla:* `matches`  
*Definición:*
```sql
CREATE INDEX matches_round_mvp_score_idx
ON matches USING btree (id_season_division, id_round, mvp_score DESC NULLS LAST)
WHERE status = 'completed' AND mvp_player_id IS NOT NULL;
```
- **Propósito:** Optimiza las consultas de clasificación y ranking de MVP por división de temporada y jornada sobre los partidos ya finalizados (`status = 'completed'`) que cuentan con un MVP asignado.
