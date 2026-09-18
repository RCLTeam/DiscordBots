"""Declarative Base and common mixins for LigaBot SQLAlchemy 2.0 models."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Naming convention matching Alembic 001_initial_schema.py
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarativa con convenciones de nomenclatura y __repr__ asíncrono seguro."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """Representación segura en entornos asíncronos que no dispara cargas diferidas."""
        cols: list[str] = []
        for col in self.__table__.columns:
            if col.name in self.__dict__:
                cols.append(f"{col.name}={self.__dict__[col.name]!r}")
            else:
                cols.append(f"{col.name}=<unloaded>")
        return f"<{self.__class__.__name__}({', '.join(cols)})>"


class UUIDPrimaryKeyMixin:
    """Mixin que proporciona clave primaria UUID universal (v4)."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    """Mixin que proporciona marca temporal created_at en zona horaria UTC."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
