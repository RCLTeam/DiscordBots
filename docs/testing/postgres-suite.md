# Suite de integración contra PostgreSQL real

[⬅️ Volver a Testing](README.md) | [⬅️ Volver a Documentación](../README.md)

---

## 1. Por qué existe

La suite habitual corre sobre **PGlite**, que no empaqueta la extensión `btree_gist`
(ver [límites del entorno](environment-limits.md)). Sin esa extensión no se puede crear
`one_competitive_team_per_player_and_season`, la restricción de exclusión que la base de
datos compartida añadió en `0001_competitive_membership_per_season.sql`:

```sql
EXCLUDE USING gist ("season_name" WITH =, "discord_user_id" WITH =)
    WHERE ("role" IN ('top', 'jungle', 'mid', 'adc', 'support', 'substitute'))
```

Esa restricción, junto con las claves foráneas compuestas, es la que provocó el fallo del
[issue #63](https://github.com/RCLTeam/DiscordBots/issues/63): `/traspasa-equipo` abortaba
con `NotNullViolationError` sobre `season_name`. Una suite verde en PGlite **no** demuestra
que el bot respete el esquema de producción, así que estos casos se ejecutan aparte contra
un PostgreSQL real.

Fichero: `tests/test_postgres_competitive_season.py`. Marcador: `@pytest.mark.postgres`.

## 2. Qué cubre

| Caso | Qué comprueba |
|---|---|
| `test_transfer_player_persiste_la_temporada` | El `INSERT` de `/traspasa-equipo` lleva `season_name`: es el error exacto del issue. |
| `test_traspaso_en_la_misma_temporada_libera_la_plaza` | Un traspaso dentro de la temporada libera la plaza competitiva previa **antes** de insertar, sin `ExclusionViolation`. |
| `test_la_plaza_de_otra_temporada_no_se_toca` | Una posición competitiva de otra temporada se conserva. |
| `test_cambio_de_posicion_falla_en_la_aplicacion_no_en_la_base_de_datos` | El conflicto competitivo sale como `CompetitivePositionConflictError`, no como error de base de datos. |
| `test_temporada_vigente_por_fechas` | La temporada vigente se deduce de `starts_on`/`ends_on`, no de una columna inexistente. |

## 3. Cómo levantarla

**Nunca** apuntes la variable a la base de datos compartida ni a producción: la suite
ejecuta `DROP SCHEMA public CASCADE` antes de aplicar las migraciones.

### Con Docker

```bash
docker run --rm -d --name ligabot-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ligabot_test \
  -p 55432:5432 postgres:16

export LIGA_BOT_POSTGRES_DSN="postgresql+asyncpg://postgres:postgres@localhost:55432/ligabot_test"
uv run pytest tests/test_postgres_competitive_season.py -v

docker rm -f ligabot-pg
```

### Con un PostgreSQL local

Basta una base de datos vacía y un usuario con permisos para crear extensiones
(`CREATE EXTENSION btree_gist` requiere ser superusuario o tener el rol adecuado):

```bash
createdb ligabot_test
export LIGA_BOT_POSTGRES_DSN="postgresql+asyncpg://usuario:clave@localhost:5432/ligabot_test"
uv run pytest -m postgres -v
```

Sin `LIGA_BOT_POSTGRES_DSN` definida, los cinco tests se omiten (`skipped`) y el resto de
la suite no se ve afectada.

## 4. Cómo prepara el esquema

1. `DROP SCHEMA public CASCADE` y `CREATE SCHEMA public`, con un driver **síncrono**
   (`psycopg`): Alembic es síncrono y así el esquema no queda atado a ningún bucle de
   eventos, que es lo que haría fallar a un motor asíncrono compartido entre tests.
2. `alembic upgrade head`.
3. Las dos sentencias que PGlite no admite: `CREATE EXTENSION IF NOT EXISTS btree_gist` y
   el `ALTER TABLE ... ADD CONSTRAINT ... EXCLUDE USING gist`. Van sueltas porque asyncpg
   no prepara varias sentencias en una misma llamada.

Cada test abre su propio motor asíncrono y una sesión con `rollback` al terminar, así que
no se pisan entre ellos.
