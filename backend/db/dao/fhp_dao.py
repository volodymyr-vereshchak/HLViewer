"""Reading ФХП change events for a set of lines.

Two statements serve a whole report, whatever its length:

  1. every change of every requested quantity, for every line, inside the range;
  2. the one change that precedes the range, per (line, quantity), which is the
     value in force when the range opens.

There is no third: the value that held before the FIRST in-range change comes
back with that change itself, as its `old_value`.

The `edit_type` dictionary is deliberately not joined. Codes 1/2/3 (густина,
CO2, N2) mean the same thing on every computer type present in the data, so the
four-table join `EditArchiveDao.get_range` needs (edit_archive → gas_volume_line
→ gas_volume_calc → gas_volume_calc_type → edit_type) buys nothing here. A
fourth quantity would change that: outside this trio the same `edit_type_id`
means different things per computer family, and the join has to come back.
"""

from datetime import datetime
from typing import Sequence

from sqlalchemy import text

from backend.db.dao.basic_dao import BasicDao


class FhpDao(BasicDao):
    async def changes_in_range(
        self,
        line_ids: Sequence[int],
        edit_type_ids: Sequence[int],
        range_from: datetime,
        range_to: datetime,
    ) -> list[dict]:
        """Every change inside [range_from, range_to), ordered so Python never
        has to sort: (line_id, edit_type_id, period, id)."""
        if not line_ids or not edit_type_ids:
            return []
        rows = (await self.session.execute(
            text(
                "SELECT line_id, edit_type_id, period, old_value, new_value "
                "FROM edit_archive "
                "WHERE line_id = ANY(CAST(:line_ids AS bigint[])) "
                "  AND edit_type_id = ANY(CAST(:type_ids AS bigint[])) "
                "  AND period >= :range_from AND period < :range_to "
                "ORDER BY line_id, edit_type_id, period, id"
            ),
            {
                "line_ids": list(line_ids),
                "type_ids": list(edit_type_ids),
                "range_from": range_from,
                "range_to": range_to,
            },
        )).mappings().all()
        return [dict(r) for r in rows]

    async def hourly_flow(
        self,
        line_ids: Sequence[int],
        range_from: datetime,
        range_to: datetime,
    ) -> list[dict]:
        """Volume, pressure and temperature per line-hour.

        Everything the volume recalculation needs beyond the composition: the
        volume it is correcting, and the P/T that place both compositions on
        the compressibility surface. P and T are MEASURED, so they are the same
        for the entered and the reference gas.
        """
        if not line_ids:
            return []
        rows = (await self.session.execute(
            text(
                "SELECT line_id, period, volume, pressure, temperature "
                "FROM hourly_archive "
                "WHERE line_id = ANY(CAST(:line_ids AS bigint[])) "
                "  AND period >= :range_from AND period < :range_to "
                "ORDER BY line_id, period"
            ),
            {
                "line_ids": list(line_ids),
                "range_from": range_from,
                "range_to": range_to,
            },
        )).mappings().all()
        return [dict(r) for r in rows]

    async def last_data_period(self, line_ids: Sequence[int]) -> datetime | None:
        """How far the archive actually reaches for these lines.

        Read from `hourly_archive`, the dense series, and NOT from
        `edit_archive`: a composition legitimately holds for days, so the last
        change says nothing about how far the import has run. This is the same
        value the overview screen shows as the moment of the latest data
        (`/hourly_last_period/`, base_archive_ep.get_last_period).

        Falls back to the whole table when the route's own lines have no hourly
        rows — better a global horizon than none.
        """
        if not line_ids:
            return None
        scoped = (await self.session.execute(
            text(
                "SELECT max(period) FROM hourly_archive "
                "WHERE line_id = ANY(CAST(:line_ids AS bigint[]))"
            ),
            {"line_ids": list(line_ids)},
        )).scalar()
        if scoped is not None:
            return scoped
        return (await self.session.execute(
            text("SELECT max(period) FROM hourly_archive")
        )).scalar()

    async def seed_changes(
        self,
        line_ids: Sequence[int],
        edit_type_ids: Sequence[int],
        range_from: datetime,
    ) -> list[dict]:
        """The last change before the range, per (line, quantity).

        CROSS JOIN LATERAL, not LEFT: pairs with no history at all must drop
        out, so the caller falls back to the first in-range `old_value`.
        """
        if not line_ids or not edit_type_ids:
            return []
        rows = (await self.session.execute(
            text(
                "SELECT l.line_id, t.edit_type_id, s.period, s.new_value "
                "FROM unnest(CAST(:line_ids AS bigint[])) AS l(line_id) "
                "CROSS JOIN unnest(CAST(:type_ids AS bigint[])) AS t(edit_type_id) "
                "CROSS JOIN LATERAL ("
                "    SELECT ea.period, ea.new_value FROM edit_archive ea"
                "    WHERE ea.line_id = l.line_id"
                "      AND ea.edit_type_id = t.edit_type_id"
                "      AND ea.period < :range_from"
                "    ORDER BY ea.period DESC, ea.id DESC LIMIT 1"
                ") s"
            ),
            {
                "line_ids": list(line_ids),
                "type_ids": list(edit_type_ids),
                "range_from": range_from,
            },
        )).mappings().all()
        return [dict(r) for r in rows]
