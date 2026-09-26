# Módulo de Tickets y Administración de Staff

[⬅️ Volver a Roles](../roles/README.md) | [Siguiente: Calendario y Jornadas ➡️](../schedule/README.md)

---

## Resumen Ejecutivo

El módulo de Tickets y Administración de Staff coordina la supervisión de canales de atención y soporte a usuarios, la administración y sincronización del árbol de comandos de aplicación (*Application Commands*) ante la API de Discord y el registro transaccional inmutable de auditoría para operaciones críticas. Su función principal consiste en actuar como centinela pasivo y activo frente a tickets desatendidos (notificando a los estamentos de staff tras 24 horas de inactividad sin destruir canales automáticamente), garantizar la ausencia de canales huérfanos mediante mecanismos de compensación ante fallos de persistencia y registrar trazas estructuradas en formato JSONB.

---

## Tabla de Contenidos

| Documento | Descripción Técnica |
|---|---|
| [Comandos Slash (`commands.md`)](commands.md) | Documentación completa de los comandos `/sync`, `/sincronizar`, `/revisar-tickets` y `/revisar-tickets-manual`, sus restricciones de autorización multinivel (`is_staff_or_admin`, `is_authorized_scheduler`), formato de Embeds con truncado defensivo para límites de Discord y refutación fáctica de comandos hipotéticos. |
| [Servicios de Dominio (`services.md`)](services.md) | Arquitectura y flujo operativo de `TicketService`, bucle periódico de 24 horas (`check_tickets_loop`) con auto-restart, normalización de categorías con Unicode NFKD, prevención de auto-bucle del bot, supresión de alertas repetidas, throttling de 0.3s contra límites de tasa de Discord y garantías de rollback anti-huérfanos. |
| [Capa de Persistencia (`persistence.md`)](persistence.md) | Modelos declarativos SQLAlchemy 2.0 (`TicketNotice`, `AuditLog`), esquemas relacionales, repositorios asíncronos (`TicketNoticeRepository`, `AuditLogRepository`), patrón de upsert idempotente y algoritmo de normalización recursiva `_to_json_safe` para compatibilidad con JSONB en PostgreSQL y PGlite. |
