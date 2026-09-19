"""RoleRequest declarative model and lifecycle status enum."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from liga_bot.models.base import Base
from liga_bot.models.enums import RoleRequestStatus


class RoleRequest(Base):
    """Representa una solicitud de verificación y vinculación de rol de equipo."""

    __tablename__ = "role_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nombre_lol: Mapped[str] = mapped_column(String(100), nullable=False)
    riot_tag: Mapped[str] = mapped_column(String(20), nullable=False)
    equipo: Mapped[str] = mapped_column(String(100), nullable=False)
    canal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estado: Mapped[RoleRequestStatus] = mapped_column(
        Enum(RoleRequestStatus, name="rolerequeststatus", native_enum=True),
        default=RoleRequestStatus.PENDING,
        server_default="PENDING",
        nullable=False,
    )
    staff_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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

    __table_args__ = (
        Index("ix_role_requests_user_id", "user_id"),
        Index("ix_role_requests_canal_id", "canal_id"),
        Index("ix_role_requests_estado", "estado"),
    )

    def __init__(self, **kwargs) -> None:
        """Inicializa RoleRequest garantizando estado por defecto PENDING."""
        kwargs.setdefault("estado", RoleRequestStatus.PENDING)
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        """Representación segura para depuración en entornos asíncronos sin cargas diferidas."""
        d = self.__dict__
        req_id = d.get("id", getattr(self, "id", None))
        user_id = d.get("user_id", "<unloaded>")
        nombre_lol = d.get("nombre_lol", "<unloaded>")
        riot_tag = d.get("riot_tag", "<unloaded>")
        equipo = d.get("equipo", "<unloaded>")
        estado = d.get("estado", "<unloaded>")
        return (
            f"<RoleRequest(id={req_id!r}, user_id={user_id!r}, "
            f"nombre_lol={nombre_lol!r}, riot_tag={riot_tag!r}, "
            f"equipo={equipo!r}, estado={estado!r})>"
        )


__all__ = [
    "RoleRequest",
    "RoleRequestStatus",
]
