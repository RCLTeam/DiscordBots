"""Base repository providing generic async CRUD operations."""

from collections.abc import Sequence
from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import Uuid, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    """
    Repositorio base genérico que provee operaciones CRUD comunes asíncronas
    usando SQLAlchemy 2.0 AsyncSession.
    """

    def __init__(self, session: AsyncSession, model_cls: type[ModelT]) -> None:
        self._session = session
        self._model_cls = model_cls
        mapper = inspect(model_cls)
        self._pk_columns = mapper.primary_key
        self._is_single_uuid_pk = len(self._pk_columns) == 1 and isinstance(
            self._pk_columns[0].type, Uuid
        )

    @property
    def session(self) -> AsyncSession:
        """Sesión asíncrona activa asociada al repositorio."""
        return self._session

    @property
    def model_cls(self) -> type[ModelT]:
        """Clase de modelo gestionada por este repositorio."""
        return self._model_cls

    async def get_by_id(self, entity_id: UUID | int | str) -> ModelT | None:
        """
        Obtiene una entidad por su clave primaria (UUID o BigInteger).
        Retorna None si no existe o si el identificador no tiene un formato válido.
        """
        target_id: Any = entity_id
        if self._is_single_uuid_pk:
            if isinstance(entity_id, UUID):
                target_id = entity_id
            elif isinstance(entity_id, str):
                try:
                    target_id = UUID(entity_id)
                except ValueError:
                    return None
            else:
                return None
        elif isinstance(entity_id, str):
            try:
                target_id = UUID(entity_id)
            except ValueError:
                target_id = entity_id

        return await self._session.get(self._model_cls, target_id, populate_existing=True)

    async def list_all(self) -> Sequence[ModelT]:
        """Obtiene todas las entidades registradas del modelo."""
        stmt = select(self._model_cls)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count(self) -> int:
        """Retorna el número total de entidades del modelo."""
        stmt = select(func.count()).select_from(self._model_cls)
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def create(self, entity: ModelT | None = None, **kwargs: Any) -> ModelT:
        """
        Persiste una nueva entidad en la base de datos.
        Acepta una instancia existente o argumentos para instanciarla.
        Ejecuta flush y refresh para obtener IDs y valores por defecto.
        """
        if entity is None:
            entity = self._model_cls(**kwargs)
        self._session.add(entity)
        await self._session.flush()
        await self._session.refresh(entity)
        return entity

    async def update(self, entity: ModelT, **kwargs: Any) -> ModelT:
        """
        Actualiza los campos indicados de una entidad existente y sincroniza el estado.
        """
        for key, value in kwargs.items():
            if hasattr(entity, key):
                setattr(entity, key, value)
        await self._session.flush()
        await self._session.refresh(entity)
        return entity

    async def delete(self, entity: ModelT) -> None:
        """Elimina la entidad de la sesión y sincroniza con flush."""
        await self._session.delete(entity)
        await self._session.flush()

    async def delete_by_id(self, entity_id: UUID | int | str) -> bool:
        """
        Elimina una entidad por su identificador primario.
        Retorna True si fue encontrada y eliminada, False si no existía.
        """
        entity = await self.get_by_id(entity_id)
        if entity is None:
            return False
        await self.delete(entity)
        return True
