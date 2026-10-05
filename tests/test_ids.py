"""
Pruebas unitarias para src/liga_bot/utils/ids.py.

Fija la limpieza de identificadores que comparten los repositorios y servicios de
casters y plantillas: UUID de entidades e ID de usuario de Discord como entero o texto.
"""

from uuid import UUID

import pytest

from liga_bot.utils.ids import clean_user_id_int, clean_user_id_str, clean_uuid

UUID_TEXTO = "12345678-1234-5678-1234-567812345678"


class _ConTextoUUID:
    def __str__(self) -> str:
        return UUID_TEXTO


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (UUID(UUID_TEXTO), UUID(UUID_TEXTO)),
        (UUID_TEXTO, UUID(UUID_TEXTO)),
        (f"  {UUID_TEXTO}  ", UUID(UUID_TEXTO)),
        (UUID_TEXTO.replace("-", ""), UUID(UUID_TEXTO)),
        (_ConTextoUUID(), UUID(UUID_TEXTO)),
        (None, None),
        ("", None),
        ("   ", None),
        ("no-es-un-uuid", None),
        (123, None),
        (12.5, None),
        (b"bytes", None),
    ],
    ids=[
        "uuid",
        "texto",
        "texto-con-espacios",
        "hex-sin-guiones",
        "objeto-con-str-uuid",
        "none",
        "vacio",
        "solo-espacios",
        "texto-invalido",
        "entero",
        "flotante",
        "bytes",
    ],
)
def test_clean_uuid(valor, esperado):
    assert clean_uuid(valor) == esperado


def test_clean_uuid_devuelve_la_misma_instancia_uuid():
    valor = UUID(UUID_TEXTO)
    assert clean_uuid(valor) is valor


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (123, 123),
        ("123", 123),
        ("  123  ", 123),
        ("+123", 123),
        (12.9, 12),
        ("", None),
        ("   ", None),
        ("abc", None),
        (0, None),
        ("0", None),
        (-10, None),
        ("-10", None),
        (None, None),
        (True, None),
        (False, None),
        (object(), None),
    ],
)
def test_clean_user_id_int(valor, esperado):
    assert clean_user_id_int(valor) == esperado


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (123, "123"),
        ("123", "123"),
        ("  123  ", "123"),
        ("abc", "abc"),
        (0, "0"),
        (-10, "-10"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_clean_user_id_str(valor, esperado):
    assert clean_user_id_str(valor) == esperado
