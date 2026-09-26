[⬅️ Volver a Tickets](../tickets/README.md) | [Siguiente: Estrategia de Pruebas ➡️](../../testing/README.md)

---

# Calendario y Seguimiento de Partidos (Schedule & Match Tracking)

El subsistema de Calendario y Seguimiento de Partidos gestiona el ciclo de vida completo de los enfrentamientos deportivos en LigaBot. Proporciona herramientas automatizadas para la programación de partidos individuales y la ingesta masiva de jornadas mediante archivos CSV, el aprovisionamiento de categorías y canales de texto privados en Discord con control de acceso por división y roles, la publicación de mensajes oficiales de convocatoria y Fearless Draft, y la persistencia transaccional en PostgreSQL con garantías contra canales huérfanos.

## Resumen Ejecutivo

- **Aprovisionamiento Dinámico en Discord**: Crea canales privados bajo categorías estructuradas por jornada (`PREMIER - JORNADA {X}` y `ASCENSO - JORNADA {X}`), configurando matrices de permisos herméticas para los roles de los equipos participantes, el equipo de administración y los CEOs de cada división.
- **Protocolo Oficial de Coordinación**: Publica automáticamente en cada canal creado los mensajes institucionales de acuerdo de horarios, penalizaciones por convocatoria tardía (OP.GG) y normativas de Fearless Draft.
- **Ingesta en Lote Resiliente**: Permite a los administradores subir archivos CSV para crear jornadas completas, tolerando codificaciones con BOM (Microsoft Excel), detectando delimitadores automáticos y aislando fallos por fila para permitir importaciones parciales seguras.
- **Garantía Anti-Canales Huérfanos**: Implementa un protocolo de rollback defensivo que elimina de inmediato los canales creados en Discord si se produce un fallo durante el posteo de mensajes o la persistencia relacional en la base de datos.

---

## Contenido del Módulo

| Documento | Descripción |
|---|---|
| [`commands.md`](commands.md) | Especificación de los comandos slash `/crear-partido`, `/importar-jornada` y el alias `/crear-jornada`, incluyendo parámetros, permisos y embeds de respuesta. |
| [`services.md`](services.md) | Arquitectura de `ScheduleService`, flujo multi-fase de aprovisionamiento, detección simétrica de duplicados, rollback defensivo y utilidades de formato. |
| [`persistence.md`](persistence.md) | Modelo declarativo `Match`, restricciones DDL (`UniqueConstraint`, `CheckConstraint`), índices, claves foráneas en cascada y consultas en `MatchRepository`. |
