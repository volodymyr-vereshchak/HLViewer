"""
Factory for the reference-type CRUD routers (/sys-types/, /edit-types/).

SysType and EditType are structurally identical (numeric device code +
human-readable name, scoped by gas_volume_calc_type_id) and their routers were
copy-pasted line for line. This factory builds one from the model/DAO pair and
the two field names that differ.
"""

from typing import Optional, Type

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import create_model
from sqlalchemy import String, cast, func, or_
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.engine import async_session_factory


def make_type_router(
    *,
    prefix: str,
    tag: str,
    model: type,
    dao_cls: type,
    create_model_cls: type,
    update_model_cls: type,
    list_model_cls: type,
    type_id_field: str,
    name_field: str,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag], dependencies=[Depends(get_current_user)])

    type_id_col = getattr(model, type_id_field)
    name_col = getattr(model, name_field)
    not_found = f"{model.__name__} not found"

    # Concrete response model per router (FastAPI needs a real class, not a Generic)
    paged_model = create_model(
        f"Paged{model.__name__}s", total=(int, ...), items=(list[list_model_cls], ...)
    )

    @router.get("/", response_model=paged_model)
    async def get_types(
        calc_type_id: Optional[int] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ):
        async with async_session_factory() as session:
            base_q = select(model)
            if calc_type_id is not None:
                base_q = base_q.where(model.gas_volume_calc_type_id == calc_type_id)
            if search:
                base_q = base_q.where(or_(
                    name_col.ilike(f"%{search}%"),
                    cast(type_id_col, String).ilike(f"%{search}%"),
                ))

            total = (
                await session.execute(select(func.count()).select_from(base_q.subquery()))
            ).scalar_one()
            result = await session.execute(
                base_q.order_by(model.gas_volume_calc_type_id, type_id_col)
                .offset(skip).limit(limit)
            )
            items = result.scalars().all()

        return {"total": total, "items": items}

    @router.post("/", response_model=list_model_cls, status_code=status.HTTP_201_CREATED)
    async def create_type(body: create_model_cls):
        try:
            async with async_session_factory() as session:
                entry = await dao_cls(session=session).create_item(body)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return entry

    @router.patch("/{type_db_id}", response_model=list_model_cls)
    async def update_type(type_db_id: int, body: update_model_cls):
        async with async_session_factory() as session:
            entry = await dao_cls(session=session).update_by_id(type_db_id, body)
        if not entry:
            raise HTTPException(status_code=404, detail=not_found)
        return entry

    @router.delete("/{type_db_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_type(type_db_id: int):
        async with async_session_factory() as session:
            deleted = await dao_cls(session=session).delete_item(type_db_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=not_found)

    return router
