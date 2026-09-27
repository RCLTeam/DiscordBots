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
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liga_bot.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from liga_bot.models.enums import Division, MatchStatus

if TYPE_CHECKING:
    from liga_bot.models.roster import SeasonDivision
    from liga_bot.models.team import Team

DEFAULT_PREMIER_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
DEFAULT_ASCEND_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")


class Match(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Representa un enfrentamiento programado entre dos equipos en una jornada."""

    __tablename__ = "matches"

    jornada: Mapped[int] = mapped_column(Integer, nullable=False)
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
            if division is not None and (
                division == Division.ASCEND or str(division).upper() == "ASCEND"
            ):
                kwargs["id_season_division"] = DEFAULT_ASCEND_SEASON_DIVISION_ID
            else:
                kwargs["id_season_division"] = DEFAULT_PREMIER_SEASON_DIVISION_ID

        super().__init__(*args, **kwargs)

        if division is not None:
            self.division = division

    @property
    def division(self) -> Division:
        """Resuelve dinámicamente la división competitiva mediante season_division."""
        sd = self.season_division
        if sd is not None and getattr(sd, "division_name", None):
            try:
                return Division(sd.division_name)
            except ValueError:
                pass
        if hasattr(self, "_division_override") and self._division_override is not None:
            return self._division_override
        return Division.PREMIER

    @division.setter
    def division(self, value: Division | str | None) -> None:
        div_val: Division | None
        if value is None:
            div_val = None
        elif isinstance(value, Division):
            div_val = value
        else:
            div_val = Division(str(value))
        self._division_override = div_val
        if self.season_division is not None and div_val is not None:
            self.season_division.division_name = div_val.value
