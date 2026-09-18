# Manual de Operaciones y Comandos — LigaBot

Este documento constituye el manual operativo para el equipo de **Staff**, **Administradores** y **CEOs de División** de la liga RCL (Rift Champions League). Detalla el funcionamiento, sintaxis, permisos requeridos, validaciones y respuestas esperadas de cada comando slash en Discord y de las herramientas administrativas de consola (CLI).

---

## 📑 Tabla de Contenidos

- [1. Matriz de Permisos y Roles](#1-matriz-de-permisos-y-roles)
- [2. Referencia de Comandos Slash](#2-referencia-de-comandos-slash)
  - [/registrar-equipo](#registrar-equipo)
  - [/equipos](#equipos)
  - [/crear-partido](#crear-partido)
  - [/importar-jornada (y alias /crear-jornada)](#importar-jornada-y-alias-crear-jornada)
  - [/revisar-tickets](#revisar-tickets)
  - [/sync (y alias /sincronizar)](#sync-y-alias-sincronizar)
- [3. Formato y Especificación de Archivos CSV](#3-formato-y-especificación-de-archivos-csv)
- [4. Tareas en Segundo Plano y Auditoría Automática](#4-tareas-en-segundo-plano-y-auditoría-automática)
- [5. Interfaz de Línea de Comandos (CLI Administrativo)](#5-interfaz-de-línea-de-comandos-cli-administrativo)

---

## 1. Matriz de Permisos y Roles

LigaBot gestiona el acceso a sus funciones mediante una jerarquía basada en los roles de Discord configurados en el entorno (`.env`):

| Rol / Función | Variable de Entorno | Permisos Asociados |
| :--- | :--- | :--- |
| **Administradores** | `ADMIN_ROLE_ID` | Acceso irrestricto a todos los comandos, incluyendo sincronización del árbol (`/sync`) y registro de equipos. |
| **Staff / Árbitros** | `STAFF_ROLE_ID` | Registro de equipos (`/registrar-equipo`), creación manual de partidos, importación de jornadas CSV y auditoría de tickets. |
| **CEO División Premier** | `CEO_PREMIER_ROLE_ID` | Creación e importación de jornadas de Premier, visibilidad y coordinación en canales de Premier. |
| **CEO División Ascenso** | `CEO_ASCEND_ROLE_ID` | Creación e importación de jornadas de Ascenso, visibilidad y coordinación en canales de Ascenso. |
| **Organizador General** | `ORGANIZADOR_ROLE_ID` | Rol auxiliar opcional exento de avisos en auditorías de tickets. |
| **Miembros de Equipo** | Asignado por `/registrar-equipo` | Acceso de lectura/escritura exclusivo en su canal de enfrentamiento asignado. |

---

## 2. Referencia de Comandos Slash

### `/registrar-equipo`

Registra un nuevo equipo participante en la base de datos o actualiza sus metadatos existentes de manera idempotente.

- **Permisos requeridos**: Rol de **Staff** (`STAFF_ROLE_ID`) o **Administrador** (`ADMIN_ROLE_ID`).
- **Visibilidad de respuesta**: Efímera (*ephemeral*, solo visible para quien ejecuta el comando).

#### Parámetros

| Parámetro | Tipo | Requerido | Descripción y Restricciones |
| :--- | :---: | :---: | :--- |
| `rol` | `Role` | Sí | Rol de Discord que identifica a los jugadores del equipo. |
| `nombre` | `String` | Sí | Nombre oficial del equipo. Máximo 100 caracteres. No puede estar vacío. |
| `tag` | `String` | Sí | Acrónimo del equipo. Entre 1 y 4 caracteres alfanuméricos (ej: `PSP`, `FNX`, `LOB`). |
| `division` | `Choice` | Sí | División competitiva: `Premier` (`PREMIER`) o `Ascenso` (`ASCEND`). |

#### Reglas de Validación
1. El comando debe ejecutarse obligatoriamente dentro del servidor de Discord (no en mensajes directos).
2. El `nombre` se trunca o rechaza si supera 100 caracteres.
3. El `tag` se limpia de espacios en blanco y no puede exceder los 4 caracteres.
4. Si el equipo ya existe (por ID de rol de Discord o por nombre), el registro se actualiza automáticamente con la nueva división, tag o slug sin crear duplicados.

#### Respuesta de Ejemplo
```
✅ Equipo Registrado con Éxito
• Nombre: Planar Shock Pingus
• Tag: `PSP`
• División: PREMIER
• Rol de Discord: @Planar Shock Pingus (`1547729760384319501`)
• Slug Canal: `planar-shock-pingus`
```

---

### `/equipos`

Muestra el listado de equipos actualmente registrados en la liga, organizados por división y con sus tags y roles asignados.

- **Permisos requeridos**: Todos los miembros del servidor.
- **Visibilidad de respuesta**: Pública (visible para todo el canal).

#### Parámetros

| Parámetro | Tipo | Requerido | Descripción |
| :--- | :---: | :---: | :--- |
| `division` | `Choice` | No | Filtro opcional: `Todas las divisiones` (`ALL`), `Premier` (`PREMIER`) o `Ascenso` (`ASCEND`). |

#### Respuesta de Ejemplo
```
🛡️ Equipos Registrados — Todas las Divisiones
Total de equipos: 8

División PREMIER (4):
• [PSP] Planar Shock Pingus — Rol: @Planar Shock Pingus (Canal: `planar-shock-pingus`)
• [FNX] Fnix Esports — Rol: @Fnix Esports (Canal: `fnix-esports`)
• [LOB] Lobos — Rol: @Lobos (Canal: `lobos`)
• [CRV] Cuervos — Rol: @Cuervos (Canal: `cuervos`)

División ASCEND (4):
• [DRG] Dragones — Rol: @Dragones (Canal: `dragones`)
• [FXA] Fenix Ascend — Rol: @Fenix Ascend (Canal: `fenix-ascend`)
• [KRK] Kraken Esports — Rol: @Kraken Esports (Canal: `kraken-esports`)
• [VIP] Viper Gaming — Rol: @Viper Gaming (Canal: `viper-gaming`)
```

---

### `/crear-partido`

Crea el canal de texto privado para un partido individual, configura sus permisos de acceso y publica de forma inmediata los mensajes oficiales de coordinación y draft.

- **Permisos requeridos**: **Staff**, **Administrador**, o **CEO** de la división respectiva.
- **Visibilidad de respuesta**: Efímera (*ephemeral*).

#### Parámetros

| Parámetro | Tipo | Requerido | Descripción |
| :--- | :---: | :---: | :--- |
| `jornada` | `Integer` | Sí | Número entero de la jornada (ej: `1`, `2`). |
| `equipo1` | `String` | Sí | Nombre, slug o tag del equipo local (ej: `PSP` o `Planar Shock Pingus`). |
| `equipo2` | `String` | Sí | Nombre, slug o tag del equipo visitante (ej: `FNX` o `Fnix Esports`). |
| `fecha` | `String` | No | Fecha pactada en formato `DD/MM/YYYY` (por defecto: `Por definir`). |
| `hora` | `String` | No | Hora pactada en formato `HH:MM` (por defecto: `Por definir`). |

#### Flujo Operativo y Reglas de Validación
1. **Validación de Equipos**: Ambos equipos deben existir en la base de datos.
2. **Validación de División**: Ambos equipos deben pertenecer a la misma división. Si se intenta emparejar un equipo de Premier con uno de Ascenso, la operación es rechazada con un mensaje de error explícito.
3. **Impedimento de Auto-Enfrentamiento**: `equipo1` y `equipo2` no pueden ser el mismo equipo.
4. **Idempotencia**: Si el partido ya fue creado previamente para esa jornada, se devuelve un aviso indicando que ya existe el canal sin duplicarlo.
5. **Aprovisionamiento del Canal**:
   - Categoría: Se ubica en `PREMIER - JORNADA {jornada}` o `ASCENSO - JORNADA {jornada}` (se crea automáticamente si no existe).
   - Nombre del canal: Formato normalizado `j{jornada}-{slug1}-vs-{slug2}` (ej: `j1-planar-shock-pingus-vs-fnix-esports`).
   - Permisos: `@everyone` sin acceso; roles de ambos equipos con acceso completo; Staff, Admin y CEO de la división con acceso de supervisión.
6. **Publicación de Mensajes Oficiales**:
   - **Mensaje 1**: Mención a los roles de ambos equipos, recordatorio de canal único de coordinación, plazo límite de horario (jueves 23:59h), plazo de alineaciones en OP.GG (4h antes) y tabla de penalizaciones por retraso (-1 BAN, 0 BANS, abandono).
   - **Mensaje 2**: Normativa de Fearless Draft (Draftcore), obligatoriedad de canales de voz y enlace al canal de reglamento oficial.
7. **Garantía Anti-Huérfanos**: Si el bot falla al enviar los mensajes iniciales o al registrar el partido en la base de datos, el canal recién creado se destruye inmediatamente para no dejar canales huérfanos sin persistencia.

---

### `/importar-jornada` (y alias `/crear-jornada`)

Procesa un archivo CSV con la programación completa de una jornada, aprovisionando todos los canales y partidos en un único lote transaccional.

- **Permisos requeridos**: **Staff**, **Administrador**, o **CEO** de la división respectiva.
- **Visibilidad de respuesta**: Efímera (*ephemeral*).

#### Parámetros

| Parámetro | Tipo | Requerido | Descripción |
| :--- | :---: | :---: | :--- |
| `jornada` | `Integer` | Sí | Número entero de la jornada a importar. |
| `archivo` | `Attachment` | Sí | Archivo adjunto con extensión `.csv`. |

#### Comportamiento
- Procesa el archivo línea por línea con validación estricta de encabezados.
- Admite archivos exportados desde Excel o Google Sheets con delimitadores de coma (`,`) o punto y coma (`;`).
- Si una fila contiene un error (ej: equipo inexistente o cruce de divisiones), el error se captura y se reporta en el resumen final sin abortar la creación del resto de partidos válidos.
- Emite un Embed con el desglose de filas procesadas, canales aprovisionados e incidencias detectadas (con truncamiento seguro si supera los límites de Discord).

---

### `/revisar-tickets`

Ejecuta una auditoría inmediata bajo demanda sobre las categorías de soporte técnico, fichajes y administración, localizando tickets que llevan más de 24 horas sin respuesta.

- **Permisos requeridos**: **Staff**, **Administrador**, o **CEO**.
- **Visibilidad de respuesta**: Efímera (*ephemeral*).

#### Comportamiento
1. Escanea todos los canales de texto de las categorías configuradas:
   - `TICKETS-GENERAL-PREMIER`
   - `TICKETS-GENERAL-ASCEND`
   - `TICKETS-FICHAJES-PREMIER`
   - `TICKETS-FICHAJES-ASCEND`
   - `TICKETS-ADMINISTRACION`
2. Si el último mensaje del canal fue enviado hace 24 horas o más por un usuario normal (no Staff, ni Admin, ni CEO, ni el bot), el canal se clasifica como inactivo.
3. Se verifica si el canal ya recibió una alerta en las últimas 24 horas en la tabla `ticket_notices` para no reenviar avisos innecesarios.
4. En los canales que requieren atención, se envía un mensaje de alerta oficial:
   ```
   ⚠️ TICKET_SIN_RESPUESTA: Este ticket lleva más de 24 horas sin respuesta del Staff.
   ```
5. Entre cada canal escaneado se introduce una pequeña pausa (`0.3s`) para evitar saturar el límite de peticiones de Discord.
6. El comando responde con un informe completo indicando canales revisados, alertas enviadas y canales descartados.

---

### `/sync` (y alias `/sincronizar`)

Sincroniza manualmente el árbol de comandos slash (`app_commands.CommandTree`) con la API de Discord.

- **Permisos requeridos**: Exclusivo para **Staff** (`STAFF_ROLE_ID`) o **Administradores** (`ADMIN_ROLE_ID`).
- **Visibilidad de respuesta**: Efímera (*ephemeral*).

#### Parámetros

| Parámetro | Tipo | Requerido | Descripción |
| :--- | :---: | :---: | :--- |
| `guild_id` | `String` | No | ID de un servidor específico a sincronizar (opcional). Si se omite, se usa el servidor actual o el configurado en `.env`. |
| `global_sync` | `Boolean` | No | Si es `True`, sincroniza globalmente para todos los servidores (por defecto: `False`). |

*Nota arquitectónica: LigaBot no sincroniza comandos automáticamente en `on_ready` para evitar bloqueos por rate-limiting en reconexiones.*

---

## 3. Formato y Especificación de Archivos CSV

Para el comando `/importar-jornada`, el archivo CSV debe respetar la siguiente estructura:

### Columnas Obligatorias y Opcionales
- `Jornada`: Número de la jornada (ej: `1`).
- `Equipo 1`: Nombre oficial o acrónimo del equipo local.
- `Equipo 2`: Nombre oficial o acrónimo del equipo visitante.
- `Fecha` *(opcional)*: Fecha sugerida en formato `DD/MM/YYYY`.
- `Hora` *(opcional)*: Hora sugerida en formato `HH:MM`.

### Ejemplo 1: Delimitado por comas (formato estándar)
```csv
Jornada,Equipo 1,Equipo 2,Fecha,Hora
1,Planar Shock Pingus,Fnix Esports,15/09/2026,21:00
1,Lobos,Cuervos,15/09/2026,22:00
```

### Ejemplo 2: Delimitado por punto y coma y usando acrónimos/tags (exportación Excel europea)
```csv
Jornada;Equipo 1;Equipo 2;Fecha;Hora
1;DRG;FXA;16/09/2026;20:00
1;KRK;VIP;16/09/2026;21:00
```

---

## 4. Tareas en Segundo Plano y Auditoría Automática

Además del comando manual `/revisar-tickets`, `TicketsCog` ejecuta una tarea periódica automatizada:

- **Frecuencia**: Se ejecuta automáticamente cada **24 horas** mediante `@tasks.loop(hours=24)`.
- **Autocorrección y Resiliencia**: Si ocurre una interrupción de red o fallo imprevisto, el decorador `@check_tickets_loop.error` intercepta la excepción y reinicia el bucle automáticamente (`check_tickets_loop.restart()`).
- **Parada Ordenada**: Al detener el bot (`LigaBot.close()`), todos los bucles en segundo plano se cancelan limpiamente para liberar memoria y sockets.

---

## 5. Interfaz de Línea de Comandos (CLI Administrativo)

LigaBot incluye una herramienta de administración en consola (`src/liga_bot/cli.py`) para precargar y sincronizar datos en masa sin necesidad de interactuar por Discord.

### Sintaxis General

```bash
uv run python -m liga_bot.cli [subcomando] [opciones]
# O mediante el binario registrado:
uv run liga-bot [subcomando] [opciones]
```

### Subcomando: `seed-teams`

Siembra los equipos de la liga en la base de datos de manera idempotente (crea los que no existen y actualiza los existentes si cambiaron de nombre o tag).

#### 1. Precarga de Equipos Canónicos
Por defecto, inserta los 8 equipos oficiales de la liga RCL:
- **Premier**: Planar Shock Pingus (`PSP`), Fnix Esports (`FNX`), Lobos (`LOB`), Cuervos (`CRV`).
- **Ascenso**: Dragones (`DRG`), Fenix Ascend (`FXA`), Kraken Esports (`KRK`), Viper Gaming (`VIP`).

```bash
uv run python -m liga_bot.cli seed-teams
```

#### 2. Filtrar por División
Permite sembrar únicamente los equipos de una división determinada:
```bash
uv run python -m liga_bot.cli seed-teams --division PREMIER
uv run python -m liga_bot.cli seed-teams --division ASCEND
```

#### 3. Cargar desde Archivo JSON Externo
Permite importar equipos definidos en un archivo JSON:
```bash
uv run python -m liga_bot.cli seed-teams --json-file ruta/a/equipos.json
```

Formato del archivo JSON:
```json
[
  {
    "name": "Planar Shock Pingus",
    "tag": "PSP",
    "division": "PREMIER",
    "discord_role_id": 1547729760384319501
  },
  {
    "name": "Fnix Esports",
    "tag": "FNX",
    "division": "PREMIER",
    "discord_role_id": 1547729760384319502
  }
]
```

#### 4. Cargar desde Archivo CSV Externo
```bash
uv run python -m liga_bot.cli seed-teams --csv-file ruta/a/equipos.csv
```

Formato del archivo CSV:
```csv
name,tag,division,discord_role_id
Planar Shock Pingus,PSP,PREMIER,1547729760384319501
Fnix Esports,FNX,PREMIER,1547729760384319502
```

#### 5. Limpiar y Resembrar (`--clear`)
Elimina todos los equipos registrados previamente antes de aplicar la siembra:
```bash
uv run python -m liga_bot.cli seed-teams --clear
```
