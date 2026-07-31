from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_allowed_line_ids
from backend.db.engine import get_session


class BaseArchiveRouter:
    def __init__(self, path: str, archive_list_class, tags: list[str], archive_dao):
        self.router = APIRouter()
        self.archive_dao = archive_dao
        self.router.add_api_route(
            path=path,
            endpoint=self.get_archive,
            response_model=list[archive_list_class],
            tags=tags,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path=path[:-1] + "_counts/",
            endpoint=self.get_archive_counts,
            tags=tags,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path=path[:-1] + "_last_period/",
            endpoint=self.get_last_period,
            tags=tags,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )

    @staticmethod
    def _check_dates(from_date, to_date):
        """Both ends are required; the length is not capped.

        There used to be a per-endpoint day limit (90 / 400 / 30, depending on
        which router you happened to be in). It rejected periods that are
        perfectly reasonable to ask for, and the client — which pages the table
        and knows what it is about to draw — is the one that should decide what
        is too much to request."""
        if not from_date or not to_date:
            raise HTTPException(status_code=400, detail="Вкажіть початок і кінець періоду")

    @staticmethod
    def _scope_line_ids(line_id, allowed_line_ids):
        """Restrict the requested line_ids to the ones this user may see."""
        if allowed_line_ids is not None:
            allowed_set = set(allowed_line_ids)
            return [lid for lid in line_id if lid in allowed_set] if line_id else allowed_line_ids
        return line_id

    @staticmethod
    def _scope_is_empty(line_id) -> bool:
        """True when scoping left the user with NO permitted lines. Callers must
        return an empty result then — passing [] into the DAO would be read as
        "no filter" and leak every branch's data."""
        return line_id is not None and not line_id

    async def get_archive(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
        allowed_line_ids: list[int] | None = Depends(get_allowed_line_ids),
        session: AsyncSession = Depends(get_session),
    ):
        self._check_dates(from_date, to_date)
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        if self._scope_is_empty(line_id):
            return []
        archive_dao = self.archive_dao(session=session)
        archives = await archive_dao.get_range(from_date, to_date, line_id)
        return archives

    async def get_last_period(
        self,
        line_id: list[int] = Query(None),
        session: AsyncSession = Depends(get_session),
    ):
        from sqlalchemy import func
        from sqlmodel import select
        dao = self.archive_dao(session=session)
        stmt = select(func.max(dao.model.period))
        if line_id:
            stmt = stmt.where(dao.model.line_id.in_(line_id))
        result = await session.execute(stmt)
        period = result.scalar()
        return {"last_period": period.isoformat() if period else None}

    async def get_archive_counts(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
        session: AsyncSession = Depends(get_session),
    ):
        # The hourly view asks for the И/А counters over the same range as the
        # archive, so it answers the same way when a date is missing instead of
        # quietly counting the whole table.
        self._check_dates(from_date, to_date)
        return await self.archive_dao(session=session).get_data_counts_by_hour(
            from_date=from_date, to_date=to_date, line_id=line_id
        )
