"""Limpieza defensiva de identificadores antes de consultar la base de datos.

Los repositorios y servicios de casters y plantillas reciben identificadores desde
Discord, desde la base de datos o como texto. Estas funciones los normalizan y
devuelven ``None`` cuando el valor no es válido, en lugar de dejar que PostgreSQL
rechace la consulta.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


def clean_uuid(val: Any) -> UUID | None:
    """Convierte un valor a ``UUID`` o devuelve ``None`` si no es un UUID válido.

    Las cadenas se recortan antes de convertirlas; cualquier otro objeto se convierte
    a partir de su representación ``str``.
    """
    if val is None:
        return None
    if isinstance(val, UUID):
        return val
    text = val.strip() if isinstance(val, str) else str(val)
    try:
        return UUID(text)
    except (ValueError, AttributeError, TypeError):
        return None


def clean_user_id_int(val: Any) -> int | None:
    """Convierte un ID de usuario de Discord a entero positivo o devuelve ``None``.

    Descarta booleanos, ceros, negativos y cadenas vacías o no numéricas. Es la forma
    que usan las columnas ``BigInteger`` de casters.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned:
            return None
        try:
            val_int = int(cleaned)
            return val_int if val_int > 0 else None
        except ValueError:
            return None
    try:
        val_int = int(val)
        return val_int if val_int > 0 else None
    except (ValueError, TypeError):
        return None


def clean_user_id_str(val: str | int | None) -> str | None:
    """Convierte un ID de usuario de Discord a texto sin espacios o devuelve ``None``.

    Solo recorta espacios: no valida que el texto sea numérico. Es la forma que usan
    las columnas de texto de plantillas y auditoría (``discord_user_id``).
    """
    if val is None:
        return None
    cleaned = str(val).strip()
    return cleaned if cleaned else None
