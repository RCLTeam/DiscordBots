"""
Módulo de utilidades de formato, normalización y plantillas de Discord para LigaBot.

Proporciona:
- Normalización Unicode NFKD y generación de slugs seguros para canales de Discord.
- Normalización y acotación estricta de tags de equipo (máx 4 caracteres).
- Generación de nombres canónicos para canales de enfrentamiento.
- Normalización de nombres para comparaciones insensibles a caracteres tipográficos.
- Plantillas y formateadores de mensajes de coordinación de partidos (verbatim).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any, Final
from urllib.parse import quote

from liga_bot.config import DEFAULT_REGLAMENTO_CHANNEL

# ---------------------------------------------------------------------------
# Plantillas verbatim de mensajes de coordinación (liga_bot.py:57-86)
# ---------------------------------------------------------------------------

MENSAJE_1: Final[str] = (
    "**Jornada {jornada} [{fecha} {hora}]** {equipo1} VS {equipo2}\n\n"
    "Este canal es el único medio oficial para organizar vuestro enfrentamiento. "
    "Ante cualquier incidencia, el STAFF resolverá basándose únicamente en lo hablado aquí, "
    "así que no uséis mensajes privados.\n\n"
    "**ACUERDO DE HORARIO**\n"
    "El horario debe quedar cerrado antes del martes a las 23:59h. "
    "Si no llegáis a un acuerdo, el encuentro se disputará en el horario asignado "
    "por defecto.\n\n"
    "**CONVOCATORIA**\n"
    "Primer mapa. Enviad por este canal la alineación (OP.GG) con sus roles "
    "(TOP, JGL, MID, ADC, SUPP) y los suplentes, con un mínimo de 6h de antelación "
    "respecto a la hora del partido.\n\n"
    "El equipo que no presente su alineación dentro de ese plazo disputará el partido "
    "sin derecho a bans.\n"
)

MENSAJE_2: Final[str] = (
    "**PREPARACIÓN Y DRAFT**\n"
    "* Desde la hora pactada disponéis de 10 minutos para arrancar el draft; "
    "superado ese margen, el mapa se da por perdido.\n"
    "* Comunicad cuanto antes las sustituciones y la elección de lado entre mapas.\n"
    "* Los jugadores permanecerán en sus canales de voz durante toda la serie, descansos "
    "incluidos. Coachs y suplentes pueden acompañarles en los descansos y en el draft.\n"
    "* El draft se realiza en https://lol.draftcore.net/ con formato Fearless Draft. "
    "El STAFF os facilitará por aquí el enlace que debéis utilizar.\n"
    "* Usad la función de intercambio de draftcore para que el orden de los campeones "
    "coincida con el de la partida (TOP, JNG, MID, ADC, SUPP).\n"
    "* Si algo falla, coordinadlo por este canal o recurrid a https://drafter.lol/.\n\n"
    "Tenéis la normativa completa en {reglamento}. Recordad que desconocerla no exime "
    "de cumplirla.\n"
)


# ---------------------------------------------------------------------------
# Funciones de normalización de cadenas y slugs
# ---------------------------------------------------------------------------


def normalize_slug(text: str, max_length: int = 100) -> str:
    """
    Genera un slug seguro para nombres de canal de Discord a partir de un texto.

    - Aplica normalización NFKD para descomponer glifos y caracteres especiales.
    - Elimina marcas diacríticas y acentos combinados.
    - Pasa a minúsculas y reemplaza caracteres no alfanuméricos por guiones.
    - Colapsa guiones consecutivos y recorta guiones en los extremos.
    - Limita la longitud al máximo especificado (por defecto 100, límite de Discord).
    """
    if not text:
        return ""

    # Normalización NFKD y descarte de marcas diacríticas
    nfkd_form = unicodedata.normalize("NFKD", text)
    decomposed = "".join(c for c in nfkd_form if not unicodedata.combining(c))

    # Conversión a minúsculas
    lowered = decomposed.lower()

    # Reemplazo de cualquier caracter que no sea letra a-z o dígito 0-9 por guión
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)

    # Colapso de guiones repetidos y recorte en extremos
    slug = re.sub(r"-+", "-", slug).strip("-")

    # Recorte a longitud máxima sin dejar guión final
    if max_length > 0 and len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")

    return slug


def normalize_tag(tag: str, max_length: int = 4) -> str:
    """
    Normaliza el tag o acrónimo de un equipo deportivo.

    - Elimina espacios en blanco internos y externos.
    - Pasa a mayúsculas.
    - Trunca a un máximo de 4 caracteres para respetar la restricción relacional.
    """
    if not tag:
        return ""

    cleaned = "".join(c for c in tag.strip() if not c.isspace())
    upper_tag = cleaned.upper()
    return upper_tag[:max_length]


OPGG_DEFAULT_REGION: Final[str] = "euw"


def build_opgg_url(
    game_name: str,
    riot_tag: str | None = None,
    region: str = OPGG_DEFAULT_REGION,
) -> str:
    """
    Construye el enlace al perfil de op.gg de una cuenta de League of Legends.

    Formato: https://op.gg/es/lol/summoners/<region>/<NombreInvocador>-<RiotTag>
    Los espacios y caracteres especiales se codifican para que el enlace sea válido.
    """
    cleaned_name = game_name.strip()
    cleaned_tag = (riot_tag or "").strip().lstrip("#")
    slug = f"{cleaned_name}-{cleaned_tag}" if cleaned_tag else cleaned_name
    return f"https://op.gg/es/lol/summoners/{region}/{quote(slug, safe='')}"


def apply_team_tag(nick: str, team_tag: str, known_tags: Iterable[str]) -> str:
    """
    Antepone el tag del equipo al apodo respetando el resto del nombre.

    - Si la primera palabra del apodo es el tag de otro equipo, se sustituye.
    - Si ya es el tag de este equipo, el apodo se deja intacto.
    - En cualquier otro caso, el tag se añade al principio.
    """
    tag = normalize_tag(team_tag)
    cleaned_nick = nick.strip()
    if not tag or not cleaned_nick:
        return cleaned_nick

    known = {normalize_tag(t) for t in known_tags}
    known.discard("")

    first, _, rest = cleaned_nick.partition(" ")
    if first.upper() in known:
        if first.upper() == tag:
            return cleaned_nick
        return f"{tag} {rest}".strip()

    return f"{tag} {cleaned_nick}"


def format_match_channel_name(
    jornada: int,
    team1_tag_or_slug: str,
    team2_tag_or_slug: str,
    max_length: int = 100,
) -> str:
    """
    Genera el nombre normalizado del canal de texto de Discord para un partido.

    Formato canónico: 'j{jornada}-{team1_slug}-vs-{team2_slug}'
    Garantiza una longitud máxima de 100 caracteres compatible con la API de Discord.
    """
    slug1 = normalize_slug(team1_tag_or_slug)
    slug2 = normalize_slug(team2_tag_or_slug)
    channel_name = f"j{jornada}-{slug1}-vs-{slug2}"

    if max_length > 0 and len(channel_name) > max_length:
        channel_name = channel_name[:max_length].rstrip("-")

    return channel_name


def normalize_name(name: str) -> str:
    """
    Normaliza el nombre de un equipo o rol para facilitar comparaciones insensibles
    a mayúsculas, acentos, emojis o caracteres tipográficos decorativos.

    Preserva espacios internos simples entre palabras.
    """
    if not name:
        return ""

    nfkd = unicodedata.normalize("NFKD", name)
    without_diacritics = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = "".join(c for c in without_diacritics if c.isalnum() or c.isspace())
    return " ".join(cleaned.lower().split())


# ---------------------------------------------------------------------------
# Formateadores de mensajes de partido
# ---------------------------------------------------------------------------


def format_mensaje_1(
    *args: Any,
    jornada: int | None = None,
    fecha: str | None = None,
    hora: str | None = None,
    equipo1: str | int | None = None,
    equipo2: str | int | None = None,
    team1_role_id: int | None = None,
    team2_role_id: int | None = None,
    **kwargs: Any,
) -> str:
    """
    Formatea el primer mensaje de coordinación de partido con reglas y convocatorias.
    Coincide exactamente con la plantilla verbatim de liga_bot.py:57-74.

    Soporta firmas flexibles:
    - format_mensaje_1(team1_role_id=111, team2_role_id=222)
    - format_mensaje_1(111, 222) [cuando se pasan 2 enteros de roles]
    - format_mensaje_1(jornada=1, fecha="13/09/2026", hora="21:00", equipo1="<@&111>", ...)
    - format_mensaje_1(1, "13/09/2026", "21:00", "<@&111>", "<@&222>")
    """
    # Resolución por argumentos posicionales
    if len(args) == 2 and isinstance(args[0], int) and isinstance(args[1], int):
        if team1_role_id is None:
            team1_role_id = args[0]
        if team2_role_id is None:
            team2_role_id = args[1]
    elif len(args) >= 5:
        jornada = args[0] if jornada is None else jornada
        fecha = args[1] if fecha is None else fecha
        hora = args[2] if hora is None else hora
        equipo1 = args[3] if equipo1 is None else equipo1
        equipo2 = args[4] if equipo2 is None else equipo2

    if team1_role_id is not None and equipo1 is None:
        equipo1 = f"<@&{team1_role_id}>"
    if team2_role_id is not None and equipo2 is None:
        equipo2 = f"<@&{team2_role_id}>"

    # Si se pasaron enteros directos en equipo1/equipo2
    if isinstance(equipo1, int):
        equipo1 = f"<@&{equipo1}>"
    elif equipo1 is None:
        equipo1 = "Equipo 1"

    if isinstance(equipo2, int):
        equipo2 = f"<@&{equipo2}>"
    elif equipo2 is None:
        equipo2 = "Equipo 2"

    jornada_val = 1 if jornada is None else jornada
    fecha_val = "Por definir" if fecha is None else fecha
    hora_val = "Por definir" if hora is None else hora

    return MENSAJE_1.format(
        jornada=jornada_val,
        fecha=fecha_val,
        hora=hora_val,
        equipo1=equipo1,
        equipo2=equipo2,
    )


def format_mensaje_2(
    reglamento: str = DEFAULT_REGLAMENTO_CHANNEL,
) -> str:
    """
    Formatea el segundo mensaje de preparación, draft y enlace al reglamento.
    Coincide exactamente con la plantilla verbatim de liga_bot.py:76-86.
    """
    return MENSAJE_2.format(reglamento=reglamento)
