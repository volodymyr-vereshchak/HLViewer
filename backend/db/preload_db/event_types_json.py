"""
The event-type dictionaries and their JSON files: FLOWTYPE, SYSNAME, EDITNAME.

These three files are how the dictionaries reach another installation — they
travel with the code and are loaded into the database on every backend start
(`preload_db`), so a name edited only in the admin panel is overwritten by the
file at the next restart. Both directions live here so the format is defined
once: the reader used at startup and the writer used by «Зберегти в JSON» can
never drift apart.

Usage:
    python -m backend.db.preload_db.event_types_json  # export DB → files
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.engine import async_session_factory
from backend.db.models import EDIT_TYPE_CONSTRAINT, SYS_TYPE_CONSTRAINT
from backend.db.models.edit_type_model import EditType
from backend.db.models.gas_volume_calc_type_model import (
    GAS_VOLUME_CALC_TYPE_CONSTRAINT,
    GasVolumeCalcType,
)
from backend.db.models.sys_type_model import SysType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Resolved from the module, not the working directory: this runs both from the
# repo root (uvicorn) and from wherever a request happens to be served.
PRELOAD_DIR = Path(__file__).parent
FLOWTYPE_PATH = PRELOAD_DIR / "FLOWTYPE.json"
SYSNAME_PATH = PRELOAD_DIR / "SYSNAME.json"
EDITNAME_PATH = PRELOAD_DIR / "EDITNAME.json"

# 6828 SYSNAME rows in one INSERT ... VALUES is enough to stall Postgres.
BATCH_SIZE = 1000


@dataclass
class EventTypeCounts:
    """How many rows each dictionary moved, so the admin panel can say more
    than "готово"."""

    flowtype: int = 0
    sysname: int = 0
    editname: int = 0
    wiped: bool = False


async def export_event_types(session: AsyncSession) -> EventTypeCounts:
    """Write the dictionaries as they stand in the DB back into the JSON files.

    The shape is the one the original ASK export produced, because that is what
    `import_event_types` (and every older copy of these files) reads.
    """
    calc_types = (await session.execute(
        select(GasVolumeCalcType).order_by(GasVolumeCalcType.type_id)
    )).scalars().all()
    sys_types = (await session.execute(
        select(SysType).order_by(SysType.gas_volume_calc_type_id, SysType.sys_type_id)
    )).scalars().all()
    edit_types = (await session.execute(
        select(EditType).order_by(EditType.gas_volume_calc_type_id, EditType.edit_type_id)
    )).scalars().all()

    flowtype = {"FLOWTYPE": [
        {"ID_TYPE": c.type_id, "TYPENAME": c.type_name} for c in calc_types
    ]}
    sysname = {"SYSNAME": [
        {"ID_TYPE": s.gas_volume_calc_type_id, "SYS_ID": s.sys_type_id, "SYSNAME": s.sys_name}
        for s in sys_types
    ]}
    editname = {"EDITNAME": [
        {"ID_TYPE": e.gas_volume_calc_type_id, "EDIT_ID": e.edit_type_id, "EDITNAME": e.edit_name}
        for e in edit_types
    ]}

    _write(FLOWTYPE_PATH, flowtype)
    _write(SYSNAME_PATH, sysname)
    _write(EDITNAME_PATH, editname)

    counts = EventTypeCounts(
        flowtype=len(calc_types), sysname=len(sys_types), editname=len(edit_types)
    )
    logger.info(
        f"Довідники збережено: FLOWTYPE {counts.flowtype}, "
        f"SYSNAME {counts.sysname}, EDITNAME {counts.editname}"
    )
    return counts


async def import_event_types(session: AsyncSession, force: bool = False) -> EventTypeCounts:
    """Load the JSON files into the DB.

    Rows are matched by the pair (event code, CALCULATOR TYPE CODE) — the code
    from `gas_vol_calc_type.type_id`, which is what `ID_TYPE` in the files means
    and what the archive joins on. It is not the calc-type row id; using that
    would silently disconnect every name from its events.

    Without `force` this is a merge: names are updated from the file, missing
    rows are added, and rows present only in the DB are left alone. This is also
    what runs on every backend start.

    `force` first empties sys_type and edit_type, so rows added by hand and
    absent from the file are gone for good. It deliberately does NOT touch
    gas_vol_calc_type: deleting a calculator type cascades to its lines and
    their whole archive, so a "reload the event dictionaries" button must never
    be able to reach it.
    """
    counts = EventTypeCounts(wiped=force)

    if force:
        # Raw DELETE, not the ORM: loading 6828 rows only to delete them one by
        # one is minutes of work for a statement Postgres does instantly.
        # Nothing references these tables (archives keep the bare code and join
        # the name in), so there is no FK to trip over.
        await session.execute(text("DELETE FROM sys_type"))
        await session.execute(text("DELETE FROM edit_type"))
        await session.commit()

    flow_type = _read(FLOWTYPE_PATH, "FLOWTYPE")
    instances = [
        {
            "type_id": row["ID_TYPE"],
            "type_name": row["TYPENAME"].strip(),
            "updated_at": datetime.now(),
        }
        for row in flow_type
    ]
    if instances:
        await GasVolumeCalcTypeDao(session=session).bulk_upsert_with_update(
            instances, GAS_VOLUME_CALC_TYPE_CONSTRAINT
        )
    counts.flowtype = len(instances)

    edit_name = _read(EDITNAME_PATH, "EDITNAME")
    instances = [
        {
            "edit_type_id": row["EDIT_ID"],
            "gas_volume_calc_type_id": row["ID_TYPE"],
            "edit_name": row["EDITNAME"].strip(),
            "updated_at": datetime.now(),
        }
        for row in edit_name
    ]
    await _upsert_batched(EditTypeDao(session=session), instances, EDIT_TYPE_CONSTRAINT)
    counts.editname = len(instances)

    sys_name = _read(SYSNAME_PATH, "SYSNAME")
    instances = [
        {
            "sys_type_id": row["SYS_ID"],
            "gas_volume_calc_type_id": row["ID_TYPE"],
            "sys_name": row["SYSNAME"].strip(),
            "updated_at": datetime.now(),
        }
        for row in sys_name
    ]
    await _upsert_batched(SysTypeDao(session=session), instances, SYS_TYPE_CONSTRAINT)
    counts.sysname = len(instances)

    logger.info(
        f"Довідники завантажено{' (перезапис)' if force else ''}: "
        f"FLOWTYPE {counts.flowtype}, SYSNAME {counts.sysname}, EDITNAME {counts.editname}"
    )
    return counts


def _read(path: Path, key: str) -> list[dict]:
    """The file's row list, or nothing if the file is not there.

    A missing file is not an error: an installation may legitimately carry only
    some of the three, and the alternative — refusing to start — is worse than
    leaving a dictionary as it already is in the database.
    """
    if not path.exists():
        logger.warning(f"{path.name} відсутній — довідник {key} пропущено")
        return []
    return json.loads(path.read_text(encoding="utf8"))[key]


def _write(path: Path, payload: dict) -> None:
    """One canonical shape: 2-space indent, LF, trailing newline.

    The three files arrived from three different tools and were indented with
    tabs, four spaces and two spaces respectively; the first export rewrote all
    of them wholesale, so a one-line correction reached the reviewer as a
    40 000-line diff. `newline` is pinned too — without it the same export run
    on Windows writes CRLF and in the container LF, which is the same noise
    again on every second save.
    """
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


async def _upsert_batched(dao, instances: list[dict], constraint: list[str]) -> None:
    for i in range(0, len(instances), BATCH_SIZE):
        await dao.bulk_upsert_with_update(instances[i : i + BATCH_SIZE], constraint)


async def main() -> None:
    async with async_session_factory() as session:
        await export_event_types(session)


if __name__ == "__main__":
    asyncio.run(main())
