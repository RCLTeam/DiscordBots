# Servicios de Dominio: Vigilancia de Tickets y Ciclo de Vida de Canales

Este documento detalla la arquitectura y lógica operativa del servicio de dominio `TicketService`, responsable de la supervisión de canales de tickets, control de inactividad, despacho de avisos y coordinación con la base de datos relacional. Asimismo, se documentan las garantías anti-huérfanos y la normalización de canales en `DiscordBots`.

---

## 1. Arquitectura y Rol de `TicketService`

El servicio `TicketService` opera como centinela pasivo y activo del estado de los tickets de soporte.

- **Ubicación en código:** `src/liga_bot/services/ticket_service.py:97-381`
- **Principio de No Destrucción:**
  `TicketService` **NO** elimina, archiva ni cierra canales de tickets automáticamente bajo ninguna circunstancia. Su responsabilidad se limita a:
  1. Auditar los canales de texto ubicados en las categorías de tickets configuradas.
  2. Determinar el tiempo transcurrido desde el último mensaje relevante.
  3. Despachar una mención de advertencia al Staff si el canal permanece $\ge 24\text{h}$ sin respuesta oficial.
  4. Actualizar el estado en la base de datos relacional (`ticket_notices.is_pending_staff = True`).
  
  La resolución, confirmación y borrado final de los canales de tickets corresponde exclusivamente a la interacción de los usuarios y moderadores mediante las vistas interactivas (`src/liga_bot/ui/roles.py:336, 432`).

---

## 2. Tipos de Datos en Memoria y Estados de Auditoría

Para garantizar un seguimiento transparente y optimizado en memoria, el servicio define enumeraciones y dataclasses con `slots=True`:

### 2.1 Enumeración `ChannelAuditStatus` (`L35-44`)

| Estado | Significado Técnico | Acción en Base de Datos |
|---|---|---|
| `ALERT_SENT` | Inactividad $\ge 24\text{h}$ sin intervención de staff. Alerta enviada al canal de Discord. | Ejecuta `TicketNoticeRepository.record_alert(...)`. Marca `is_pending_staff = True`. |
| `SKIPPED_RECENT` | El último mensaje tiene una antigüedad inferior al umbral configurado ($< 24\text{h}$). | Sin modificaciones en base de datos. |
| `SKIPPED_STAFF` | El último mensaje fue emitido por un miembro de Staff, Admin u Organizador. | Ejecuta `record_staff_response(...)`. Resetea `is_pending_staff = False`. |
| `SKIPPED_ALREADY_ALERTED` | Ya se emitió un aviso en las últimas 24h, o el último mensaje del canal es el propio aviso del bot. | Sin modificaciones en base de datos (previene spam). |
| `SKIPPED_EMPTY` | El canal no contiene mensajes (`history(limit=1)` vacío). | Omitido. |
| `SKIPPED_FORBIDDEN` | El bot carece del permiso `read_message_history` en el canal. | Omitido con registro en log warning. |
| `SKIPPED_ERROR` | Error de transporte o HTTP al interactuar con el Gateway de Discord. | Omitido con registro en log error. |

### 2.2 Dataclasses de Auditoría (`L47-89`)

```python
@dataclass(slots=True)
class ChannelAuditDetail:
    channel_id: int
    channel_name: str
    category_name: str
    status: ChannelAuditStatus
    last_message_at: datetime | None = None
    author_id: int | None = None
    author_is_staff: bool = False
    detail: str = ""


@dataclass(slots=True)
class TicketAuditResult:
    categories_scanned: int = 0
    channels_scanned: int = 0
    alerts_sent: int = 0
    skipped_recent: int = 0
    skipped_staff: int = 0
    skipped_already_alerted: int = 0
    skipped_empty: int = 0
    skipped_forbidden: int = 0
    skipped_error: int = 0
    details: list[ChannelAuditDetail] = field(default_factory=list)

    def summary(self) -> str: ...
```

---

## 3. Algoritmo Quirúrgico de Auditoría (`audit_channel`)

El método `audit_channel(self, channel, category_name)` (`L156-296`) ejecuta una inspección en 7 etapas deterministas sobre cada canal individual:

```
[ Canal de Discord ]
        │
        ▼
1. Lectura de Historial (limit=1)
   ├── discord.Forbidden   ──► SKIPPED_FORBIDDEN
   ├── discord.HTTPException ─► SKIPPED_ERROR
   └── Sin mensajes         ──► SKIPPED_EMPTY
        │
        ▼
2. Prevención de Auto-Bucle
   └── ¿Último mensaje es del bot Y contiene DEFAULT_TICKET_AVISO_MARCADOR?
        ├── SÍ ───────────────► SKIPPED_ALREADY_ALERTED
        └── NO
             │
             ▼
3. Resolución de Autor y Roles
   └── ¿Autor posee rol en staff_role_ids?
        ├── SÍ ───────────────► Actualiza BD (is_pending_staff=False)
        │                       Retorna SKIPPED_STAFF
        └── NO
             │
             ▼
4. Evaluación del Delta Temporal
   └── ¿delta = (now - msg_time) < threshold (24h)?
        ├── SÍ ───────────────► SKIPPED_RECENT (Ticket en regla)
        └── NO
             │
             ▼
5. Supresión de Alertas Duplicadas (<24h)
   └── Consulta TicketNoticeRepository.get_by_channel_id(channel.id)
       ¿notice.last_alert_sent_at < 24h?
        ├── SÍ ───────────────► SKIPPED_ALREADY_ALERTED
        └── NO
             │
             ▼
6. Despacho de Mensaje a Discord (_send_alert_message)
        │
        ▼
7. Persistencia Atómica (record_alert con is_pending_staff=True)
   └── Retorna ALERT_SENT
```

### 3.1 Prevención de Auto-Bucle del Bot (`L206-220`)

Si un bot envía un aviso de inactividad a un canal, ese aviso se convierte en el último mensaje registrado en Discord. Para evitar que en la siguiente ejecución el bot interprete su propio aviso como un mensaje de usuario sin responder y genere una cascada infinita de alertas:

```python
if (
    self.bot
    and self.bot.user
    and mensaje.author.id == self.bot.user.id
    and DEFAULT_TICKET_AVISO_MARCADOR in mensaje.content
):
    return ChannelAuditDetail(
        channel_id=channel.id,
        channel_name=channel.name,
        category_name=cat_name,
        status=ChannelAuditStatus.SKIPPED_ALREADY_ALERTED,
        last_message_at=msg_time,
        author_id=mensaje.author.id,
        detail="Bot alert already is last message",
    )
```

Donde `DEFAULT_TICKET_AVISO_MARCADOR = "⚠️ TICKET_SIN_RESPUESTA"` (`src/liga_bot/config.py:33`).

### 3.2 Identificación de Intervención de Staff (`L117-154, L222-243`)

El método `is_staff_author` determina si el autor de un mensaje pertenece a los estamentos de gestión:
1. Reúne los identificadores de roles con privilegios (`staff_role_ids`, `L117-128`):
   - `settings.staff_role_id`
   - `settings.admin_role_id`
   - `settings.ceo_premier_role_id`
   - `settings.ceo_ascend_role_id`
   - `settings.organizador_role_id` (si está presente)
2. Descarta al bot propio (`author.id == self.bot.user.id -> False`).
3. Resuelve el objeto `discord.Member` mediante caché local o llamada API (`resolve_member`).
4. Si el autor posee alguno de los roles de staff:
   - Abre sesión transaccional: `async with transactional_session(self.session_factory) as session:`.
   - Invoca `await repo.record_staff_response(channel.id, response_time=msg_time)`.
   - Se actualiza `last_staff_message_at = msg_time` y se resetea `is_pending_staff = False`.
   - Retorna `ChannelAuditStatus.SKIPPED_STAFF`.

### 3.3 Supresión de Alertas Repetidas y Ventana de 24 Horas (`L257-274`)

Si el autor no es staff y el delta respecto a `msg_time` supera el umbral (`ticket_revision_hours`, por defecto 24 horas):
1. Consulta `TicketNoticeRepository.get_by_channel_id(channel.id)`.
2. Si existe un registro con `last_alert_sent_at` y `(now - last_alert_sent_at) < threshold`, el aviso se suprime y se devuelve `SKIPPED_ALREADY_ALERTED`.
3. Esto garantiza que un canal inactivo reciba como máximo una alerta por día natural, evitando el acoso por spam en canales desatendidos.

### 3.4 Despacho y Formato Canónico del Mensaje (`L297-321`)

Cuando procede enviar la alerta, el método `_send_alert_message` resuelve las menciones de los roles de administración en el servidor:
- Si los roles están presentes, construye menciones explícitas de Discord (`<@&role_id>`).
- Si ningún rol puede resolverse, utiliza el texto `@Staff`.
- Mensaje emitido al canal:
  ```text
  ⚠️ TICKET_SIN_RESPUESTA
  <@&staff_role_id> <@&admin_role_id> — Este ticket lleva más de 24h sin respuesta del staff.
  ```

Tras el envío exitoso, ejecuta `await repo.record_alert(...)` registrando la hora UTC actual y marcando `is_pending_staff = True`.

---

## 4. Normalización NFKD y Throttling contra Rate Limits

### 4.1 Normalización de Categorías con Unicode NFKD (`L92-94, L331-344`)

Discord permite que los administradores utilicen fuentes Unicode estilizadas en los nombres de categoría (por ejemplo: negritas matemáticas, caracteres góticos o acentos diacríticos). Para identificar las categorías sin falsos negativos:

```python
def normalize_category_name(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).lower()
```

El método `check_tickets` busca coincidencias contra un conjunto canónico predefinido (`src/liga_bot/config.py:126`):
- `"TICKETS-GENERAL-PREMIER"`
- `"TICKETS-GENERAL-ASCEND"`
- `"TICKETS-FICHAJES-PREMIER"`
- `"TICKETS-FICHAJES-ASCEND"`
- `"TICKETS-ADMINISTRACION"`
- Categoría personalizada en `settings.tickets_category_name`
- Nombre genérico `"tickets"`

### 4.2 Throttling Defensivo de 0.3 Segundos (`L109, L369-371`)

La API de Discord impone un límite de peticiones (*Rate Limit*) en rutas de lectura de canales de aproximadamente 5 peticiones por segundo por token de bot. Para prevenir el bloqueo con código de estado HTTP 429 (*Too Many Requests*):

- El constructor inicializa `throttle_delay: float = 0.3` (300 milisegundos).
- En el bucle de canales de `check_tickets`:
  ```python
  if self.throttle_delay > 0:
      await asyncio.sleep(self.throttle_delay)
  ```
- **Garantía Operativa:** Una pausa de 300 ms entre canales establece una tasa teórica máxima de ~3.3 peticiones por segundo, manteniéndose por debajo del umbral de saturación de Discord sin degradar significativamente la duración de la auditoría.

---

## 5. Tarea en Segundo Plano: `check_tickets_loop` en `TicketsCog`

El control periódico automatizado se implementa en `TicketsCog` (`src/liga_bot/cogs/tickets.py:68-115`):

- **Frecuencia:** `@tasks.loop(hours=24)`.
- **Sincronización Previa (`@check_tickets_loop.before_loop`, L103-106):**
  Ejecuta `await self.bot.wait_until_ready()`. Asegura que la auditoría no comience hasta que la conexión con el Gateway esté completamente establecida y las cachés de servidores y canales estén inicializadas.
- **Resiliencia y Auto-Reinicio (`@check_tickets_loop.error`, L108-115):**
  Captura cualquier excepción no controlada en el bucle, la registra con `logger.error(..., exc_info=True)` y comprueba el estado del loop. Si quedó inactivo (`not self.check_tickets_loop.is_running()`), ejecuta de inmediato `self.check_tickets_loop.restart()`, impidiendo que un fallo transitorio de red desactive la vigilancia indefinidamente.
- **Ciclo de Vida Limpio (`L221-229`):**
  Al descargar la extensión (`cog_unload`) o apagar el bot (`stop_loops`), se cancela la tarea con `self.check_tickets_loop.cancel()`.

---

## 6. Garantías Anti-Huérfanos y Ciclo de Vida de Canales

El aprovisionamiento de canales en Discord y la persistencia en bases de datos relacionales son operaciones desacopladas que carecen de transaccionalidad distribuida nativa (no existe Two-Phase Commit entre la API de Discord y PostgreSQL). Para evitar la proliferación de **canales huérfanos** (canales creados en Discord pero inexistentes en base de datos), el sistema implementa un patrón de rollback compensatorio:

### 6.1 Rollback en `RoleService.create_role_request_ticket` (`src/liga_bot/services/role_service.py:275-296`)

Cuando un usuario solicita un cambio de rol:
1. Se crea el canal privado en Discord (`await guild.create_text_channel(...)`).
2. Se intenta registrar la entidad `RoleRequest` en base de datos.
3. Si la base de datos lanza una excepción (caída del servidor PostgreSQL, violación de restricción, fallo de conexión):
   ```python
   except Exception as exc:
       logger.error("Error al registrar solicitud de rol en base de datos para canal %s: %s", channel.id, exc)
       try:
           await channel.delete(reason="Error al registrar solicitud de rol en base de datos")
       except Exception as del_exc:
           logger.warning("No se pudo eliminar canal huérfano %s: %s", channel.id, del_exc)
       return False, "Error al registrar la solicitud en base de datos.", None
   ```
4. El canal recién creado en Discord es eliminado de inmediato mediante `channel.delete()`.

### 6.2 Rollback en `ScheduleService.provision_match_channel` (`src/liga_bot/services/schedule_service.py:347-353`)

De forma homóloga, en la creación automatizada de canales para partidos de jornada:
```python
except Exception as exc:
    # Garantía Anti-Huérfanos: eliminación inmediata del canal
    if created_channel is not None:
        try:
            await created_channel.delete(reason="Rollback: error de aprovisionamiento")
        except Exception:
            pass
    logger.error("Error al aprovisionar partido para jornada %s: %s", jornada, exc, exc_info=True)
    return MatchResult(...)
```

### 6.3 Normalización de Nombres de Canal con `normalize_slug` (`src/liga_bot/utils/formatting.py:70-100`)

Para garantizar que los nombres de los canales creados sean siempre válidos según las especificaciones de Discord (máximo 100 caracteres, sin espacios ni caracteres especiales):

1. **NFKD y Descomposición:** Descompone acentos y glifos con `unicodedata.normalize("NFKD", text)`.
2. **Purga Diacrítica:** Filtra caracteres eliminando aquellos donde `unicodedata.combining(c)` sea verdadero.
3. **Conversión a Minúsculas:** Aplica `.lower()`.
4. **Sustitución Alfanumérica:** Reemplaza caracteres no alfanuméricos por guiones: `re.sub(r"[^a-z0-9]+", "-", lowered)`.
5. **Colapso de Guiones:** Elimina guiones redundantes y recorta extremos: `re.sub(r"-+", "-", slug).strip("-")`.
6. **Límite de Longitud:** Trunca a `max_length` caracteres (por defecto 100) limpiando posibles guiones terminales residuales (`slug[:max_length].rstrip("-")`).
