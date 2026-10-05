"""Team declarative model."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liga_bot.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from liga_bot.models.enums import Division

if TYPE_CHECKING:
    from liga_bot.models.match import Match
    from liga_bot.models.roster import SeasonDivision

DEFAULT_PREMIER_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
DEFAULT_ASCEND_SEASON_DIVISION_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")


class Team(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Representa un equipo participante en la liga."""

    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    tag: Mapped[str] = mapped_column("short_name", String(16), nullable=False)
    season_division_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("seasons_divisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    discord_role_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    # Relación con la división de temporada
    season_division: Mapped[SeasonDivision] = relationship("SeasonDivision", lazy="joined")

    # Relaciones bidireccionales con partidos
    home_matches: Mapped[list[Match]] = relationship(
        "Match",
        foreign_keys="[Match.team1_id]",
        back_populates="team1",
        passive_deletes=True,
        lazy="select",
    )
    away_matches: Mapped[list[Match]] = relationship(
        "Match",
        foreign_keys="[Match.team2_id]",
        back_populates="team2",
        passive_deletes=True,
        lazy="select",
    )

    __table_args__ = (Index("ix_teams_season_division_id", "season_division_id"),)

    def __init__(
        self,
        *args: Any,
        slug: str | None = None,
        division: Division | str | None = None,
        **kwargs: Any,
    ) -> None:
        """Inicializador con compatibilidad hacia atrás para tests y fixtures existentes."""
        kwargs.pop("slug", None)
        kwargs.pop("division", None)
        if "season_division_id" not in kwargs:
            if division is not None and (
                division == Division.ASCEND or str(division).upper() == "ASCEND"
            ):
                kwargs["season_division_id"] = DEFAULT_ASCEND_SEASON_DIVISION_ID
            else:
                kwargs["season_division_id"] = DEFAULT_PREMIER_SEASON_DIVISION_ID

        super().__init__(*args, **kwargs)

        if slug is not None:
            self._slug = slug
        if division is not None:
            self.division = division

    @property
    def slug(self) -> str:
        """Compatibilidad con tests y código legado: deriva del tag en minúsculas."""
        if hasattr(self, "_slug") and self._slug:
            return self._slug
        if self.tag:
            return self.tag.lower()
        return ""

    @slug.setter
    def slug(self, value: str | None) -> None:
        self._slug = value

    @property
    def division(self) -> Division:
        """Resuelve dinámicamente la división competitiva mediante season_division."""
        sd = self.season_division
        if sd is not None and getattr(sd, "division_name", None):
            try:
                # La web guarda 'Premier'/'Ascend'; el enum usa mayúsculas.
                return Division(str(sd.division_name).strip().upper())
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
