import asyncio
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import (
    SysArchive,
    SysType,
    GasVolumeCalc,
    Line,
    GasVolumeCalcType,
)


class SysArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = SysArchive

    async def get_range(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ):
        statement = (
            select(self.model, SysType)
            .outerjoin(Line, self.model.line_id == Line.id)
            .outerjoin(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .outerjoin(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .outerjoin(
                SysType,
                (GasVolumeCalcType.type_id == SysType.gas_volume_calc_type_id)
                & (self.model.sys_type_id == SysType.sys_type_id),
            )
        )
        if from_date:
            statement = statement.where(self.model.period >= from_date)
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        query = await self.session.execute(statement)
        rows = query.all()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._build_range_result, rows)

    @staticmethod
    def _build_range_result(rows):
        result = []
        for sys_archive, sys_type in rows:
            result.append({
                "id": sys_archive.id,
                "line_id": sys_archive.line_id,
                "period": sys_archive.period,
                "sys_type_id": sys_archive.sys_type_id,
                "volume": sys_archive.volume,
                "sys_name": sys_type.sys_name if sys_type else f"Неизвестный код {sys_archive.sys_type_id}",
            })
        return result


    async def get_grouped(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ) -> list[dict]:
        """Aggregate sys_archive by (line_id, sys_type_id): return min/max period and count.
        Python-side groups lines under each accident type."""
        statement = (
            select(
                self.model.line_id,
                Line.name.label("line_name"),
                self.model.sys_type_id,
                SysType.sys_name.label("sys_name"),
                func.min(self.model.period).label("first_seen"),
                func.max(self.model.period).label("last_seen"),
                func.count().label("event_count"),
            )
            .outerjoin(Line, self.model.line_id == Line.id)
            .outerjoin(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .outerjoin(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .outerjoin(
                SysType,
                (GasVolumeCalcType.type_id == SysType.gas_volume_calc_type_id)
                & (self.model.sys_type_id == SysType.sys_type_id),
            )
            .group_by(
                self.model.line_id,
                Line.name,
                self.model.sys_type_id,
                SysType.sys_name,
            )
        )
        if from_date:
            statement = statement.where(self.model.period >= from_date)
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        result = await self.session.execute(statement)
        rows = result.all()

        grouped: dict[tuple, dict] = {}
        for row in rows:
            sys_name = row.sys_name or f"Невідомий код {row.sys_type_id}"
            key = (row.sys_type_id, sys_name)
            if key not in grouped:
                grouped[key] = {
                    "sys_type_id": row.sys_type_id,
                    "sys_name": sys_name,
                    "first_seen": row.first_seen,
                    "last_seen": row.last_seen,
                    "total_events": 0,
                    "lines": [],
                }
            parent = grouped[key]
            if row.first_seen < parent["first_seen"]:
                parent["first_seen"] = row.first_seen
            if row.last_seen > parent["last_seen"]:
                parent["last_seen"] = row.last_seen
            parent["total_events"] += row.event_count
            parent["lines"].append({
                "line_id": row.line_id,
                "line_name": row.line_name or f"Лінія {row.line_id}",
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
                "event_count": row.event_count,
            })

        return sorted(grouped.values(), key=lambda x: x["sys_type_id"])


if __name__ == "__main__":
    # result = SysArchiveDao().get_data_counts_by_hour(line_id=[5])
    # print(result)
    pass
