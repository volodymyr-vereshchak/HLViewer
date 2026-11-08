"""
Automatic weather update: the server fetches the missing days from Open-Meteo.

The manual paths (browser download, file import) stay for an offline server;
this one needs the SERVER to reach archive-api.open-meteo.com and
api.open-meteo.com — directly or through HTTPS_PROXY, which httpx honours.
Each run closes the history gaps, then rewrites the forecast (16 days ahead
and the last week the archive has not published yet). It does exactly what the browser
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
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
#: Gaps closer than this join one request: a request per missing day of a
#: long ragged stretch would be worse than re-reading the few days between.
MERGE_GAP_DAYS = 30
#: The forecast API's maximum horizon; the first days are the reliable ones.
FORECAST_DAYS = 16
#: Past days taken with the forecast: they cover the week the archive has
#: not published yet, until the archive replaces them.
FORECAST_PAST_DAYS = 7
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
    """Requests that close the gaps — the same plan the browser builds
    (planRequests in src/domain/weather/openMeteo.ts).

    Points go in fixed groups of POINTS_PER_REQUEST by their position in the
    id-sorted list. Within a group, the gaps of all its points are merged
    into segments (runs closer than MERGE_GAP_DAYS join one segment), and each
    segment is asked for only for the points that have a gap in it. One old
    missing day therefore costs a request for that day, not a re-download of
    everything since then for the whole group. Segments are cut into
    calendar years and clipped to last_day."""
    out = []
    for kind in ("daily", "hourly"):
        for g in range(0, len(gaps), POINTS_PER_REQUEST):
            runs = []
            for i in range(g, min(g + POINTS_PER_REQUEST, len(gaps))):
                for r in gaps[i][kind]:
                    if r.first <= last_day:
                        runs.append((r.first, min(r.last, last_day), i))
            runs.sort()
            segments: list[list] = []  # [first, last, {indexes}]
            for first, last, i in runs:
                if segments and (first - segments[-1][1]).days <= MERGE_GAP_DAYS:
                    seg = segments[-1]
                    seg[1] = max(seg[1], last)
                    seg[2].add(i)
                else:
                    segments.append([first, last, {i}])
            for first, last, idx in segments:
                for y in range(first.year, last.year + 1):
                    out.append({
                        "kind": kind, "indexes": sorted(idx),
                        "start": first if y == first.year else date(y, 1, 1),
                        "end": last if y == last.year else date(y, 12, 31),
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


def forecast_params(points: list[WeatherPoint]) -> dict:
    return {
        "latitude": ",".join(f"{p.latitude:.5f}" for p in points),
        "longitude": ",".join(f"{p.longitude:.5f}" for p in points),
        "daily": ",".join(DAILY_VARS),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "Europe/Kyiv",
        "wind_speed_unit": "ms",
        "past_days": FORECAST_PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
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


#: Waits before each retry of a refused request, seconds. Open-Meteo answers
#: "The service is overloaded" (and 429 when a minute's quota is spent) to a
#: burst of heavy requests — a first multi-year load is exactly that.
RETRY_DELAYS_SEC = (10, 30, 60, 120)
#: Pause between requests, so a run does not itself cause the overload.
REQUEST_PAUSE_SEC = 1.0
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_TRANSIENT_WORDS = ("overload", "too many", "limit exceeded", "try again")

# Indirection for tests: they replace it to run without waiting.
_sleep = asyncio.sleep


class _Transient(Exception):
    """A refusal worth retrying: overload, rate limit, a dropped connection."""


async def _get_once(client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
    try:
        resp = await client.get(url, params=params)
    except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as e:
        raise _Transient(f"{e.__class__.__name__}: {e}") from e
    try:
        payload = resp.json()
    except ValueError:
        if resp.status_code in _TRANSIENT_STATUS:
            raise _Transient(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        raise
    if resp.status_code != 200:
        reason = str(payload.get("reason", "")) if isinstance(payload, dict) else ""
        if resp.status_code in _TRANSIENT_STATUS or any(w in reason.lower() for w in _TRANSIENT_WORDS):
            raise _Transient(f"Open-Meteo: {reason or f'HTTP {resp.status_code}'}")
        parse_locations(payload)  # raises with Open-Meteo's own reason
        resp.raise_for_status()
    return parse_locations(payload)


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
    """GET with retries on transient refusals; the last one is raised as is.
    A permanent refusal (a bad parameter, say) is raised at once."""
    await _sleep(REQUEST_PAUSE_SEC)
    for attempt, delay in enumerate((*RETRY_DELAYS_SEC, None)):
        try:
            return await _get_once(client, url, params)
        except _Transient as e:
            if delay is None:
                raise RuntimeError(f"{e} (після {attempt + 1} спроб)") from e
            logger.warning("Open-Meteo refused (%s), retry %d in %ss", e, attempt + 1, delay)
            await _heartbeat()
            await _sleep(delay)
    raise AssertionError("unreachable")


async def _store(point_id: int, loc: dict, source: str, today: date) -> tuple[int, int]:
    async with async_session_factory() as session:
        d, h = await WeatherDao(session).import_rows(
            point_id, loc["daily"], loc["hourly"], source=source, today=today,
        )
        await session.commit()
    return d, h


async def _refresh_branch(client: httpx.AsyncClient, branch_id: int, today: date) -> dict:
    """History gaps first, then the forecast. Counts of rows written."""
    counts = {"ad": 0, "ah": 0, "fd": 0, "fh": 0}
    async with async_session_factory() as session:
        dao = WeatherDao(session)
        points = list((await session.execute(
            select(WeatherPoint).where(WeatherPoint.branch_id == branch_id).order_by(WeatherPoint.id)
        )).scalars())
        first, last = await dao.archive_range(branch_id)
    if not points:
        return counts

    if first is not None:
        last = min(last, today - timedelta(days=1))
        async with async_session_factory() as session:
            missing = {m.point_id: m for m in await WeatherDao(session).missing(
                [p.id for p in points], first, last)}
        gaps = [{"daily": missing[p.id].daily, "hourly": missing[p.id].hourly} for p in points]
        for req in plan_requests(gaps, today - timedelta(days=ARCHIVE_LAG_DAYS)):
            group = [points[i] for i in req["indexes"]]
            locs = await _get(client, ARCHIVE_URL, archive_params(group, req["kind"], req["start"], req["end"]))
            for point, loc in zip(group, locs):
                d, h = await _store(point.id, loc, "archive", today)
                counts["ad"] += d
                counts["ah"] += h
            await _heartbeat()

    for g in range(0, len(points), POINTS_PER_REQUEST):
        group = points[g:g + POINTS_PER_REQUEST]
        for point, loc in zip(group, await _get(client, FORECAST_URL, forecast_params(group))):
            d, h = await _store(point.id, loc, "forecast", today)
            counts["fd"] += d
            counts["fh"] += h
        await _heartbeat()
    return counts


async def execute_locked(today: Optional[date] = None, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
    """The run itself; the caller holds the lock (acquire())."""
    today = today or date.today()
    status, error, result = "done", None, None
    total = {"ad": 0, "ah": 0, "fd": 0, "fh": 0}
    failures = []
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC, transport=transport) as client:
            for branch_id in await _branch_ids():
                try:
                    for k, v in (await _refresh_branch(client, branch_id, today)).items():
                        total[k] += v
                except Exception as e:  # one branch must not stop the others
                    logger.exception("Weather update failed for branch %s", branch_id)
                    failures.append(f"філія {branch_id}: {e.__class__.__name__}: {e}"[:500])
        # History, then forecast: daily / hourly rows written.
        result = f"{total['ad']} / {total['ah']}; прогноз {total['fd']} / {total['fh']}"
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
    logger.info("Weather update: %s, %s", status, result)


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
