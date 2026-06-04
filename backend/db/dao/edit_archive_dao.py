from datetime import datetime

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import (
    EditArchive,
    EditType,
    Line,
    GasVolumeCalc,
    GasVolumeCalcType,
)


class EditArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = EditArchive

    async def get_range(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ):
        statement = (
            select(self.model, EditType)
            .outerjoin(Line, self.model.line_id == Line.id)
            .outerjoin(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .outerjoin(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .outerjoin(
                EditType,
                (GasVolumeCalcType.type_id == EditType.gas_volume_calc_type_id)
                & (self.model.edit_type_id == EditType.edit_type_id),
            )
        )
        if from_date:
            statement = statement.where(self.model.period >= from_date)
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        query = await self.session.execute(statement)
        result = []
        for edit_archive, edit_type in query.all():
            row = edit_archive.model_dump()
            row["edit_name"] = (
                edit_type.edit_name
                if edit_type
                else f"Неизвестный код {row['edit_type_id']}"
            )
            result.append(row)
        return result

    # Columns the table is allowed to sort on, server-side (maps API name → ORM column).
    _ORDER_COLUMNS = {
        "period": EditArchive.period,
        "edit_name": EditType.edit_name,
        "old_value": EditArchive.old_value,
        "new_value": EditArchive.new_value,
    }

    @staticmethod
    def _build_paged_rows(rows):
        result = []
        for edit_archive, edit_type in rows:
            row = edit_archive.model_dump()
            row["edit_name"] = (
                edit_type.edit_name
                if edit_type
                else f"Неизвестный код {row['edit_type_id']}"
            )
            result.append(row)
        return result

    async def get_range_paged(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
        skip: int = 0,
        limit: int = 50,
        order_by: str = "period",
        order_dir: str = "asc",
    ) -> dict:
        """Paginated variant of get_range: returns {"total": int, "items": [...]}.
        Used by the edit-archive table view; full get_range stays for callers
        that need every row."""
        filters = []
        if from_date:
            filters.append(self.model.period >= from_date)
        if to_date:
            filters.append(self.model.period <= to_date)
        if line_id:
            filters.append(self.model.line_id.in_(line_id))

        count_stmt = select(func.count()).select_from(self.model)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = (await self.session.execute(count_stmt)).scalar_one()

        statement = (
            select(self.model, EditType)
            .outerjoin(Line, self.model.line_id == Line.id)
            .outerjoin(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .outerjoin(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .outerjoin(
                EditType,
                (GasVolumeCalcType.type_id == EditType.gas_volume_calc_type_id)
                & (self.model.edit_type_id == EditType.edit_type_id),
            )
        )
        for f in filters:
            statement = statement.where(f)

        order_col = self._ORDER_COLUMNS.get(order_by, self.model.period)
        statement = statement.order_by(
            order_col.desc() if order_dir == "desc" else order_col.asc()
        )
        statement = statement.offset(max(skip, 0)).limit(max(min(limit, 500), 1))

        rows = (await self.session.execute(statement)).all()
        return {"total": total, "items": self._build_paged_rows(rows)}


if __name__ == "__main__":
    import asyncio
    from backend.db.engine import async_session_factory

    async def get_range():
        async with async_session_factory() as session:
            result = await EditArchiveDao(session).get_range()
            print(result)

    asyncio.run(get_range())
