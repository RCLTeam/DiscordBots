"""Match declarative model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liga_bot.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from liga_bot.models.enums import (
    Division,
    MatchStatus,
    ensure_division_assignable,
    resolve_entity_division,
)

if TYPE_CHECKING:
    from liga_bot.models.caster import MatchCaster, MatchCasterCard
    from liga_bot.models.roster import SeasonDivision
    from liga_bot.models.team import Team

DEFAULT_PREMIER_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
DEFAULT_ASCEND_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")


class Match(Base, UUIDPrimaryKeyMixin, TimestampMixin, UpdatedAtMixin):
    """Representa un enfrentamiento programado entre dos equipos en una jornada."""

    __tablename__ = "matches"

    jornada: Mapped[int] = mapped_column(Integer, nullable=False)
    # Jornada del lado web (rounds.id). La gobierna RCL-Next: el bot solo la enlaza
    # cuando ya existe, nunca la crea.
    id_round: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    id_season_division: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "seasons_divisions.id",
            name="fk_matches_id_season_division",
            ondelete="CASCADE",
        ),
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
        Enum(
            MatchStatus,
            name="match_status",
            values_callable=lambda obj: [e.value for e in obj],
            native_enum=True,
        ),
        default=MatchStatus.SCHEDULED,
        server_default="scheduled",
        nullable=False,
    )
    stream_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    stream_url_live: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # Relación con la división de temporada
    season_division: Mapped[SeasonDivision] = relationship(
        "SeasonDivision",
        foreign_keys=[id_season_division],
        lazy="joined",
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

    # Relaciones con casters y tarjetas de publicación
    casters: Mapped[list[MatchCaster]] = relationship(
        "MatchCaster",
        back_populates="match",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    caster_cards: Mapped[list[MatchCasterCard]] = relationship(
        "MatchCasterCard",
        back_populates="match",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("jornada", "team1_id", "team2_id", name="uq_matches_jornada_teams"),
        CheckConstraint("team1_id != team2_id", name="ck_matches_distinct_teams"),
        Index("ix_matches_jornada", "jornada"),
        Index("ix_matches_status", "status"),
        Index("ix_matches_id_season_division", "id_season_division"),
    )

    def __init__(
        self,
        *args: Any,
        division: Division | str | None = None,
        **kwargs: Any,
    ) -> None:
        """Inicializador con soporte para argumento 'division' legacy."""
        kwargs.pop("division", None)
        if "id_season_division" not in kwargs:
            if Division.from_name(division) == Division.ASCEND:
                kwargs["id_season_division"] = DEFAULT_ASCEND_SEASON_DIVISION_ID
            else:
                kwargs["id_season_division"] = DEFAULT_PREMIER_SEASON_DIVISION_ID

        super().__init__(*args, **kwargs)

        if division is not None:
            self.division = division

    @property
    def division(self) -> Division:
        """Resuelve la división a partir de season_division (sin distinguir mayúsculas)."""
        return resolve_entity_division(
            self.season_division,
            getattr(self, "_division_override", None),
            self,
        )

    @division.setter
    def division(self, value: Division | str | None) -> None:
        # Nunca escribe en seasons_divisions: es una tabla compartida que gestiona la web.
        self._division_override = ensure_division_assignable(self.season_division, value)
