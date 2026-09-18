"""Match declarative model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liga_bot.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from liga_bot.models.enums import Division, MatchStatus

if TYPE_CHECKING:
    from liga_bot.models.team import Team


class Match(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Representa un enfrentamiento programado entre dos equipos en una jornada."""

    __tablename__ = "matches"

    jornada: Mapped[int] = mapped_column(Integer, nullable=False)
    division: Mapped[Division] = mapped_column(
        Enum(Division, name="division", native_enum=True),
        nullable=False,
    )
    team1_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", name="fk_matches_team1_id", ondelete="CASCADE"),
        nullable=False,
    )
    team2_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", name="fk_matches_team2_id", ondelete="CASCADE"),
        nullable=False,
    )
    discord_channel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        unique=True,
        nullable=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="matchstatus", native_enum=True),
        default=MatchStatus.PENDIENTE,
        server_default="PENDIENTE",
        nullable=False,
    )

    # Relaciones eager seguras para contextos asíncronos
    team1: Mapped[Team] = relationship(
        "Team",
        foreign_keys=[team1_id],
        back_populates="home_matches",
        lazy="selectin",
    )
    team2: Mapped[Team] = relationship(
        "Team",
        foreign_keys=[team2_id],
        back_populates="away_matches",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("jornada", "team1_id", "team2_id", name="uq_matches_jornada_teams"),
        CheckConstraint("team1_id != team2_id", name="ck_matches_distinct_teams"),
        Index("ix_matches_jornada", "jornada"),
        Index("ix_matches_status", "status"),
    )
