# Arquitectura y Núcleo de Ejecución

[⬅️ Volver a Documentación](../README.md) | [Siguiente: Base de Datos ➡️](../features/database/README.md)

Este directorio documenta los fundamentos arquitectónicos, el ciclo de vida de los procesos, el sistema de configuración basado en Pydantic Settings v2, la consola de administración CLI y la infraestructura de motores de base de datos de `LigaBot`.

---

## Índice de Documentación

| Documento | Descripción |
|---|---|
| [**runtime.md**](./runtime.md) | Detalla la clase `LigaBot`, el contenedor de inyección de dependencias (6 servicios de dominio), el ciclo de vida asíncrono en `setup_hook`, el cierre ordenado e idempotente en `close`, el manejo de señales OS y el desacoplamiento de `on_ready`. |
| [**configuration.md**](./configuration.md) | Describe la configuración mediante Pydantic Settings v2, la matriz exhaustiva de las 18 variables de entorno, constantes canónicas de la liga, normalización asíncrona de URLs y la factoría singleton en caché. |
| [**cli.md**](./cli.md) | Explica la interfaz de línea de comandos construida sobre la librería estándar `argparse`, la sintaxis del comando `seed-teams`, los esquemas JSON/CSV, la delimitación transaccional y el algoritmo de coincidencia idempotente. |
| [**database-engine.md**](./database-engine.md) | Analiza la estrategia de motor dual (PostgreSQL vía `asyncpg` y PGlite vía WebAssembly/Node.js), el sistema de cerrojos `_engine_locks` para evitar *rollback bleed*, las factorías de sesiones asíncronas y los context managers transaccionales. |
