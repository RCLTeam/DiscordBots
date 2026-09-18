"""
Pruebas unitarias para src/liga_bot/utils/formatting.py.

Verifica normalización Unicode NFKD, diacríticos, acrónimos de equipos,
nombres de canal para Discord y plantillas verbatim de mensajes de coordinación.
"""

import string

from liga_bot.config import DEFAULT_REGLAMENTO_CHANNEL
from liga_bot.utils.formatting import (
    MENSAJE_1,
    MENSAJE_2,
    format_match_channel_name,
    format_mensaje_1,
    format_mensaje_2,
    normalize_name,
    normalize_slug,
    normalize_tag,
)

# ---------------------------------------------------------------------------
# 1. normalize_slug
# ---------------------------------------------------------------------------


def test_normalize_slug_basic():
    assert normalize_slug("Planar Shock Pingus") == "planar-shock-pingus"
    assert normalize_slug("Fnix Esports") == "fnix-esports"
    assert normalize_slug("Team Solo Mid") == "team-solo-mid"


def test_normalize_slug_diacritics_and_accents():
    # Acentos españoles y caracteres diacríticos
    assert normalize_slug("Fénix Esports") == "fenix-esports"
    assert normalize_slug("Cádiz C.F.") == "cadiz-c-f"
    assert normalize_slug("Pingüino") == "pinguino"
    assert normalize_slug("Niño Malo") == "nino-malo"


def test_normalize_slug_stylized_fonts():
    # Tipografías matemáticas o estilizadas de Discord
    assert normalize_slug("📜𝗥𝗘𝗚𝗟𝗔𝗠𝗘𝗡𝗧𝗢📜") == "reglamento"
    assert normalize_slug("𝗧𝗜𝗖𝗞𝗘𝗧𝗦-𝗚𝗘𝗡𝗘𝗥𝗔𝗟") == "tickets-general"


def test_normalize_slug_special_characters_and_emojis():
    # Emojis, barras, corchetes y puntuación
    assert normalize_slug("🔴| Planar Shock Pingus") == "planar-shock-pingus"
    assert normalize_slug("Team #1 (LoL)!") == "team-1-lol"
    assert normalize_slug("G2 @ Madrid & Berlin") == "g2-madrid-berlin"


def test_normalize_slug_hyphens_deduplication():
    # Guiones repetidos y en bordes
    assert normalize_slug("---Team---Name---") == "team-name"
    assert normalize_slug("   Team   ---   Name   ") == "team-name"
    assert normalize_slug("a--b--c") == "a-b-c"


def test_normalize_slug_max_length_truncation():
    long_text = "a" * 150
    slug = normalize_slug(long_text, max_length=100)
    assert len(slug) == 100
    assert slug == "a" * 100

    # Truncado que caería en un guión
    text_with_dashes = "abc-def-ghi"
    truncated = normalize_slug(text_with_dashes, max_length=4)
    # "abc-" recortado a max 4 sin guión final -> "abc"
    assert truncated == "abc"


def test_normalize_slug_empty_and_symbols():
    assert normalize_slug("") == ""
    assert normalize_slug("   ") == ""
    assert normalize_slug("---") == ""
    assert normalize_slug("🎉✨🔥") == ""


# ---------------------------------------------------------------------------
# 2. normalize_tag
# ---------------------------------------------------------------------------


def test_normalize_tag_basic():
    assert normalize_tag("psp") == "PSP"
    assert normalize_tag("fnx") == "FNX"
    assert normalize_tag("G2") == "G2"


def test_normalize_tag_whitespace_handling():
    assert normalize_tag("  tsm  ") == "TSM"
    assert normalize_tag(" t  s ") == "TS"


def test_normalize_tag_max_length_truncation():
    assert normalize_tag("LONGERTAG") == "LONG"
    assert normalize_tag("ABCDE", max_length=4) == "ABCD"
    assert normalize_tag("XYZ", max_length=4) == "XYZ"


def test_normalize_tag_empty():
    assert normalize_tag("") == ""
    assert normalize_tag("   ") == ""


# ---------------------------------------------------------------------------
# 3. format_match_channel_name
# ---------------------------------------------------------------------------


def test_format_match_channel_name_standard():
    channel = format_match_channel_name(1, "PSP", "FNX")
    assert channel == "j1-psp-vs-fnx"

    channel_j10 = format_match_channel_name(10, "LOB", "CRV")
    assert channel_j10 == "j10-lob-vs-crv"


def test_format_match_channel_name_with_full_names():
    channel = format_match_channel_name(2, "Planar Shock", "Fnix Esports")
    assert channel == "j2-planar-shock-vs-fnix-esports"


def test_format_match_channel_name_discord_length_limit():
    team1 = "a" * 60
    team2 = "b" * 60
    channel = format_match_channel_name(1, team1, team2, max_length=100)
    assert len(channel) <= 100
    assert not channel.endswith("-")
    assert channel.startswith("j1-")


# ---------------------------------------------------------------------------
# 4. normalize_name
# ---------------------------------------------------------------------------


def test_normalize_name_clean():
    assert (
        normalize_name("🔴| Planar Shock Pingus")
        == normalize_name("Planar Shock Pingus")
        == "planar shock pingus"
    )
    assert normalize_name("Fénix Esports") == "fenix esports"
    assert normalize_name("   Multiple    Spaces   ") == "multiple spaces"
    assert normalize_name("") == ""


# ---------------------------------------------------------------------------
# 5. format_mensaje_1 & format_mensaje_2 (Verbatim matching)
# ---------------------------------------------------------------------------


def test_format_mensaje_1_verbatim():
    msg = format_mensaje_1(
        jornada=1,
        fecha="13/09/2026",
        hora="21:00",
        equipo1="<@&111>",
        equipo2="<@&222>",
    )
    assert msg.startswith("**Jornada 1 [13/09/2026 21:00]** <@&111> VS <@&222>")
    assert "Este canal es la única vía oficial para coordinar vuestro partido." in msg
    assert "**ACUERDO DE HORARIO**" in msg
    assert "Debéis confirmar el horario antes del jueves a las 23:59h." in msg
    assert "**CONVOCATORIA**" in msg
    assert "* (-1 BAN): Si se envía con menos de 4 horas de antelación" in msg
    assert "* (0 BANS mapa 1): Si se envía con menos de 30 minutos" in msg
    assert "* Abandono: Si no se ha enviado a falta de 5 minutos para el inicio." in msg


def test_format_mensaje_1_role_ids_signature():
    # Comprobar llamada con team1_role_id y team2_role_id
    msg = format_mensaje_1(team1_role_id=111, team2_role_id=222)
    assert "<@&111> VS <@&222>" in msg
    assert "**Jornada 1 [Por definir Por definir]**" in msg

    # Comprobar llamada posicional con dos enteros
    msg2 = format_mensaje_1(333, 444)
    assert "<@&333> VS <@&444>" in msg2


def test_format_mensaje_2_verbatim():
    msg_default = format_mensaje_2()
    assert msg_default.startswith("**PREPARACIÓN Y DRAFT**")
    assert "https://lol.draftcore.net/ en formato Fearless Draft." in msg_default
    assert "https://drafter.lol/" in msg_default
    assert f"Normas completas y detalladas en {DEFAULT_REGLAMENTO_CHANNEL}." in msg_default

    # Con mención personalizada de canal
    custom_reglamento = "<#1548795782360993999>"
    msg_custom = format_mensaje_2(reglamento=custom_reglamento)
    assert f"Normas completas y detalladas en {custom_reglamento}." in msg_custom


def test_templates_have_no_missing_keys():
    # Comprobar que las constantes MENSAJE_1 y MENSAJE_2 contienen exactamente los campos esperados
    formatter = string.Formatter()
    fields_1 = {field_name for _, field_name, _, _ in formatter.parse(MENSAJE_1) if field_name}
    assert fields_1 == {"jornada", "fecha", "hora", "equipo1", "equipo2"}

    fields_2 = {field_name for _, field_name, _, _ in formatter.parse(MENSAJE_2) if field_name}
    assert fields_2 == {"reglamento"}
