"""
Automatic weather update: the server fetches the missing days from Open-Meteo.

The manual paths (browser download, file import) stay for an offline server;
this one needs the SERVER to reach archive-api.open-meteo.com — directly or
through HTTPS_PROXY, which httpx honours. It does exactly what the browser
does (src/domain/weather/openMeteo.ts): per branch, the gaps from
/weather/plan, points in fixed groups of POINTS_PER_REQUEST, one request per
kind and calendar year, stopping ARCHIVE_LAG_DAYS before today.

Settings, lock and status live in the single weather_job row. Timestamps are
written with the application's clock (Europe/Kyiv), not Postgres's now()
(UTC) — the scheduler compares them with datetime.now(); see the long note
in dpd_archive_refresh about the loop the mixed clocks once caused.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx
import sqlalchemy as sa
from sqlmodel import select

from backend.db.dao.weather_dao import WeatherDao
from backend.db.engine import async_session_factory
from backend.db.models.weather_model import (
    WeatherDailyRow,
    WeatherHourlyRow,
    WeatherPoint,
)

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
POINTS_PER_REQUEST = 10
ARCHIVE_LAG_DAYS = 7
REQUEST_TIMEOUT_SEC = 180
#: A running job silent for this long is taken to have died.
STALE_SECONDS = 30 * 60

DAILY_VARS = {
    "temperature_2m_mean": "t_mean",
    "temperature_2m_min": "t_min",
    "temperature_2m_max": "t_max",
    "precipitation_sum": "precipitation",
    "wind_speed_10m_max": "wind_max",
    "shortwave_radiation_sum": "radiation",
}
HOURLY_VARS = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "wind_speed_10m": "wind_speed",
    "shortwave_radiation": "radiation",
    "precipitation": "precipitation",
    "cloud_cover": "cloud_cover",
}

_ENSURE_ROW = "INSERT INTO weather_job (id, status) VALUES (1, 'idle') ON CONFLICT (id) DO NOTHING"


# ── settings and status ─────────────────────────────────────────────────────

async def read_status() -> dict:
    async with async_session_factory() as session:
        await session.execute(sa.text(_ENSURE_ROW))
        await session.commit()
        row = (await session.execute(sa.text(
            """
            SELECT auto_enabled, refresh_time, status, started_at, finished_at, error, result,
                   (status = 'running'
                    AND updated_at < now() - make_interval(secs => :stale)) AS is_stale
            FROM weather_job WHERE id = 1
            """
        ), {"stale": STALE_SECONDS})).mappings().one()
    out = dict(row)
    if out.pop("is_stale"):
        out["status"] = "error"
        out["error"] = "Оновлення перервано (процес зупинився)"
    return out


async def write_settings(auto_enabled: Optional[bool], refresh_time: Optional[str]) -> dict:
    async with async_session_factory() as session:
        await session.execute(sa.text(_ENSURE_ROW))
        if auto_enabled is not None:
            await session.execute(sa.text("UPDATE weather_job SET auto_enabled = :v WHERE id = 1"),
                                  {"v": auto_enabled})
        if refresh_time is not None:
            await session.execute(sa.text("UPDATE weather_job SET refresh_time = :v WHERE id = 1"),
                                  {"v": refresh_time})
        await session.commit()
    return await read_status()


async def acquire() -> bool:
    """Atomically claim the job. False if one is already running."""
    async with async_session_factory() as session:
        await session.execute(sa.text(_ENSURE_ROW))
        claimed = (await session.execute(sa.text(
            """
            UPDATE weather_job
            SET status = 'running', started_at = :started, updated_at = now(),
                finished_at = NULL, error = NULL, result = NULL
            WHERE id = 1
              AND (status <> 'running' OR updated_at < now() - make_interval(secs => :stale))
            RETURNING id
            """
        ), {"started": datetime.now(), "stale": STALE_SECONDS})).scalar() is not None
        await session.commit()
        return claimed


async def _heartbeat() -> None:
    async with async_session_factory() as session:
        await session.execute(sa.text(
            "UPDATE weather_job SET updated_at = now() WHERE id = 1 AND status = 'running'"
        ))
        await session.commit()


async def _finalize(status: str, error: Optional[str], result: Optional[str]) -> None:
    # Only closes a RUNNING job, so a process that hung and woke up after its
    # lock was taken over does not write over the run that replaced it.
    async with async_session_factory() as session:
        await session.execute(sa.text(
            """
            UPDATE weather_job SET status = :status, error = :error, result = :result,
                   finished_at = :finished, updated_at = now()
            WHERE id = 1 AND status = 'running'
            """
        ), {"status": status, "error": error, "result": result, "finished": datetime.now()})
        await session.commit()


# ── what to ask for ─────────────────────────────────────────────────────────

def plan_requests(gaps: list[dict], last_day: date) -> list[dict]:
    """Same grouping as the browser's planRequests: points by position in the
    id-sorted list, POINTS_PER_REQUEST to a request, the span from a group's
    earliest to latest gap, cut into calendar years and clipped to last_day."""
    out = []
    for kind in ("daily", "hourly"):
        for g in range(0, len(gaps), POINTS_PER_REQUEST):
            idx = list(range(g, min(g + POINTS_PER_REQUEST, len(gaps))))
            runs = [r for i in idx for r in gaps[i][kind]]
            if not runs:
                continue
            start = min(r.first for r in runs)
            end = min(max(r.last for r in runs), last_day)
            if start > end:
                continue
            for y in range(start.year, end.year + 1):
                out.append({
                    "kind": kind, "indexes": idx,
                    "start": start if y == start.year else date(y, 1, 1),
                    "end": end if y == end.year else date(y, 12, 31),
                })
    return out


def archive_params(points: list[WeatherPoint], kind: str, start: date, end: date) -> dict:
    return {
        "latitude": ",".join(f"{p.latitude:.5f}" for p in points),
        "longitude": ",".join(f"{p.longitude:.5f}" for p in points),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        kind: ",".join(DAILY_VARS if kind == "daily" else HOURLY_VARS),
        "timezone": "Europe/Kyiv",
        "wind_speed_unit": "ms",
    }


def parse_locations(payload: Any) -> list[dict]:
    """Open-Meteo JSON (one location or a list) → per location rows."""
    locations = payload if isinstance(payload, list) else [payload]
    out = []
    for loc in locations:
        if isinstance(loc, dict) and loc.get("error"):
            raise RuntimeError(f"Open-Meteo: {loc.get('reason') or 'помилка'}")
        daily, hourly = [], []
        block = loc.get("daily")
        if block:
            for i, t in enumerate(block["time"]):
                daily.append(WeatherDailyRow(
                    day=date.fromisoformat(t[:10]),
                    **{dst: (block.get(src) or [None] * len(block["time"]))[i]
                       for src, dst in DAILY_VARS.items()},
                ))
        block = loc.get("hourly")
        if block:
            for i, t in enumerate(block["time"]):
                hourly.append(WeatherHourlyRow(
                    stamp=datetime.fromisoformat(t),
                    **{dst: (block.get(src) or [None] * len(block["time"]))[i]
                       for src, dst in HOURLY_VARS.items()},
                ))
        out.append({"daily": daily, "hourly": hourly})
    return out


# ── the run ─────────────────────────────────────────────────────────────────

async def _branch_ids() -> list[int]:
    async with async_session_factory() as session:
        return list((await session.execute(
            select(WeatherPoint.branch_id).distinct().order_by(WeatherPoint.branch_id)
        )).scalars())


async def _refresh_branch(client: httpx.AsyncClient, branch_id: int, today: date) -> tuple[int, int]:
    async with async_session_factory() as session:
        dao = WeatherDao(session)
        points = list((await session.execute(
            select(WeatherPoint).where(WeatherPoint.branch_id == branch_id).order_by(WeatherPoint.id)
        )).scalars())
        first, last = await dao.archive_range(branch_id)
    if not points or first is None:
        return 0, 0
    yesterday = today - timedelta(days=1)
    last = min(last, yesterday)
    async with async_session_factory() as session:
        missing = {m.point_id: m for m in await WeatherDao(session).missing([p.id for p in points], first, last)}
    gaps = [{"daily": missing[p.id].daily, "hourly": missing[p.id].hourly} for p in points]

    nd = nh = 0
    for req in plan_requests(gaps, today - timedelta(days=ARCHIVE_LAG_DAYS)):
        group = [points[i] for i in req["indexes"]]
        resp = await client.get(ARCHIVE_URL, params=archive_params(group, req["kind"], req["start"], req["end"]))
        try:
            payload = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if resp.status_code != 200:
            parse_locations(payload)  # raises with Open-Meteo's own reason
            resp.raise_for_status()
        for point, loc in zip(group, parse_locations(payload)):
            async with async_session_factory() as session:
                d, h = await WeatherDao(session).import_rows(point.id, loc["daily"], loc["hourly"])
                await session.commit()
            nd += d
            nh += h
        await _heartbeat()
    return nd, nh


async def execute_locked(today: Optional[date] = None, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
    """The run itself; the caller holds the lock (acquire())."""
    today = today or date.today()
    status, error, result = "done", None, None
    nd = nh = 0
    failures = []
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC, transport=transport) as client:
            for branch_id in await _branch_ids():
                try:
                    d, h = await _refresh_branch(client, branch_id, today)
                    nd += d
                    nh += h
                except Exception as e:  # one branch must not stop the others
                    logger.exception("Weather update failed for branch %s", branch_id)
                    failures.append(f"філія {branch_id}: {e.__class__.__name__}: {e}"[:500])
        result = f"{nd} / {nh}"
        if failures:
            status, error = "error", "; ".join(failures)[:2000]
    except Exception as e:
        logger.exception("Weather update failed")
        status, error = "error", f"{e.__class__.__name__}: {e}"[:2000]
    finally:
        try:
            await _finalize(status, error, result)
        except Exception:
            logger.exception("Failed to persist the weather update state")
    logger.info("Weather update: %s, rows %s / %s", status, nd, nh)


async def run_refresh(**kwargs) -> bool:
    """Claim the lock and run. False if one is already running."""
    if not await acquire():
        return False
    await execute_locked(**kwargs)
    return True


#: Detached runs, kept referenced so the event loop does not drop them.
_background: set[asyncio.Task] = set()


async def start_in_background() -> bool:
    """For the «update now» button: claim the lock here, so a running job is
    reported at once (False), then run detached and let the request return."""
    if not await acquire():
        return False
    task = asyncio.create_task(execute_locked())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True
