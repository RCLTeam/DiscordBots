# Seguridad e Invariantes de Protección en WebSocket Bridge

Este documento detalla las medidas de seguridad perimetral, las invariantes criptográficas, la validación estricta de identificadores y las políticas de desconexión aplicadas en el servicio `WebsocketBridgeService` de **LigaBot**.

---

## 1. Validación y Normalización de UUID (RFC 4122)

Cada mensaje entrante procesado por el puente WebSocket requiere un identificador de correlación único que cumpla estrictamente con el estándar **RFC 4122**. La extracción y verificación se implementa en `extract_request_id` (`src/liga_bot/services/bridge_protocol.py:10-50`).

### Invariantes de Validación

1. **Aislamiento de tipos:** El identificador extraído (`data.content.id` o `data.id`) debe ser estrictamente una cadena de texto (`isinstance(raw_id, str)`). Cualquier tipo numérico, booleano, lista u objeto anidado es rechazado inmediatamente devolviendo `None`.
2. **Sanitización de espacios en blanco:** Se aplica `raw_id.strip()`. Si la cadena resultante está vacía, se descarta sin evaluar.
3. **Compatibilidad de formatos:** Se pasa la cadena a `uuid.UUID(cleaned_id)`, lo que valida y permite:
   - Formatos estándar de 36 caracteres con guiones (ej. `123e4567-e89b-12d3-a456-426614174000`).
   - Formatos continuos de 32 caracteres hexadecimales sin guiones (ej. `123e4567e89b12d3a456426614174000`).
   - Caracteres en mayúsculas o minúsculas.
4. **Normalización canónica determinista:** La salida se obtiene mediante `str(parsed_uuid)`, garantizando que el identificador retornado esté siempre normalizado al formato **8-4-4-4-12 en minúsculas con guiones**.

### Resistencia Frente a Degradación de Esquema

Si el cliente envía un diccionario de contenido con un campo `"id"` corrupto o no válido:

```json
{
  "type": "LOGIN",
  "data": {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "content": {
      "id": "valor-invalido-no-uuid",
      "token": "secret"
    }
  }
}
```

La regla de precedencia en `bridge_protocol.py:33` establece que al existir `content["id"]`, este toma precedencia absoluta sobre `data["id"]`. Dado que `"valor-invalido-no-uuid"` no es un UUID válido, la función **no realiza fallback** a `data["id"]` y devuelve `None`. Esto previene ataques de confusión de campos o envenenamiento de identificadores.

---

## 2. Descarte Silencioso Pre-Autenticación (*Zero Reconnaissance Leakage*)

Para mitigar ataques de reconocimiento de puertos, escaneo de vulnerabilidades y ataques de diccionario (*fuzzing*), el servidor implementa una política de **descarte silencioso absoluto** antes de que la sesión esté autenticada (`src/liga_bot/services/websocket_bridge_service.py:217-250`).

### Matriz de Comportamiento Pre-Autenticación

| Evento Recibido | Comportamiento del Servidor | Bytes Emitidos al Cliente |
|---|---|---|
| Trama no binaria/texto (e.g. control no estándar) | `continue` (ignorado) | **0 bytes** |
| JSON malformado o truncado | Captura excepción y `continue` | **0 bytes** |
| Ausencia de `data` o UUID no válido | `extract_request_id` devuelve `None`, `continue` | **0 bytes** |
| Comando no reconocido (e.g. `PING`, `ADMIN`, `STATUS`) | `continue` (ignorado en pre-login) | **0 bytes** |
| Comando `SUGGESTION_CREATED` sin autenticar | `continue` (ignorado en pre-login) | **0 bytes** |
| Comando `LOGIN` con token inválido o erróneo | No autentica, `continue` | **0 bytes** |
| Comando `LOGIN` con token válido | Emite `LOGIN_SUCCESS` | Respuesta JSON legítima |

### Beneficios de Seguridad

- **Cero fugas de información:** Un atacante que intente descubrir si el servicio expone una API interna o probee comandos arbitrarios no recibe ningún mensaje de error JSON (`ERROR`, `UNAUTHORIZED`, `INVALID_COMMAND`, etc.).
- **Imposibilidad de inferencia sintáctica:** No es posible determinar si un JSON enviado fue rechazado por formato, por ausencia de UUID o por token inválido, eliminando oráculos de error.

---

## 3. Verificación de Token en Tiempo Constante y Protección de Supertoken Vacío

La comprobación del secreto de integración se realiza en `_validate_token` (`websocket_bridge_service.py:182-190`):

```python
def _validate_token(self, token: str | None) -> bool:
    """Valida el token mediante secrets.compare_digest evitando bypass por supertoken vacío."""
    expected = self.settings.discord_bot_supertoken
    if not expected or not expected.strip():
        logger.warning("Intento de login rechazado: discord_bot_supertoken no configurado.")
        return False
    if not token or not isinstance(token, str):
        return False
    return secrets.compare_digest(token, expected)
```

### Invariantes Criptográficas

1. **Tiempo constante (`secrets.compare_digest`):** La comparación de cadenas no utiliza el operador estándar `==` (que retorna tempranamente ante el primer carácter discrepante), sino `secrets.compare_digest`. Esto neutraliza ataques de canal lateral basados en análisis de tiempos de respuesta (*timing attacks*).
2. **Protección contra omisión por supertoken vacío (*Empty Supertoken Guard*):** Si la variable de entorno `DISCORD_BOT_SUPERTOKEN` no fue configurada o contiene una cadena vacía o compuesta únicamente de espacios en blanco (`not expected or not expected.strip()`):
   - Cualquier intento de conexión es **rechazado incondicionalmente** (`return False`).
   - Se registra una advertencia en los logs del servidor.
   - Es imposible autenticarse enviando un token vacío `""` o nulo para evadir la seguridad en entornos mal configurados.

---

## 4. Timeout de Autenticación (Código de Cierre 4001)

Para prevenir el agotamiento de recursos o descriptores de archivos (*File Descriptors*) mediante conexiones inactivas (*Slowloris / Idle Sockets*), se ejecuta una tarea vigilante en segundo plano (`websocket_bridge_service.py:192-205`):

```python
async def _auth_timeout(
    self,
    ws: web.WebSocketResponse,
    is_authenticated_check: Any,
) -> None:
    try:
        await asyncio.sleep(self.auth_timeout_seconds)
        if not is_authenticated_check() and not ws.closed:
            logger.info("Cerrando WebSocket por timeout de autenticación (código 4001).")
            await ws.close(code=4001, message=b"Authentication timeout")
    except asyncio.CancelledError:
        pass
```

### Reglas Operativas

1. **Temporizador inmutable:** El temporizador se inicializa inmediatamente al aceptar el socket (`prepare()`) con una duración por defecto de `10.0` segundos (`auth_timeout_seconds`, acotado a un mínimo de `0.01s`).
2. **Cancelación ante éxito:** Si el cliente envía una trama `LOGIN` con credenciales válidas antes de expirar el plazo, se ejecuta `login_timer.cancel()`, desactivando el cierre.
3. **Inmunidad ante saturación de tráfico:** El envío de tramas no autenticadas o tráfico basura no reinicia el temporizador. La conexión se desconecta exactamente tras los 10 segundos iniciales con el código de cierre personalizado `4001` y mensaje `b"Authentication timeout"`.
4. **Limpieza en bloque `finally`:** Si la conexión finaliza por cualquier otra razón (desconexión del cliente o error de red), el temporizador se cancela para evitar corrutinas huérfanas en el bucle de eventos.

---

## 5. Cierre Ordenado y Limpieza de Recursos (Código 1000)

Durante la detención del bot o la parada explícita del servicio (`WebsocketBridgeService.stop()`), el sistema ejecuta un protocolo de parada en tres etapas consecutivas (`websocket_bridge_service.py:108-140`):

```
                       WebsocketBridgeService.stop()
                                     │
                                     ▼
                     Etapa 1: Cierre de Sockets Activos
                     • Itera sobre _active_sockets
                     • ws.close(code=1000, message=b"Server shutting down")
                     • _active_sockets.clear()
                                     │
                                     ▼
                     Etapa 2: Drenado de Tareas en Segundo Plano
                     • asyncio.wait(_background_tasks, timeout=2.0)
                     • Cancelación de tareas pendientes (t.cancel())
                     • asyncio.gather(*pending, return_exceptions=True)
                     • _background_tasks.clear()
                                     │
                                     ▼
                     Etapa 3: Teardown de Red
                     • await _runner.cleanup()
                     • _runner = None, _site = None, _app = None
```

- **Código de cierre RFC 6455:** Se utiliza el código estándar `1000` (`WS_NORMAL_CLOSURE`) con la razón en bytes `b"Server shutting down"`.
- **Ventana de gracia para entregas:** Las publicaciones en Discord en vuelo disponen de un margen de hasta `2.0` segundos para completar la interacción HTTP con Discord antes de ser canceladas de forma segura.
- **Idempotencia:** Si `stop()` es invocado múltiples veces consecutivas, las llamadas subsecuentes no generan errores ni excepciones.
