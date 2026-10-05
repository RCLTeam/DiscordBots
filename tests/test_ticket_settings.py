"""
Pruebas de los ajustes de la auditoría de tickets: carga desde Settings y su
reflejo en el resumen de /revisar-tickets.
"""

from unittest.mock import MagicMock

import discord
import pytest
from discord.ext import commands
from pydantic import ValidationError

from liga_bot.cogs.tickets import TicketsCog
from liga_bot.config import DEFAULT_TICKET_REVISION_HOURS, Settings
from liga_bot.services.ticket_service import TicketAuditResult


def test_ticket_settings_defaults():
    """Los ajustes de auditoría conservan los valores que el servicio aplicaba de hecho."""
    settings = Settings()
    assert settings.organizador_role_id == 0
    assert settings.ticket_revision_hours == DEFAULT_TICKET_REVISION_HOURS == 24
    assert settings.tickets_category_name == ""


def test_ticket_settings_read_from_environment(monkeypatch):
    """Las variables de entorno de la auditoría de tickets se aplican."""
    monkeypatch.setenv("ORGANIZADOR_ROLE_ID", "1550000000000000000")
    monkeypatch.setenv("TICKET_REVISION_HOURS", "48")
    monkeypatch.setenv("TICKETS_CATEGORY_NAME", "SOPORTE-EXTRA")

    settings = Settings()
    assert settings.organizador_role_id == 1550000000000000000
    assert settings.ticket_revision_hours == 48
    assert settings.tickets_category_name == "SOPORTE-EXTRA"


@pytest.mark.parametrize("hours", [0, -1])
def test_ticket_revision_hours_must_be_positive(hours: int):
    """Un umbral de 0 h o negativo avisaría en cada pasada; se rechaza al cargar."""
    with pytest.raises(ValidationError):
        Settings(ticket_revision_hours=hours)


def test_audit_embed_shows_configured_threshold():
    """El resumen de /revisar-tickets muestra el umbral configurado, no 24 h fijas."""
    cog = TicketsCog(MagicMock(spec=commands.Bot), ticket_service=MagicMock(), auto_start=False)
    user = MagicMock(spec=discord.Member)
    user.id = 12345
    user.display_name = "StaffUser"
    user.mention = "<@12345>"

    embed = cog._build_audit_embed(TicketAuditResult(revision_hours=48), user)

    field = next(f for f in embed.fields if f.name == "⏭️ Canales Descartados / En Regla")
    assert "Activos (<48h)" in field.value
    assert "Ya notificados (<48h)" in field.value
    assert "24h" not in field.value
