# Interfaz de Línea de Comandos (CLI) de Administración

[⬅️ Volver a Arquitectura](./README.md)

Este documento describe la arquitectura y el funcionamiento de la consola de administración CLI de `LigaBot`, implementada en `src/liga_bot/cli.py`. Detalla el motor de comandos basado en la librería estándar `argparse`, el subcomando de siembra `seed-teams`, los esquemas de ingestión de archivos JSON y CSV, el control transaccional atómico y la semántica de correspondencia idempotente.

---

## 1. Arquitectura del Motor CLI: Argparse vs Click

A diferencia de herramientas basadas en paquetes de terceros como Click, el CLI de `LigaBot` está implementado exclusivamente sobre la librería estándar `argparse` (`src/liga_bot/cli.py:11`).

### Rationale de Diseño
1. **Cero Dependencias Externas Adicionales:** Elimina dependencias pesadas en el entorno de ejecución, permitiendo que scripts de administración, contenedores CI y tareas de mantenimiento operen directamente con el intérprete estándar.
2. **Parsing Determinista:** La construcción del parser mediante `build_parser()` (`src/liga_bot/cli.py:367-418`) separa limpiamente el análisis sintáctico de la ejecución asíncrona.
3. **Desacoplamiento Asíncrono:** La función síncrona `main()` delega en `asyncio.run(run_seed_command(...))` (`src/liga_bot/cli.py:427-436`), facilitando que las operaciones de base de datos se ejecuten en un bucle asíncrono limpio con cierre garantizado de descriptores.

---

## 2. Definición de Comandos y Sintaxis (`seed-teams`)

El punto de entrada canónico para la administración está registrado como `liga-cli` en `pyproject.toml` (`[project.scripts] liga-cli = "liga_bot.cli:main"`).

El comando principal del CLI es `seed-teams`, diseñado para poblar y actualizar los equipos participantes en la base de datos:

```bash
# Invocación directa mediante el script canónico
uv run liga-cli seed-teams [opciones]

# Ayuda del CLI
uv run liga-cli --help

# O alternativamente mediante ejecución del módulo
python -m liga_bot.cli seed-teams [opciones]
```

### Opciones y Banderas Soportadas (`src/liga_bot/cli.py:376-417`)

| Bandera | Argumento Destino | Tipo | Valor por Defecto | Descripción |
|---|---|---|---|---|
| `-f`, `--file` | `file_path` | `str` | `None` | Ruta a un archivo JSON o CSV con los registros de equipos. |
| `--json-file` | `json_file` | `str` | `None` | Ruta explícita a un archivo JSON. |
| `--csv-file` | `csv_file` | `str` | `None` | Ruta explícita a un archivo CSV. |
| `-d`, `--division` | `division` | `str` | `None` | Filtra e inserta únicamente equipos de la división (`PREMIER` o `ASCEND`, case-insensitive). |
| `--clear` | `clear` | `bool` | `False` | Elimina todos los equipos existentes en la base de datos antes de sembrar. |

---

## 3. Ingestión y Validación de Archivos de Datos

La función `load_teams_from_file(file_path: Path) -> list[TeamSeedData]` (`src/liga_bot/cli.py:170-194`) procesa y valida los archivos de entrada según su extensión:

```
                      Archivo de Entrada (Path)
                                 |
           +---------------------+---------------------+
           |                                           |
           v                                           v
      Sufijo .json                                Sufijo .csv
           |                                           |
   Codificación UTF-8                         Codificación UTF-8-SIG
   Validación: raíz es list                  csv.DictReader (sin BOM)
           |                                           |
           +---------------------+---------------------+
                                 |
                                 v
                     _parse_team_dict(row, source)
                                 |
            +--------------------+--------------------+
            |                    |                    |
       Validación           Normalización        Validación
     Claves Requeridas       Tag & Slug        Division Enum
```

### 3.1 Esquema JSON
- El archivo debe estar codificado en UTF-8.
- La raíz del documento JSON **debe ser obligatoriamente una lista** (`list[dict]`). Si la raíz es un diccionario u otro tipo primitivo, lanza `ValueError("El archivo JSON debe contener una lista de objetos de equipos.")`.

```json
[
  {
    "name": "Vanguard Gaming",
    "tag": "VAN",
    "division": "PREMIER",
    "discord_role_id": 1547729760384319501
  },
  {
    "name": "Frostbite Esports",
    "tag": "FRO",
    "division": "ASCEND",
    "discord_role_id": 1548795784655405001
  }
]
```

### 3.2 Esquema CSV
- El archivo se lee con codificación `utf-8-sig` (`src/liga_bot/cli.py:186`). Esto neutraliza de forma automática la marca de orden de bytes (BOM) generada comúnmente por Microsoft Excel en Windows.
- La primera fila debe contener los encabezados exactos: `name,tag,division,discord_role_id`.

```csv
name,tag,division,discord_role_id
Vanguard Gaming,VAN,PREMIER,1547729760384319501
Frostbite Esports,FRO,ASCEND,1548795784655405001
```

### 3.3 Rechazo de Formatos No Soportados
Cualquier otra extensión (por ejemplo `.xml`, `.txt`, `.yaml`) es rechazada de inmediato con `ValueError(f"Formato no soportado '{suffix}'. Debe ser .json o .csv")` (`src/liga_bot/cli.py:190-191`). Si el archivo no existe en el sistema de archivos, lanza `FileNotFoundError`.

### 3.4 Validación de Registros Individuales (`_parse_team_dict`)
En `src/liga_bot/cli.py:196-223`, cada registro es validado con los siguientes criterios:
1. **Campos Requeridos:** `name`, `tag`, `division` y `discord_role_id`. Si falta alguna clave, lanza `ValueError(f"{source}: Falta la propiedad requerida '{err.args[0]}'.")`.
2. **Nombre No Vacío:** Si `name.strip()` está vacío, lanza `ValueError(f"{source}: El nombre del equipo no puede estar vacío.")`.
3. **Conversión Numérica de Rol:** `int(data["discord_role_id"])`. Si no es convertible a entero, lanza `ValueError(f"{source}: discord_role_id debe ser un entero válido.")`.
4. **Validación de División:** Transforma la cadena con `Division[div_str.strip().upper()]`. Si el valor no coincide con las claves del enum `Division` (`PREMIER`, `ASCEND`), captura `KeyError` y lanza `ValueError` enumerando las opciones legales.
5. **Normalización de Tag:** Aplica `normalize_tag(tag)` para convertir a mayúsculas y ajustar espacios.

---

## 4. Lógica de Siembra, Frontera Transaccional e Idempotencia

La función `seed_teams()` (`src/liga_bot/cli.py:232-302`) gestiona la inserción y actualización en base de datos:

### 4.1 Límite Transaccional Atómico
Todo el proceso de siembra se ejecuta dentro de un único bloque transaccional atómico:

```python
async with transactional_session(session_factory) as session:
    repo = TeamRepository(session)
    ...
```

- Si ocurre cualquier error imprevisto a mitad del sembrado, SQLAlchemy realiza un `ROLLBACK` completo de la transacción, evitando que la base de datos quede con equipos duplicados o a medio importar.
- Al salir exitosamente del bloque, se ejecuta un único `COMMIT` que consolida la totalidad del lote.

### 4.2 Purga Previa Opcional (`--clear`)
Si el argumento `clear_existing` es `True` (`src/liga_bot/cli.py:250-254`):
1. Obtiene la lista actual de todos los equipos: `all_existing = await repo.list_all()`.
2. Itera y elimina cada equipo mediante `await repo.delete(existing)`.
3. Registra en el log la cantidad de equipos eliminados antes de proceder a la inserción.

### 4.3 Algoritmo de Coincidencia Idempotente (Matching)
Para cada equipo en los datos de entrada, el sistema aplica una estrategia de búsqueda en dos niveles (`src/liga_bot/cli.py:264-268`):

1. **Búsqueda Primaria por Snowflake de Discord:**
   ```python
   existing = await repo.get_by_role_id(role_id)
   ```
2. **Fallback por Nombre Canónico:**
   ```python
   if existing is None:
       existing = await repo.get_by_name(name)
   ```

### 4.4 Evaluación de Diferencias (Delta Update)
- **Caso Creación (`existing is None`):**
  Invoca `await repo.create(name=name, tag=tag, slug=slug, division=division, discord_role_id=role_id)` e incrementa `stats["created"]`.
- **Caso Existente:**
  Compara los campos actuales del modelo con los nuevos datos:
  ```python
  if (
      existing.name != name
      or existing.tag != tag
      or existing.division != division
      or existing.discord_role_id != role_id
  ):
      await repo.update(
          existing,
          name=name,
          tag=tag,
          slug=slug,
          division=division,
          discord_role_id=role_id,
      )
      stats["updated"] += 1
  else:
      stats["skipped"] += 1
  ```
  Si los datos son exactamente iguales, **no se emite ninguna sentencia SQL `UPDATE` innecesaria**, contabilizándose como `skipped`.

---

## 5. Ejecutor Asíncrono y Bootstrap Automático (`run_seed_command`)

La función `run_seed_command()` (`src/liga_bot/cli.py:305-365`) prepara el entorno de ejecución:

### Bootstrap Automático de Tablas
Para permitir que el CLI funcione inmediatamente en bases de datos en memoria (`pglite:///:memory:`) o instancias locales nuevas sin requerir la ejecución manual previa de migraciones de Alembic, las líneas 329-331 generan el esquema relacional en caliente:

```python
async with target_engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
```

### Códigos de Salida y Limpieza de Recursos
- **Éxito (Código 0):** Imprime en salida estándar el resumen formateado:
  `✅ Sembrado finalizado con éxito: X creados, Y actualizados, Z sin cambios (Total procesados: N).` y retorna `0`.
- **Error (Código 1):** Registra el traceback completo con `logger.exception("Error al sembrar equipos: %s", err)`, imprime `❌ Error al sembrar equipos: {err}` en `sys.stderr` y retorna `1`.
- **Bloque `finally:`** Si el motor de base de datos fue instanciado localmente por el comando, invoca `await close_engine(target_engine)` para detener el proceso Node.js o cerrar el pool de conexiones de forma limpia.
