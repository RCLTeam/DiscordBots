"""
Declarative SQLAlchemy 2.0 models for shared RCL-Next tables.

These models map 1:1 to the Drizzle schema in RCL-Next:
1. discord_users
2. players
3. teams (re-exported from liga_bot.models.team to ensure zero metadata duplication)
4. team_memberships
5. roster_movements
6. audit_logs

GOVERNANCE:
These tables are owned exclusively by RCL-Next. In DiscordBots, they are used
for runtime queries and in-memory test/dev fixtures (Base.metadata.create_all).
They are STRICTLY EXCLUDED from Alembic migrations in production via include_object.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liga_bot.models.base import Base
from liga_bot.models.enums import AppRole, RosterMovementAction, RosterRole
from liga_bot.models.team import Team


class DiscordUser(Base):
    """Representa un usuario registrado en Discord (discord_users)."""

    __tablename__ = "discord_users"

    discord_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    global_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[AppRole] = mapped_column(
        Enum(
            AppRole,
            name="app_role",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
        default=AppRole.VIEWER,
        server_default="viewer",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relaciones
    players: Mapped[list[Player]] = relationship(
        "Player",
        back_populates="discord_user",
        passive_deletes=True,
    )
    memberships: Mapped[list[TeamMembership]] = relationship(
        "TeamMembership",
        back_populates="discord_user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    movements: Mapped[list[RosterMovement]] = relationship(
        "RosterMovement",
        foreign_keys="[RosterMovement.discord_user_id]",
        back_populates="discord_user",
        passive_deletes=True,
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        "AuditLog",
        foreign_keys="[AuditLog.actor_discord_user_id]",
        back_populates="actor",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        role_val = self.role.value if isinstance(self.role, AppRole) else self.role
        return (
            f"<DiscordUser discord_id={self.discord_id!r} "
            f"username={self.username!r} role={role_val!r}>"
        )


class Player(Base):
    """Cuenta de juego de League of Legends asociada o no a un usuario de Discord (players)."""

    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    discord_user_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey(
            "discord_users.discord_id",
            ondelete="SET NULL",
            name="players_discord_user_id_fkey",
        ),
        nullable=True,
    )
    game_name: Mapped[str] = mapped_column(String(64), nullable=False)
    riot_tag: Mapped[str | None] = mapped_column(String(16), nullable=True)
    puuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    is_main: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relaciones
    discord_user: Mapped[DiscordUser | None] = relationship(
        "DiscordUser",
        back_populates="players",
    )

    __table_args__ = (
        UniqueConstraint("game_name", "riot_tag", name="players_game_name_riot_tag_key"),
        Index("players_discord_user_id_idx", "discord_user_id"),
    )

    def __repr__(self) -> str:
        return f"<Player id={self.id!r} game_name={self.game_name!r} riot_tag={self.riot_tag!r}>"


class TeamMembership(Base):
    """Alineación actual y miembros de plantilla de un equipo (team_memberships)."""

    __tablename__ = "team_memberships"

    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="team_memberships_team_id_fkey"),
        primary_key=True,
    )
    discord_user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(
            "discord_users.discord_id",
            ondelete="CASCADE",
            name="team_memberships_discord_user_id_fkey",
        ),
        primary_key=True,
    )
    role: Mapped[RosterRole] = mapped_column(
        Enum(
            RosterRole,
            name="roster_role",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
    )
    is_captain: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relaciones
    team: Mapped[Team] = relationship("Team", back_populates="memberships")
    discord_user: Mapped[DiscordUser] = relationship("DiscordUser", back_populates="memberships")

    __table_args__ = (
        PrimaryKeyConstraint("team_id", "discord_user_id", name="team_memberships_pkey"),
        CheckConstraint(
            "is_captain = false OR role IN ('top', 'jungle', 'mid', 'adc', 'support')",
            name="team_memberships_captain_role_check",
        ),
        Index("team_memberships_team_id_idx", "team_id"),
        Index("team_memberships_discord_user_id_idx", "discord_user_id"),
        Index(
            "team_memberships_unique_captain",
            "team_id",
            unique=True,
            postgresql_where=sa.text("is_captain = true"),
        ),
    )

    def __repr__(self) -> str:
        role_val = self.role.value if isinstance(self.role, RosterRole) else self.role
        return (
            f"<TeamMembership team_id={self.team_id!r} "
            f"discord_user_id={self.discord_user_id!r} "
            f"role={role_val!r} is_captain={self.is_captain}>"
        )


class RosterMovement(Base):
    """Historial inmutable de movimientos, altas, bajas y cambios de rol (roster_movements)."""

    __tablename__ = "roster_movements"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="roster_movements_team_id_fkey"),
        nullable=False,
    )
    discord_user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(
            "discord_users.discord_id",
            ondelete="CASCADE",
            name="roster_movements_discord_user_id_fkey",
        ),
        nullable=False,
    )
    action: Mapped[RosterMovementAction] = mapped_column(
        Enum(
            RosterMovementAction,
            name="roster_movement_action",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
    )
    role: Mapped[RosterRole | None] = mapped_column(
        Enum(
            RosterRole,
            name="roster_role",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=True,
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey(
            "discord_users.discord_id",
            ondelete="SET NULL",
            name="roster_movements_actor_id_fkey",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relaciones
    team: Mapped[Team] = relationship("Team", back_populates="movements")
    discord_user: Mapped[DiscordUser] = relationship(
        "DiscordUser",
        foreign_keys=[discord_user_id],
        back_populates="movements",
    )
    actor: Mapped[DiscordUser | None] = relationship(
        "DiscordUser",
        foreign_keys=[actor_id],
    )

    __table_args__ = (
        Index("roster_movements_team_id_idx", "team_id"),
        Index("roster_movements_discord_user_id_idx", "discord_user_id"),
        Index("roster_movements_actor_id_idx", "actor_id"),
        Index("roster_movements_created_at_idx", "created_at"),
    )

    def __repr__(self) -> str:
        act_val = (
            self.action.value if isinstance(self.action, RosterMovementAction) else self.action
        )
        return (
            f"<RosterMovement id={self.id!r} team_id={self.team_id!r} "
            f"user={self.discord_user_id!r} action={act_val!r}>"
        )


class AuditLog(Base):
    """Registro de auditoría transaccional para cambios de estado y acciones (audit_logs)."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    actor_discord_user_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey(
            "discord_users.discord_id",
            ondelete="SET NULL",
            name="audit_logs_actor_discord_user_id_fkey",
        ),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relaciones
    actor: Mapped[DiscordUser | None] = relationship(
        "DiscordUser",
        foreign_keys=[actor_discord_user_id],
        back_populates="audit_logs",
    )

    __table_args__ = (
        Index("audit_logs_entity_idx", "entity_type", "entity_id"),
        Index("audit_logs_actor_discord_user_id_idx", "actor_discord_user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id!r} action={self.action!r} "
            f"entity_type={self.entity_type!r} entity_id={self.entity_id!r}>"
        )


# Acoplamiento bidireccional dinámico con el modelo Team existente
Team.memberships = relationship(
    "TeamMembership",
    back_populates="team",
    cascade="all, delete-orphan",
    passive_deletes=True,
)
Team.movements = relationship(
    "RosterMovement",
    back_populates="team",
    cascade="all, delete-orphan",
    passive_deletes=True,
)


__all__ = [
    "AuditLog",
    "DiscordUser",
    "Player",
    "RosterMovement",
    "Team",
    "TeamMembership",
]
