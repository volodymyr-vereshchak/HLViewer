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

    def _filters(self, from_date, to_date, line_id, type_id=None):
        """The WHERE clauses every edit read shares. `type_id` is the event CODE
        (edit_type_id), which is what the table's type filter selects — one code
        can print under several labels once the %s channel is substituted, but
        it is still one kind of intervention."""
        filters = []
        if from_date:
            filters.append(self.model.period >= from_date)
        if to_date:
            filters.append(self.model.period <= to_date)
        if line_id:
            filters.append(self.model.line_id.in_(line_id))
        if type_id:
            filters.append(self.model.edit_type_id.in_(type_id))
        return filters

    def _range_statement(self):
        """Event rows plus the name and calculator type that decode them."""
        return (
            select(self.model, EditType, GasVolumeCalcType.type_id)
            .outerjoin(Line, self.model.line_id == Line.id)
            .outerjoin(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .outerjoin(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .outerjoin(
                EditType,
                (GasVolumeCalcType.type_id == EditType.gas_volume_calc_type_id)
                & (self.model.edit_type_id == EditType.edit_type_id),
            )
        )

    async def get_range(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
        type_id: list[int] = None,
    ):
        statement = self._range_statement()
        for f in self._filters(from_date, to_date, line_id, type_id):
            statement = statement.where(f)

        query = await self.session.execute(statement)
        return self._build_paged_rows(query.all())

    async def get_type_counts(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ) -> list[dict]:
        """Which intervention codes actually occur in this period, and how often.

        Feeds the table's type filter — see SysArchiveDao.get_type_counts for
        why this reads the archive and not the EditType dictionary."""
        statement = (
            select(
                self.model.edit_type_id,
                EditType.edit_name,
                func.count().label("count"),
            )
            .outerjoin(Line, self.model.line_id == Line.id)
            .outerjoin(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .outerjoin(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .outerjoin(
                EditType,
                (GasVolumeCalcType.type_id == EditType.gas_volume_calc_type_id)
                & (self.model.edit_type_id == EditType.edit_type_id),
            )
            .group_by(self.model.edit_type_id, EditType.edit_name)
            .order_by(self.model.edit_type_id)
        )
        for f in self._filters(from_date, to_date, line_id):
            statement = statement.where(f)
        rows = (await self.session.execute(statement)).all()
        return [
            {
                "type_id": type_id,
                "name": name if name is not None else f"Неизвестный код {type_id}",
                "count": count,
            }
            for type_id, name, count in rows
        ]

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
        for edit_archive, edit_type, calc_type_id in rows:
            row = edit_archive.model_dump()
            row["edit_name"] = (
                edit_type.edit_name
                if edit_type
                else f"Неизвестный код {row['edit_type_id']}"
            )
            row["gas_volume_calc_type_id"] = calc_type_id
            result.append(row)
        return result

    async def get_range_paged(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
        type_id: list[int] = None,
        skip: int = 0,
        limit: int = 50,
        order_by: str = "period",
        order_dir: str = "asc",
    ) -> dict:
        """Paginated variant of get_range: returns {"total": int, "items": [...]}.
        Used by the edit-archive table view; full get_range stays for callers
        that need every row."""
        filters = self._filters(from_date, to_date, line_id, type_id)

        count_stmt = select(func.count()).select_from(self.model)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = (await self.session.execute(count_stmt)).scalar_one()

        statement = self._range_statement()
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
