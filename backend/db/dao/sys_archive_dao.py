import asyncio
import json
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


def _sys_name(sys_name: str | None, sys_type_id: int) -> str:
    """The event's name, or a readable stand-in when the catalogue has none.

    One helper for every sys read: the compact and the ordinary endpoint feed
    the same screens, and a code that reads "Неизвестный код 99" in one place
    and something else in another looks like two different events."""
    return sys_name if sys_name is not None else f"Неизвестный код {sys_type_id}"


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
        statement = self._range_statement()
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

    # Columns the table is allowed to sort on, server-side (maps API name → ORM column).
    _ORDER_COLUMNS = {
        "period": SysArchive.period,
        "sys_name": SysType.sys_name,
        "volume": SysArchive.volume,
    }

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
        Used by the sys-archive table view; the full get_range is kept for the
        accidents report which must load every row."""
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

        statement = self._range_statement()
        for f in filters:
            statement = statement.where(f)

        order_col = self._ORDER_COLUMNS.get(order_by, self.model.period)
        statement = statement.order_by(
            order_col.desc() if order_dir == "desc" else order_col.asc()
        )
        statement = statement.offset(max(skip, 0)).limit(max(min(limit, 500), 1))

        rows = (await self.session.execute(statement)).all()
        items = self._build_range_result(rows)
        return {"total": total, "items": items}

    def _range_statement(self):
        """The event rows plus the name their calculator type gives each code.

        Columns, not ORM entities: the accidents report pulls hundreds of
        thousands of rows at once, and building a SysArchive object per row
        (identity map, attribute instrumentation, change tracking) cost 12.5s
        of the 20s the endpoint took for one month — none of it useful for
        rows that are read once and thrown away."""
        return (
            select(
                self.model.id,
                self.model.line_id,
                self.model.period,
                self.model.sys_type_id,
                self.model.volume,
                SysType.sys_name,
            )
            .outerjoin(Line, self.model.line_id == Line.id)
            .outerjoin(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
            .outerjoin(GasVolumeCalcType, GasVolumeCalc.type_id == GasVolumeCalcType.id)
            .outerjoin(
                SysType,
                (GasVolumeCalcType.type_id == SysType.gas_volume_calc_type_id)
                & (self.model.sys_type_id == SysType.sys_type_id),
            )
        )

    @staticmethod
    def _build_range_result(rows):
        return [
            {
                "id": row_id,
                "line_id": line_id,
                "period": period,
                "sys_type_id": sys_type_id,
                "volume": volume,
                "sys_name": _sys_name(sys_name, sys_type_id),
            }
            for row_id, line_id, period, sys_type_id, volume, sys_name in rows
        ]


    async def get_events_json(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ) -> str:
        """The same rows as `get_range`, as a JSON string built for bulk reads.

        The accidents report is the only caller that wants EVERY row of a
        period, and for a month over every line that is ~435 000 of them. In
        the ordinary shape that is 160 MB, of which almost everything is
        repetition: the row id it never looks at, an ISO timestamp per row, and
        the event name — only 99 distinct names across the whole month — spelled
        out 435 000 times. Here the names are sent once and referenced by index,
        the period is epoch milliseconds (which the browser also needs no
        parsing for), and a row is a plain array. Same data, 21 MB.

        `period` is stored naive — plant wall clock — so turning it into an
        instant uses the container's timezone, fixed to Europe/Kyiv in
        docker-compose. That is what makes the browser show the same hour the
        ordinary endpoint's ISO string used to produce; a container left on UTC
        would shift every timestamp in the report.

        Returns the finished JSON text: going through a response model would
        validate and re-serialise every row, which costs more than reading them
        from the database did.
        """
        statement = (
            select(
                self.model.line_id,
                self.model.period,
                self.model.sys_type_id,
                self.model.volume,
                SysType.sys_name,
            )
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

        rows = (await self.session.execute(statement)).all()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._build_events_json, rows)

    @staticmethod
    def _build_events_json(rows) -> str:
        names: list[str] = []
        # (code, name) rather than the name alone: the same code is named
        # differently by different calculator types, and the pair is what the
        # row actually resolved to.
        name_index: dict[tuple[int, str], int] = {}
        out = []
        for line_id, period, sys_type_id, volume, sys_name in rows:
            sys_name = _sys_name(sys_name, sys_type_id)
            key = (sys_type_id, sys_name)
            idx = name_index.get(key)
            if idx is None:
                idx = len(names)
                name_index[key] = idx
                names.append(sys_name)
            out.append((line_id, int(period.timestamp() * 1000), sys_type_id, volume, idx))
        return json.dumps({"names": names, "rows": out}, ensure_ascii=False)

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
