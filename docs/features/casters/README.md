[⬅️ Volver a Funcionalidades](../README.md) | [⬅️ Anterior: Calendario y Jornadas](../schedule/README.md) | [Siguiente: Estrategia de Pruebas ➡️](../../testing/README.md)

---

# Cartelera y Casters (Casters Panel & Interactive Broadcasts)

El subsistema de Cartelera y Casters gestiona la cobertura audiovisual de los enfrentamientos de la liga RCL en Discord. Proporciona herramientas para la publicación interactiva de tarjetas de partidos por jornada, la postulación en vivo de miembros en tres roles de casteo y retransmisión (**Castear**, **Retransmitir** y **Ambas mezcladas**), la garantía estricta de exclusividad de streamer en PostgreSQL, la sincronización automática in-place de horarios reprogramados y la persistencia de interacciones ante reinicios del bot mediante componentes dinámicos de Discord.

## Resumen Ejecutivo

- **Cartelera Interactiva por Jornada**: Permite a los administradores y organizadores desplegar tarjetas visuales enriquecidas (`discord.Embed`) con el comando `/panel-casters` (o su alias `/cartelera-casters`), reflejando división, equipos, horario en formato de timestamp relativo de Discord, canal de retransmisión y panel de casters.
- **Roles de Cobertura y Exclusividad Estricta**: Soporta tres modalidades de asignación mediante botones interactivos:
  - 🎙️ **Castear** (`CASTER`): Narración o análisis por voz. Abierto a cualquier número de participantes simultáneos sin límite superior.
  - 📺 **Retransmitir (Solo PC)** (`STREAMER`): Emisión técnica de la señal de juego desde el cliente de League of Legends. Exclusivo para un único usuario por partido.
  - 🎬 **Ambas mezcladas** (`BOTH`): Narración simultánea y retransmisión técnica desde el mismo PC. Exclusivo para un único usuario por partido.
  - ❌ **Desapuntarse**: Permite a cualquier usuario retirarse de forma autónoma, rehabilitando en caliente los botones de retransmisión si quien se retira ocupaba dicho puesto.
- **Garantía Dual de Exclusividad**: La restricción de un único streamer por partido se valida en dos capas desacopladas: a nivel de servicio de dominio (`CasterService.assign_caster`) y a nivel de motor de base de datos relacional mediante el índice único parcial PostgreSQL `uq_match_casters_single_streamer`.
- **Persistencia Dinámica ante Reinicios (`DynamicItem`)**: Los botones de acción operan como elementos dinámicos (`CasterActionButton`) deserializados en caliente mediante expresiones regulares de su `custom_id` (`^caster:(?P<action>cast|stream|both|leave):(?P<match_id>[0-9a-fA-F-]+)$`), permitiendo atender clics de usuarios incluso si el bot se reinicia tras publicar las tarjetas.
- **Edición In-Place sin Ruido**: Las interacciones de asignación y desasignación actualizan directamente el mensaje existente en Discord (`interaction.response.edit_message`), modificando el embed y el estado habilitado/deshabilitado de los botones sin crear nuevos mensajes ni generar spam en el canal.
- **Publicación Incremental e Idempotente**: El modelo `MatchCasterCard` rastrea qué tarjetas han sido publicadas en cada canal. Invocaciones sucesivas de `/panel-casters` no duplican mensajes: sincronizan horarios in-place de las tarjetas existentes, publican únicamente partidos nuevos incorporados a la jornada y autorreparan el registro si un mensaje fue eliminado manualmente en Discord.

---

## Contenido del Módulo

| Documento | Descripción |
|---|---|
| [`commands.md`](commands.md) | Especificación de los comandos slash `/panel-casters` y `/cartelera-casters`, opciones (`jornada`, `canal`), matriz de permisos requeridos, flujo de aplazamiento, publicación incremental, sincronización in-place y códigos de respuesta efímera. |
| [`ui.md`](ui.md) | Arquitectura visual del embed (`build_match_caster_embed`), codificación de colores, campos y badges de estado, vista persistente `MatchCasterView` (`timeout=None`), botón dinámico `CasterActionButton` (`DynamicItem`), control de roles y edición reactiva in-place. |
| [`services.md`](services.md) | Capa de servicio de dominio `CasterService` y repositorio `CasterRepository`, DTOs de resultado (`MatchCastersData`, `CasterAssignmentResult`), invariantes de exclusividad, cálculo de jornada activa y discriminación de excepciones de integridad en PostgreSQL (`23505` vs `23503`). |
| [`persistence.md`](persistence.md) | Modelos declarativos SQLAlchemy 2.0 (`MatchCaster`, `MatchCasterCard`), tipo enumerado nativo `CasterRole`, relaciones con `Match` (`selectinload`, cascadas `ondelete="CASCADE"`), índice único parcial y migración incremental Alembic `0003_match_casters.py`. |
