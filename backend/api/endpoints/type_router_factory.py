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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.engine import get_session
from backend.db.models import GasVolumeCalc, GasVolumeCalcType, Line

# Beyond this many archive rows the exact number stops being informative and
# starts being expensive: the count walks a join over three tables.
USAGE_CAP = 1000


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
    archive_model: type,
    archive_code_field: str,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag], dependencies=[Depends(get_current_user)])

    type_id_col = getattr(model, type_id_field)
    name_col = getattr(model, name_field)
    not_found = f"{model.__name__} not found"

    def conflict(exc: Exception) -> str:
        """The only integrity error these tables can raise is the unique pair.
        Said in words, because this is read in the admin panel by someone who
        mistyped a code, not in a log by someone debugging Postgres."""
        text = str(exc)
        if "constraint" in text:
            return "Подія з таким кодом для цього типу обчислювача вже існує"
        return text

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
        session: AsyncSession = Depends(get_session),
    ):
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
    async def create_type(body: create_model_cls, session: AsyncSession = Depends(get_session)):
        try:
            entry = await dao_cls(session=session).create_item(body)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=conflict(e))
        return entry

    @router.patch("/{type_db_id}", response_model=list_model_cls)
    async def update_type(
        type_db_id: int, body: update_model_cls, session: AsyncSession = Depends(get_session)
    ):
        try:
            entry = await dao_cls(session=session).update_by_id(type_db_id, body)
        except IntegrityError as e:
            # Moving a row onto a (code, calculator type) pair that already
            # exists. The DAO does not translate this one — without the catch
            # the admin panel gets a 500 for an ordinary typo.
            raise HTTPException(status_code=409, detail=conflict(e.orig))
        if not entry:
            raise HTTPException(status_code=404, detail=not_found)
        return entry

    @router.delete("/{type_db_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_type(type_db_id: int, session: AsyncSession = Depends(get_session)):
        try:
            deleted = await dao_cls(session=session).delete_item(type_db_id)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=conflict(e))
        if not deleted:
            raise HTTPException(status_code=404, detail=not_found)

    @router.get("/{type_db_id}/usage")
    async def type_usage(type_db_id: int, session: AsyncSession = Depends(get_session)):
        """How many archive rows carry this event code.

        Nothing references these dictionaries by foreign key — the archive
        stores the bare code and the name is joined in at read time — so
        deleting a type is allowed. It just leaves those rows showing a number
        instead of a name, and this is what lets the confirmation say so.

        The code alone is not the identity: the same number means different
        events on different calculator types, so the count follows the same
        join the archive itself uses (archive → line → calc → calc type code).
        Capped, because the answer only has to be "none" or "a lot".
        """
        entry = await session.get(model, type_db_id)
        if not entry:
            raise HTTPException(status_code=404, detail=not_found)

        archive_code = getattr(archive_model, archive_code_field)
        limited = (
            select(archive_model.id)
            .join(Line, archive_model.line_id == Line.id)
            .join(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .join(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .where(archive_code == getattr(entry, type_id_field))
            .where(GasVolumeCalcType.type_id == entry.gas_volume_calc_type_id)
            .limit(USAGE_CAP + 1)
            .subquery()
        )
        rows = (
            await session.execute(select(func.count()).select_from(limited))
        ).scalar_one()
        return {"archive_rows": rows, "capped": rows > USAGE_CAP}

    return router
