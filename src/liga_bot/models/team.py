"""Team declarative model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liga_bot.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from liga_bot.models.enums import Division

if TYPE_CHECKING:
    from liga_bot.models.match import Match


class Team(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Representa un equipo participante en la liga."""

    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    tag: Mapped[str] = mapped_column(String(4), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    division: Mapped[Division] = mapped_column(
        Enum(Division, name="division", native_enum=True),
        nullable=False,
    )
    discord_role_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    # Relaciones bidireccionales con partidos
    home_matches: Mapped[list[Match]] = relationship(
        "Match",
        foreign_keys="[Match.team1_id]",
        back_populates="team1",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    away_matches: Mapped[list[Match]] = relationship(
        "Match",
        foreign_keys="[Match.team2_id]",
        back_populates="team2",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint("char_length(tag) <= 4", name="ck_teams_tag_length"),
        Index("ix_teams_slug", "slug"),
        Index("ix_teams_division", "division"),
    )
