"""Módulo de validación de protocolo y extracción de identificadores UUID."""

from __future__ import annotations

import json
import uuid
from typing import Any


def extract_request_id(payload: Any) -> str | None:
    """Extrae y valida un UUID de correlación del payload.

    Acepta un diccionario parseado o una cadena/bytes JSON sin procesar.
    Busca el identificador en data.content.id o en data.id (fallback).
    Si no existe, no es un diccionario o no tiene formato UUID válido (RFC 4122), devuelve None.
    Normaliza el resultado a formato canónico estándar en minúsculas con guiones.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    raw_id: Any = None
    content = data.get("content")
    if isinstance(content, dict) and content.get("id") is not None:
        raw_id = content.get("id")
    else:
        raw_id = data.get("id")

    if not isinstance(raw_id, str):
        return None

    cleaned_id = raw_id.strip()
    if not cleaned_id:
        return None

    try:
        parsed_uuid = uuid.UUID(cleaned_id)
        return str(parsed_uuid)
    except (ValueError, TypeError, AttributeError):
        return None
