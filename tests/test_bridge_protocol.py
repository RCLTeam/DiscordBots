"""Pruebas unitarias para el validador de protocolo bridge y extractor de UUIDs."""

from __future__ import annotations

import json
from typing import Any

import pytest

from liga_bot.services.bridge_protocol import extract_request_id

VALID_UUID_V4 = "e4b3c2a1-0000-4000-8000-0123456789ab"
VALID_UUID_V1 = "123e4567-e89b-12d3-a456-426614174000"
VALID_UUID_ALT = "c7a8b9d0-1111-4222-9333-abcdef012345"


# --- Casos canónicos del plan de referencia ---


def test_extract_request_id_valid_uuid_in_content() -> None:
    """Extrae UUID válido ubicado en data.content.id."""
    payload = {
        "type": "SUGGESTION_CREATED",
        "data": {
            "content": {
                "id": VALID_UUID_V4,
                "suggestion": "Test",
            }
        },
    }
    assert extract_request_id(payload) == VALID_UUID_V4


def test_extract_request_id_valid_uuid_in_data() -> None:
    """Extrae UUID válido ubicado en data.id como fallback (típico en LOG IN)."""
    payload = {
        "type": "LOG IN",
        "data": {
            "id": VALID_UUID_V1,
            "content": "token",
        },
    }
    assert extract_request_id(payload) == VALID_UUID_V1


def test_extract_request_id_missing_uuid() -> None:
    """Descarta silenciosamente retornando None si ningún UUID está presente."""
    payload = {"type": "SUGGESTION_CREATED", "data": {"content": {"suggestion": "No ID"}}}
    assert extract_request_id(payload) is None


def test_extract_request_id_invalid_uuid() -> None:
    """Descarta silenciosamente retornando None si el UUID no tiene formato válido."""
    payload = {"type": "SUGGESTION_CREATED", "data": {"content": {"id": "not-a-uuid"}}}
    assert extract_request_id(payload) is None


def test_extract_request_id_malformed_payload() -> None:
    """Descarta silenciosamente payloads no válidos o diccionarios vacíos."""
    assert extract_request_id("not-a-dict") is None
    assert extract_request_id({}) is None


# --- Casos de normalización canónica ---


def test_extract_request_id_canonical_normalization() -> None:
    """Normaliza UUIDs en mayúsculas o sin guiones a formato RFC 4122 estándar."""
    # Mayúsculas
    upper_payload = {"data": {"id": "123E4567-E89B-12D3-A456-426614174000"}}
    assert extract_request_id(upper_payload) == VALID_UUID_V1

    # Hexadecimal continuo sin guiones (32 caracteres)
    compact_payload = {"data": {"id": "123e4567e89b12d3a456426614174000"}}
    assert extract_request_id(compact_payload) == VALID_UUID_V1


def test_extract_request_id_whitespace_handling() -> None:
    """Limpia espacios en blanco al inicio y al final del identificador."""
    payload = {"data": {"id": f"  {VALID_UUID_V4}  "}}
    assert extract_request_id(payload) == VALID_UUID_V4


def test_extract_request_id_raw_json_string() -> None:
    """Procesa correctamente cadenas y bytes JSON sin procesar."""
    raw_dict = {"type": "LOG IN", "data": {"id": VALID_UUID_V1}}
    raw_str = json.dumps(raw_dict)
    assert extract_request_id(raw_str) == VALID_UUID_V1
    assert extract_request_id(raw_str.encode("utf-8")) == VALID_UUID_V1


# --- Casos de precedencia y fallback ---


def test_extract_request_id_precedence_content_over_data() -> None:
    """Prioriza data.content.id sobre data.id si ambos están presentes."""
    payload = {
        "data": {
            "content": {"id": VALID_UUID_V4},
            "id": VALID_UUID_V1,
        }
    }
    assert extract_request_id(payload) == VALID_UUID_V4


def test_extract_request_id_fallback_when_content_id_is_none() -> None:
    """Usa data.id si content.id es explícitamente None."""
    payload = {
        "data": {
            "content": {"id": None, "text": "foo"},
            "id": VALID_UUID_V1,
        }
    }
    assert extract_request_id(payload) == VALID_UUID_V1


def test_extract_request_id_fallback_when_content_is_string() -> None:
    """Usa data.id si content no es un diccionario sino una cadena de texto."""
    payload = {
        "data": {
            "content": "secret-supertoken",
            "id": VALID_UUID_ALT,
        }
    }
    assert extract_request_id(payload) == VALID_UUID_ALT


def test_extract_request_id_fallback_when_content_has_no_id_key() -> None:
    """Usa data.id si content es un diccionario pero carece de la clave id."""
    payload = {
        "data": {
            "content": {"suggestion": "Añadir emojis"},
            "id": VALID_UUID_ALT,
        }
    }
    assert extract_request_id(payload) == VALID_UUID_ALT


def test_extract_request_id_invalid_content_id_does_not_fallback() -> None:
    """Si content.id fue provisto pero es inválido, no hace fallback a data.id."""
    payload = {
        "data": {
            "content": {"id": "not-a-valid-uuid"},
            "id": VALID_UUID_V1,
        }
    }
    assert extract_request_id(payload) is None


# --- Casos de borde adversariales y tipos no válidos ---


@pytest.mark.parametrize(
    "invalid_id",
    [
        12345,
        3.14159,
        True,
        False,
        ["e4b3c2a1-0000-4000-8000-0123456789ab"],
        {"id": "nested"},
        b"bytes-id",
    ],
)
def test_extract_request_id_non_string_types(invalid_id: Any) -> None:
    """Rechaza silenciosamente tipos de id no string."""
    payload = {"data": {"id": invalid_id}}
    assert extract_request_id(payload) is None


@pytest.mark.parametrize(
    "invalid_uuid_str",
    [
        "",
        "   ",
        "123",
        "g4b3c2a1-0000-4000-8000-0123456789ab",  # 'g' no es carácter hexadecimal válido
        "e4b3c2a1-0000-4000-8000",  # Incompleto / truncado
        "e4b3c2a1-0000-4000-8000-0123456789ab-extra",  # Longitud excesiva
        "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",
    ],
)
def test_extract_request_id_invalid_uuid_formats(invalid_uuid_str: str) -> None:
    """Rechaza silenciosamente cadenas que no corresponden a un UUID RFC 4122 válido."""
    payload = {"data": {"id": invalid_uuid_str}}
    assert extract_request_id(payload) is None


@pytest.mark.parametrize(
    "bad_data",
    [
        None,
        "not-a-dict",
        12345,
        [{"id": VALID_UUID_V4}],
        True,
    ],
)
def test_extract_request_id_non_dict_data_field(bad_data: Any) -> None:
    """Rechaza si el campo data no es un diccionario."""
    payload = {"data": bad_data}
    assert extract_request_id(payload) is None


@pytest.mark.parametrize(
    "primitive_payload",
    [
        None,
        123,
        45.67,
        True,
        False,
        [{"data": {"id": VALID_UUID_V4}}],
        object(),
    ],
)
def test_extract_request_id_primitive_payloads(primitive_payload: Any) -> None:
    """Rechaza cualquier payload raíz que no sea ni dict ni string/bytes JSON."""
    assert extract_request_id(primitive_payload) is None


@pytest.mark.parametrize(
    "malformed_json_str",
    [
        "{",
        "{unquoted_key: 1}",
        "[1, 2, 3]",
        '"hello"',
        "12345",
        "true",
        "null",
    ],
)
def test_extract_request_id_malformed_or_non_object_json_strings(
    malformed_json_str: str,
) -> None:
    """Rechaza cadenas JSON malformadas o que no decodifican en un objeto raíz."""
    assert extract_request_id(malformed_json_str) is None
