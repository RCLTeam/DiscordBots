"""Caster declarative models and role enum for Discord panel and stream exclusivity."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liga_bot.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from liga_bot.models.match import Match


class CasterRole(str, enum.Enum):
    """Rol de casteo y retransmisión para los partidos de la liga."""

    CASTER = "CASTER"
    STREAMER = "STREAMER"
    BOTH = "BOTH"


class MatchCaster(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Representa la asignación de un caster o streamer a un partido específico."""

    __tablename__ = "match_casters"

    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    caster_role: Mapped[CasterRole] = mapped_column(
        Enum(CasterRole, name="caster_role", native_enum=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    match: Mapped[Match] = relationship(
        "Match",
        back_populates="casters",
    )

    __table_args__ = (
        UniqueConstraint("match_id", "discord_user_id", name="uq_match_casters_match_user"),
        Index("ix_match_casters_match_id", "match_id"),
        Index("ix_match_casters_discord_user_id", "discord_user_id"),
        Index(
            "uq_match_casters_single_streamer",
            "match_id",
            unique=True,
            postgresql_where=text("caster_role IN ('STREAMER', 'BOTH')"),
        ),
    )


class MatchCasterCard(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Registra las tarjetas publicadas de un partido en canales de Discord
    para evitar duplicados.
    """

    __tablename__ = "match_caster_cards"

    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    match: Mapped[Match] = relationship(
        "Match",
        back_populates="caster_cards",
    )

    __table_args__ = (
        UniqueConstraint("match_id", "channel_id", name="uq_match_caster_cards_match_channel"),
        Index("ix_match_caster_cards_match_id", "match_id"),
        Index("ix_match_caster_cards_channel_id", "channel_id"),
    )
