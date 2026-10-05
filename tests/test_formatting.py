"""
Pruebas unitarias para src/liga_bot/utils/formatting.py.

Verifica normalización Unicode NFKD, diacríticos, acrónimos de equipos,
nombres de canal para Discord y plantillas verbatim de mensajes de coordinación.
"""

import string
from datetime import datetime, timezone

import pytest

from liga_bot.config import DEFAULT_REGLAMENTO_CHANNEL, Settings, get_settings
from liga_bot.utils.formatting import (
    MENSAJE_1,
    MENSAJE_2,
    apply_team_tag,
    build_opgg_url,
    format_match_channel_name,
    format_mensaje_1,
    format_mensaje_2,
    normalize_name,
    normalize_slug,
    normalize_tag,
    parse_scheduled_at,
    strip_team_tag,
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
    assert "Este canal es el único medio oficial para organizar vuestro enfrentamiento." in msg
    assert "**ACUERDO DE HORARIO**" in msg
    assert "El horario debe quedar cerrado antes del martes a las 23:59h." in msg
    assert "**CONVOCATORIA**" in msg
    assert "con un mínimo de 6h de antelación" in msg
    assert "disputará el partido sin derecho a bans." in msg
    assert "y los suplentes" in msg


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ((1, "13/09/2026", "21:00", "<@&111>", "<@&222>"), {}),
        ((333, 444), {}),
        ((), {"team1_role_id": 111, "team2_role_id": 222}),
        ((), {"jornada": 1, "fecha": "13/09/2026", "hora": "21:00"}),
    ],
    ids=["posicional", "dos-roles-posicionales", "ids-de-rol", "faltan-equipos"],
)
def test_format_mensaje_1_solo_admite_los_cinco_argumentos_por_nombre(args, kwargs):
    with pytest.raises(TypeError):
        format_mensaje_1(*args, **kwargs)


def test_format_mensaje_2_verbatim():
    msg_default = format_mensaje_2()
    assert msg_default.startswith("**PREPARACIÓN Y DRAFT**")
    assert "https://lol.draftcore.net/ con formato Fearless Draft." in msg_default
    assert "https://drafter.lol/" in msg_default
    assert f"Tenéis la normativa completa en {DEFAULT_REGLAMENTO_CHANNEL}." in msg_default

    # Con mención personalizada de canal
    custom_reglamento = "<#1548795782360993999>"
    msg_custom = format_mensaje_2(reglamento=custom_reglamento)
    assert f"Tenéis la normativa completa en {custom_reglamento}." in msg_custom


def test_templates_have_no_missing_keys():
    # Comprobar que las constantes MENSAJE_1 y MENSAJE_2 contienen exactamente los campos esperados
    formatter = string.Formatter()
    fields_1 = {field_name for _, field_name, _, _ in formatter.parse(MENSAJE_1) if field_name}
    assert fields_1 == {"jornada", "fecha", "hora", "equipo1", "equipo2"}

    fields_2 = {field_name for _, field_name, _, _ in formatter.parse(MENSAJE_2) if field_name}
    assert fields_2 == {"reglamento"}


# ---------------------------------------------------------------------------
# apply_team_tag
# ---------------------------------------------------------------------------

KNOWN_TAGS = ["PSP", "TLG", "FNX"]


def test_apply_team_tag_prefixes_when_no_previous_tag():
    """Un apodo sin tag de equipo recibe el tag al principio."""
    assert apply_team_tag("Ninym", "PSP", KNOWN_TAGS) == "PSP Ninym"
    assert apply_team_tag("Ninym #EUW", "PSP", KNOWN_TAGS) == "PSP Ninym #EUW"


def test_apply_team_tag_is_idempotent_with_same_tag():
    """Si el apodo ya empieza por el tag del equipo, se deja intacto."""
    assert apply_team_tag("TLG Hiperxp", "TLG", KNOWN_TAGS) == "TLG Hiperxp"
    assert apply_team_tag("tlg Hiperxp", "TLG", KNOWN_TAGS) == "tlg Hiperxp"


def test_apply_team_tag_replaces_previous_team_tag():
    """El tag de otro equipo en la primera palabra se sustituye, no se encadena."""
    assert apply_team_tag("TLG Hiperxp", "PSP", KNOWN_TAGS) == "PSP Hiperxp"
    assert apply_team_tag("FNX Ninym #EUW", "PSP", KNOWN_TAGS) == "PSP Ninym #EUW"


def test_apply_team_tag_ignores_first_word_that_is_not_a_tag():
    """Una primera palabra que no es tag de ningún equipo se conserva."""
    assert apply_team_tag("Dark Ninym", "PSP", KNOWN_TAGS) == "PSP Dark Ninym"
    # No se confunde con un tag por compartir prefijo
    assert apply_team_tag("PSPlayer Ninym", "PSP", KNOWN_TAGS) == "PSP PSPlayer Ninym"


def test_apply_team_tag_without_tag_returns_nick():
    """Sin tag de equipo, el apodo no se modifica."""
    assert apply_team_tag("Ninym", "", KNOWN_TAGS) == "Ninym"


# ---------------------------------------------------------------------------
# build_opgg_url
# ---------------------------------------------------------------------------


def test_build_opgg_url_nombre_y_tag():
    """El enlace usa el formato <nombre>-<tag> sobre la región por defecto."""
    assert build_opgg_url("Ninym", "Shiro") == "https://op.gg/es/lol/summoners/euw/Ninym-Shiro"


def test_build_opgg_url_admite_almohadilla():
    """El Riot Tag se acepta con o sin '#'."""
    assert build_opgg_url("Ninym", "#Shiro") == build_opgg_url("Ninym", "Shiro")


def test_build_opgg_url_codifica_caracteres_especiales():
    """Los espacios y caracteres no ASCII se codifican para que el enlace sea válido."""
    url = build_opgg_url("Planar Shock", "EUW1")
    assert url == "https://op.gg/es/lol/summoners/euw/Planar%20Shock-EUW1"
    assert " " not in url


def test_build_opgg_url_sin_tag():
    """Sin Riot Tag, el enlace queda solo con el nombre de invocador."""
    assert build_opgg_url("Solo", None) == "https://op.gg/es/lol/summoners/euw/Solo"


# ---------------------------------------------------------------------------
# parse_scheduled_at
# ---------------------------------------------------------------------------


def test_parse_scheduled_at_horario_verano():
    """En horario de verano (CEST, UTC+2) las 17:00 de la liga son 15:00 UTC."""
    dt = parse_scheduled_at("02/10/2026", "17:00")
    assert dt == datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)


def test_parse_scheduled_at_horario_invierno():
    """En horario de invierno (CET, UTC+1) las 19:00 de la liga son 18:00 UTC."""
    dt = parse_scheduled_at("15/01/2027", "19:00")
    assert dt == datetime(2027, 1, 15, 18, 0, tzinfo=timezone.utc)


def test_parse_scheduled_at_entrada_invalida():
    """Una fecha u hora con formato incorrecto devuelve None en lugar de lanzar."""
    assert parse_scheduled_at("2026-10-02", "17:00") is None
    assert parse_scheduled_at("02/10/2026", "25:00") is None
    assert parse_scheduled_at("", "") is None


# ---------------------------------------------------------------------------
# strip_team_tag
# ---------------------------------------------------------------------------


def test_strip_team_tag_quita_el_tag_inicial():
    """El tag de equipo del principio desaparece y el resto del apodo se conserva."""
    assert strip_team_tag("PSP Ninym", ["PSP", "PAN"]) == "Ninym"


def test_strip_team_tag_respeta_apodos_sin_tag():
    """Un apodo que no empieza por un tag conocido se devuelve intacto."""
    assert strip_team_tag("Ninym Shiro", ["PSP"]) == "Ninym Shiro"
    assert strip_team_tag("Ninym", ["PSP"]) == "Ninym"


def test_strip_team_tag_no_vacia_el_apodo():
    """Si el apodo es solo el tag, se deja como está en vez de quedarse sin nombre."""
    assert strip_team_tag("PSP", ["PSP"]) == "PSP"


def test_format_mensaje_2_uses_configured_reglamento_channel():
    """Sin mención explícita, el mensaje usa REGLAMENTO_CHANNEL_ID de la configuración."""
    settings = Settings(reglamento_channel_id=123456789)
    msg = format_mensaje_2(settings=settings)
    assert "Tenéis la normativa completa en <#123456789>." in msg


def test_format_mensaje_2_reads_reglamento_channel_from_environment(monkeypatch):
    """Sin argumentos, el canal sale de get_settings() (entorno)."""
    monkeypatch.setenv("REGLAMENTO_CHANNEL_ID", "987654321")
    get_settings.cache_clear()
    msg = format_mensaje_2()
    assert "Tenéis la normativa completa en <#987654321>." in msg
