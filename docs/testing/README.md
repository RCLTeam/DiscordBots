# Subsistema de Testing y Calidad

[⬅️ Volver a Documentación](../README.md) | [Siguiente: Arquitectura ➡️](../architecture/README.md)

---

## Resumen Ejecutivo

El subsistema de pruebas de `DiscordBots` proporciona una infraestructura de verificación exhaustiva, determinista y de alta fidelidad relacional. Con un total de **1.138 casos de prueba** distribuidos en **50 archivos** (49 archivos de test más `tests/conftest.py`), sumando **33.981 líneas totales de test** (27.014 líneas efectivas de código excluyendo comentarios y blancos), el conjunto valida desde la lógica pura en memoria hasta condiciones extremas de concurrencia y límites del kernel del sistema operativo.

El stack de pruebas se ejecuta de forma hermética utilizando **PGlite** (`py-pglite[sqlalchemy]`), una compilación WebAssembly/C de PostgreSQL sobre sockets UNIX locales, eliminando la necesidad de contenedores Docker externos o servicios de base de datos remotos y garantizando compatibilidad 100% con tipos y restricciones nativas de PostgreSQL.

---

## Contenido del Directorio

| Documento | Descripción |
|---|---|
| [**Estrategia de Pruebas (`strategy.md`)**](strategy.md) | Filosofía de pruebas, arquitectura en pirámide de 4 niveles (Unit 46%, Integration 15%, Resilience 9.7%, Adversarial 29.3%), ciclo de vida de fixtures con PGlite, aislamiento transaccional por savepoints y factoría de mocks de Discord. |
| [**Catálogo de Suites (`suites.md`)**](suites.md) | Inventario completo de los 49 archivos de prueba clasificados por nivel de pirámide, métricas de casos y LoC por archivo, convenciones de nomenclatura y comandos de ejecución con `uv run pytest`. |
| [**Límites de Entorno (`environment-limits.md`)**](environment-limits.md) | Análisis técnico de los 3 límites críticos de infraestructura mitigados: desbordamiento de buffer UNIX en Node 24+ (>16KB), serialización de transacciones con `asyncio.Lock` en PGlite, y liberación de descriptores de sockets en ciclos rápidos. |

---

## Comandos de Ejecución Rápida

```bash
# Recolección e inventario de casos de prueba
uv run pytest --collect-only -q

# Ejecución completa de la suite (1.138 tests, ~35.8s)
uv run pytest

# Ejecución de suites críticas de límites de entorno y resiliencia
uv run pytest tests/test_adversarial_roster_cascades.py \
              tests/test_adversarial_roster_concurrency_and_audit.py \
              tests/test_bot_bridge_lifecycle_resilience.py
```
