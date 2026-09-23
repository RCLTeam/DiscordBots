# Comandos de Calendario y Seguimiento de Partidos (Schedule & Match Tracking)

El subsistema de comandos de calendario está implementado en `src/liga_bot/cogs/schedule.py` a través de la clase `ScheduleCog`. Este módulo centraliza las operaciones de creación de enfrentamientos deportivos, aprovisionamiento automático de canales privados de coordinación en Discord, asignación de permisos por rol y división, y procesamiento masivo de jornadas mediante archivos CSV.

---

## 1. Arquitectura y Ciclo de Vida (`ScheduleCog`)

La clase `ScheduleCog` extiende `discord.ext.commands.Cog` y administra los comandos de aplicación (`app_commands`) del bot para la gestión de calendario.

- **Ubicación principal**: `src/liga_bot/cogs/schedule.py:27-65`.
- **Compatibilidad legacy**: `src/liga_bot/cogs/schedule_cog.py:1-8` re-exporta `ScheduleCog` y la función `setup` para mantener compatibilidad con imports históricos.

### 1.1 Inyección de Dependencias Resiliente

El constructor de `ScheduleCog` admite la inyección opcional de `schedule_service`, `session_factory` y `settings`. Si no se proporcionan explícitamente durante la instanciación, se resuelven mediante propiedades con evaluación perezosa (*lazy properties*):

```python
@property
def session_factory(self) -> async_sessionmaker[AsyncSession]:
    if self._session_factory is not None:
        return self._session_factory
    bot_factory = getattr(self.bot, "session_factory", None)
    if bot_factory is not None:
        return bot_factory
    return get_session_factory()


@property
def schedule_service(self) -> ScheduleService:
    if self._schedule_service is not None:
        return self._schedule_service
    bot_svc = getattr(self.bot, "schedule_service", None)
    if bot_svc is not None:
        return bot_svc
    self._schedule_service = ScheduleService(
        session_factory=self.session_factory,
        settings=self.settings,
        bot=self.bot,
    )
    return self._schedule_service
```

### 1.2 Registro e Idempotencia en la Carga

La función `setup` (`src/liga_bot/cogs/schedule.py:307-311`) implementa una comprobación de existencia previa en el registro de cogs del bot:

```python
async def setup(bot: Bot | commands.Bot) -> None:
    if "Schedule" not in bot.cogs:
        await bot.add_cog(ScheduleCog(bot))
```

Esta comprobación previene excepciones de tipo `CommandRegistrationError` al recargar dinámicamente extensiones en caliente.

---

## 2. Control de Acceso y Autorización

Todos los comandos de calendario aplican un doble nivel de seguridad: restricción nativa en la API de Discord y verificación programática en runtime.

1. **Permiso nativo de Discord**: Todos los comandos cuentan con el decorador `@app_commands.default_permissions(manage_guild=True)`, ocultándolos por defecto a usuarios sin permisos de gestión en la interfaz del cliente.
2. **Validación programática de roles**: Antes de procesar cualquier comando, se invoca `is_authorized_scheduler(interaction, self.settings)` (`src/liga_bot/cogs/permissions.py:167-190`). Para superar esta validación, el invocador debe cumplir al menos una de las siguientes condiciones:
   - Poseer permiso de Administrador de Discord (`guild_permissions.administrator`).
   - Poseer el rol Staff (`settings.staff_role_id`).
   - Poseer el rol Admin (`settings.admin_role_id`).
   - Poseer el rol CEO Premier (`settings.ceo_premier_role_id`).
   - Poseer el rol CEO Ascend (`settings.ceo_ascend_role_id`).

Si el usuario no cuenta con la autorización requerida, la interacción responde inmediatamente con un mensaje efímero:  
`"❌ No tienes permisos para crear partidos (se requiere Staff, Admin o CEO)."`

---

## 3. Catálogo de Comandos Slash

### 3.1 `/crear-partido`

Crea el registro de un enfrentamiento individual en la base de datos, valida la integridad de los equipos y divisiones, aprovisiona la categoría y el canal de texto privado en Discord, y publica los mensajes oficiales de convocatoria y reglamento.

- **Ubicación**: `src/liga_bot/cogs/schedule.py:67-172`.
- **Firma**:
  ```python
  @app_commands.command(
      name="crear-partido",
      description="Crea el canal de un partido individual y postea los mensajes oficiales",
  )
  async def crear_partido(
      self,
      interaction: discord.Interaction,
      jornada: int,
      equipo1: str,
      equipo2: str,
      fecha: str | None = None,
      hora: str | None = None,
  ) -> None
  ```

#### Parámetros

| Parámetro | Tipo | Obligatorio | Descripción |
|---|---|:---:|---|
| `jornada` | `int` | Sí | Número de la jornada competitiva. Debe ser un entero positivo (`>= 1`). |
| `equipo1` | `str` | Sí | Nombre o tag del equipo local para resolución en base de datos. |
| `equipo2` | `str` | Sí | Nombre o tag del equipo visitante para resolución en base de datos. |
| `fecha` | `str \| None` | No | Fecha del partido en formato `DD/MM/YYYY`. |
| `hora` | `str \| None` | No | Hora del partido en formato `HH:MM`. |

#### Validaciones Pre-Ejecución y Parseo

1. **Rechazo de DMs**: Si `interaction.guild is None`, responde efímeramente `"❌ Este comando solo puede ser ejecutado dentro de un servidor de Discord."`.
2. **Autorización**: Ejecuta `is_authorized_scheduler`.
3. **Validación de Jornada**: Si `jornada < 1`, responde efímeramente `"❌ El número de jornada debe ser un entero positivo (>= 1)."`.
4. **Parseo de Fechas**: Si `fecha` y `hora` son proporcionados, se combinan y parsean mediante `datetime.strptime(f"{fecha_clean} {hora_clean}", "%d/%m/%Y %H:%M").replace(tzinfo=timezone.utc)`. Si ocurre un `ValueError`, se degrada silenciosamente a `scheduled_dt = None`, permitiendo aprovisionar el canal con un horario tentativo en texto libre.
5. **Aplazamiento de Interacción**: Ejecuta `await interaction.response.defer(ephemeral=True)` para evitar el timeout de 3 segundos de Discord ante operaciones de base de datos y creación de canales.

#### Respuestas Visuales (Embeds)

El comando responde mediante un mensaje de seguimiento efímero (`interaction.followup.send`) con uno de los siguientes tres estados:

- **Éxito (`result.success and result.channel is not None`)**:
  - **Color**: Verde (`discord.Color.green()`).
  - **Título**: `✅ Partido Creado — Jornada {jornada}`.
  - **Descripción**: `Enfrentamiento: **{result.team1_name}** VS **{result.team2_name}**`.
  - **Campos**:
    - `Canal de Coordinación`: Mención del canal de Discord creado (`result.channel.mention`).
    - `Horario Programado`: Cadena formateada en `%d/%m/%Y %H:%M UTC` (si el parseo fue exitoso).
    - `Horario Tentativo`: Cadena sin parsear con fecha y hora recibidas (si no se parseó a datetime formal).
    - `Enlace al Canal`: Enlace directo de salto al canal (`[Ir al canal]({jump_url})`).
- **Duplicado (`result.is_duplicate`)**:
  - **Color**: Oro (`discord.Color.gold()`).
  - **Título**: `⚠️ Partido Ya Existente — Jornada {jornada}`.
  - **Descripción**: Mensaje explicativo detallando que el enfrentamiento ya está registrado para esa jornada.
- **Fallo General (`result.success is False`)**:
  - **Color**: Rojo (`discord.Color.red()`).
  - **Título**: `❌ Error al Crear Partido — Jornada {jornada}`.
  - **Descripción**: Causa exacta del fallo (equipo no encontrado, conflicto de división, error de Discord API, etc.).

---

### 3.2 `/importar-jornada`

Procesa en lote la creación de múltiples enfrentamientos para una jornada completa a partir de un archivo CSV adjunto.

- **Ubicación**: `src/liga_bot/cogs/schedule.py:270-286` (implementación compartida en `_importar_jornada_impl:174-269`).
- **Firma**:
  ```python
  @app_commands.command(
      name="importar-jornada",
      description="Crea todos los canales de partido de una jornada a partir de un archivo CSV",
  )
  async def importar_jornada(
      self,
      interaction: discord.Interaction,
      jornada: int,
      archivo: discord.Attachment,
  ) -> None
  ```

#### Parámetros

| Parámetro | Tipo | Obligatorio | Descripción |
|---|---|:---:|---|
| `jornada` | `int` | Sí | Número de la jornada a la que pertenecerán los partidos importados. |
| `archivo` | `discord.Attachment` | Sí | Archivo con extensión `.csv` que contiene las columnas requeridas: `equipo1,equipo2,fecha,hora`. |

#### Validaciones del Archivo y Procesamiento

1. **Extensión del archivo**: Valida `archivo.filename.lower().endswith(".csv")`. Si no es un CSV, rechaza con `"❌ El archivo adjunto debe ser de tipo CSV (.csv)."`.
2. **Decodificación resiliente**: Ejecuta `raw_bytes.decode("utf-8-sig", errors="replace")` para eliminar automáticamente el Byte Order Mark (`\ufeff`) presente en archivos generados por Microsoft Excel.
3. **Delegación al servicio**: Invoca `await self.schedule_service.create_jornada_from_csv(guild=guild, jornada=jornada, csv_content=csv_text)`.

#### Respuestas Visuales y Truncado Defensivo

El resultado agregado de la importación se presenta mediante un embed estructurado:

- **Estados de Color y Título**:
  - **100% Exitoso (`success_count > 0 and error_count == 0`)**: Color verde (`discord.Color.green()`), título `✅ Jornada {jornada} Importada con Éxito`.
  - **Parcial (`success_count > 0 and error_count > 0`)**: Color naranja (`discord.Color.orange()`), título `⚠️ Jornada {jornada} Importada Parcialmente`.
  - **Fallo Total (`success_count == 0`)**: Color rojo (`discord.Color.red()`), título `❌ Error en la Importación — Jornada {jornada}`.
- **Campos del Embed**:
  - **`Resumen de Filas`**: Muestra total de filas procesadas, partidos creados y errores/omitidos.
  - **`Canales Aprovisionados`**: Lista de menciones a los canales de texto creados (`#j1-t1-vs-t2, ...`). Si la longitud de la cadena supera los 1020 caracteres, se aplica truncado defensivo a 1000 caracteres más la coletilla ` ... (truncado)` para respetar el límite de 1024 caracteres por campo en embeds de Discord.
  - **`Incidencias Reportadas`**: Lista de hasta 10 mensajes de error con formato `• Fila X: detalle`. Si existen más de 10 errores, se añade `*... y N errores adicionales.*`, aplicando igualmente el recorte defensivo a 1020 caracteres.

---

### 3.3 `/crear-jornada`

Alias funcional de `/importar-jornada` mantenido para preservar la ergonomía y compatibilidad con interfaces históricas de administración.

- **Ubicación**: `src/liga_bot/cogs/schedule.py:288-305`.
- **Firma**:
  ```python
  @app_commands.command(
      name="crear-jornada",
      description="Alias de /importar-jornada: crea canales de partido desde un archivo CSV",
  )
  async def crear_jornada(
      self,
      interaction: discord.Interaction,
      jornada: int,
      archivo: discord.Attachment,
  ) -> None:
      await self._importar_jornada_impl(interaction, jornada, archivo)
  ```
- **Comportamiento**: Invoca internamente `_importar_jornada_impl` con los mismos parámetros, controles de acceso y respuestas que `/importar-jornada`.

---

## 4. Aclaraciones Fácticas sobre Comandos Inexistentes

En versiones conceptuales o especificaciones preliminares se propusieron comandos de consulta y sincronización bajo el grupo `/horarios` (tales como `/horarios ver`, `/horarios actualizar` o `/horarios sync`).

Se aclara taxativamente que:
1. El grupo o comando `/horarios` **NO EXISTE** en el código fuente de `DiscordBots`.
2. Las únicas interfaces slash para programación y gestión de calendario disponibles en el sistema son `/crear-partido`, `/importar-jornada` y su alias `/crear-jornada`.
3. No existen comandos de consulta de calendario en Discord; la visualización de enfrentamientos se realiza directamente a través de los canales de texto aprovisionados bajo las categorías de jornada en el servidor de Discord.
