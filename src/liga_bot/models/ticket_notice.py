"""TicketNotice declarative model."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from liga_bot.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TicketNotice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Control de estado y auditoría de inactividad para canales de tickets."""

    __tablename__ = "ticket_notices"

    discord_channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    category_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_staff_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_alert_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_pending_staff: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=sa.text("false"),
        nullable=False,
    )
