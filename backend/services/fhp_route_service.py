"""«Звірка ФХП»: comparing a route's lines against its reference composition.

Unlike the other reports in this project — accidents, ГРС trends, night flows —
this one is computed on the BACKEND rather than in the browser. That is
deliberate and worth keeping: the raw material is tens of thousands of change
events per report, decoding them is a bit-level reinterpretation, and
reconstructing the step function is stateful. Shipping the raw rows to the
client would send far more bytes to do the same arithmetic less testably.

The arithmetic itself lives in `fhp_series` (pure, unit-tested); this module
only fetches, decodes and assembles.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.fhp_dao import FhpDao
from backend.db.models.fhp_report_models import (
    FhpLineSeries,
    FhpVolumeBlock,
    FhpVolumeLine,
    FhpLineStats,
    FhpParamBlock,
    RouteFhpReport,
)
from backend.db.models.gas_route_model import GasRoute, GasRouteMember
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.line_model import Line
from backend.services import commercial_day, fhp_series
from backend.services.edit_value_codec import decode_float, is_plausible
from backend.services.volume_delta import GasState, volume_delta


@dataclass(frozen=True)
class FhpParam:
    code: str
    edit_type_id: int
    label: str
    unit: str
    decimals: int
    default_tolerance_abs: float
    default_tolerance_pct: float


# Defaults chosen against the real spread: густина sits near 0.7467, so
# 0.0020 кг/м³ is ~0.27 % and about a chromatograph's own repeatability; CO2
# and N2 sit near 0.61 and 1.95 мол.%, where 0.05 and 0.10 are one meaningful
# step. All three are adjustable in the report.
FHP_PARAMS: dict[str, FhpParam] = {
    "density": FhpParam("density", 1, "Густина", "кг/м³", 4, 0.0020, 0.30),
    "co2": FhpParam("co2", 2, "CO₂", "мол.%", 4, 0.0500, 5.00),
    "n2": FhpParam("n2", 3, "N₂", "мол.%", 4, 0.1000, 5.00),
}

DEFAULT_STALE_AFTER_HOURS = 48


class RouteReportError(Exception):
    """Something the user must fix; the message is shown as-is."""


@dataclass(frozen=True)
class RouteMemberInfo:
    member_id: int
    line_id: int
    line_name: str
    calc_name: Optional[str]
    is_reference: bool
    sort_order: int
    # Лічильник vs діафрагма — the two convert a volume differently, so a wrong
    # composition costs them different amounts.
    is_meter: bool


async def load_members(session: AsyncSession, route_id: int) -> list[RouteMemberInfo]:
    """Route members with their line and ГРС names, in display order."""
    stmt = (
        select(GasRouteMember, Line.name, GasVolumeCalc.name, Line.meter)
        .join(Line, Line.id == GasRouteMember.line_id)
        .outerjoin(GasVolumeCalc, GasVolumeCalc.id == Line.gas_volume_calc_id)
        .where(GasRouteMember.route_id == route_id)
        .order_by(GasRouteMember.sort_order, GasRouteMember.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        RouteMemberInfo(
            member_id=m.id,
            line_id=m.line_id,
            line_name=line_name or f"Лінія {m.line_id}",
            calc_name=calc_name,
            is_reference=m.is_reference,
            sort_order=m.sort_order,
            is_meter=bool(meter),
        )
        for m, line_name, calc_name, meter in rows
    ]


async def build_route_report(
    session: AsyncSession,
    route: GasRoute,
    date_from: date,
    date_to: date,
    params: Sequence[str],
    granularity: Literal["hourly", "daily"] = "hourly",
    tolerances: Optional[Mapping[str, float]] = None,
    tolerance_mode: Literal["abs", "pct"] = "abs",
    stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
    now: Optional[datetime] = None,
) -> RouteFhpReport:
    members = await load_members(session, route.id)
    if not members:
        raise RouteReportError("Маршрут не містить ліній")

    unknown = [p for p in params if p not in FHP_PARAMS]
    if unknown:
        raise RouteReportError(f"Невідомий параметр: {', '.join(unknown)}")

    hour = commercial_day.contract_hour()
    range_from, range_to = commercial_day.range_window(date_from, date_to, hour)

    line_ids = [m.line_id for m in members]
    # ALL three quantities are read regardless of what is displayed: the volume
    # recalculation needs ρ, CO2 and N2 together to get the compressibility
    # ratio, and one query with three codes costs no more than one with one.
    edit_type_ids = [p.edit_type_id for p in FHP_PARAMS.values()]
    dao = FhpDao(session=session)

    # Past the last imported hour a step function would happily hold its value
    # forever, filling the table with numbers nobody measured and reporting the
    # day as complete. The horizon is where the ARCHIVE ends, not where the
    # clock is: on this data the two are months apart.
    data_until = await dao.last_data_period(line_ids)
    horizon = (now or datetime.now()).replace(minute=0, second=0, microsecond=0)
    if data_until is not None:
        # An hourly stamp opens its hour (see enterprise_volume_service.
        # request_window), so a record at 11:00 means the hour 11:00–12:00 is
        # known and the exclusive end is 12:00.
        horizon = min(horizon, data_until.replace(minute=0, second=0, microsecond=0)
                      + timedelta(hours=1))

    clipped_at: Optional[datetime] = None
    if range_to > horizon:
        range_to = horizon
        clipped_at = horizon
    if range_to <= range_from:
        raise RouteReportError(
            "Дані по цьому маршруту є лише до "
            f"{horizon - timedelta(hours=1):%d.%m.%Y %H:00} — оберіть раніший період"
        )

    changes = await dao.changes_in_range(line_ids, edit_type_ids, range_from, range_to)
    seeds = await dao.seed_changes(line_ids, edit_type_ids, range_from)

    days = commercial_day.days_in_range(date_from, date_to)
    warnings: list[str] = []

    # Built once for every quantity — the displayed blocks fold these, and the
    # volume recalculation reads all three at the same hour.
    series_by_param = {
        code: build_param_series(
            param=FHP_PARAMS[code],
            members=members,
            changes=changes,
            seeds=seeds,
            range_from=range_from,
            range_to=range_to,
        )
        for code in FHP_PARAMS
    }

    blocks: list[FhpParamBlock] = []
    for code in params:
        blocks.append(_build_block(
            param=FHP_PARAMS[code],
            series=series_by_param[code],
            members=members,
            days=days,
            hour=hour,
            granularity=granularity,
            range_from=range_from,
            range_to=range_to,
            tolerance=_tolerance(code, tolerances, tolerance_mode),
            tolerance_mode=tolerance_mode,
            stale_after_hours=stale_after_hours,
            warnings=warnings,
        ))

    volume = await _build_volume_block(
        session=session,
        members=members,
        series_by_param=series_by_param,
        range_from=range_from,
        range_to=range_to,
        days=days,
        hour=hour,
        granularity=granularity,
        warnings=warnings,
    )

    # The clipping is NOT appended to `warnings`: it already travels as
    # `range_clipped_at` + `data_until`, and saying it twice made the screen
    # show two alerts for one fact.

    return RouteFhpReport(
        route_id=route.id,
        route_number=route.number,
        route_name=route.name,
        branch_id=route.branch_id,
        granularity=granularity,
        date_from=date_from,
        date_to=date_to,
        contract_hour=hour,
        stale_after_hours=stale_after_hours,
        data_until=data_until.isoformat() if data_until else None,
        range_clipped_at=clipped_at.isoformat() if clipped_at else None,
        params=blocks,
        volume=volume,
        warnings=warnings,
    )


async def _build_volume_block(
    *,
    session: AsyncSession,
    members: Sequence[RouteMemberInfo],
    series_by_param: Mapping[str, "ParamSeries"],
    range_from: datetime,
    range_to: datetime,
    days: Sequence[date],
    hour: int,
    granularity: str,
    warnings: list[str],
) -> Optional[FhpVolumeBlock]:
    """What each line's volume would have been on the reference composition.

    Always computed HOURLY and then summed: the correction is non-linear in the
    composition and the flow is not spread evenly over a day, so an hour that
    carried no gas contributes nothing however wrong its ФХП was. Folding daily
    averages instead would give a different — and wrong — number.
    """
    ref_ids = [m.line_id for m in members if m.is_reference]
    compared = [m for m in members if not m.is_reference]
    if not ref_ids or not compared:
        # Without a reference there is nothing to correct towards.
        return None

    reference_by_param = {
        code: fhp_series.reference_series(series.hourly, ref_ids)[0]
        for code, series in series_by_param.items()
    }

    flow = await FhpDao(session=session).hourly_flow(
        [m.line_id for m in compared], range_from, range_to
    )
    flow_by_line: dict[int, dict[datetime, dict]] = {}
    for row in flow:
        flow_by_line.setdefault(row["line_id"], {})[row["period"]] = row

    periods = _periods(granularity, days, range_from, range_to)
    lines: list[FhpVolumeLine] = []
    unreadable = 0

    for member in compared:
        hourly_volume: dict[datetime, float] = {}
        hourly_delta: dict[datetime, float] = {}

        for stamp, row in flow_by_line.get(member.line_id, {}).items():
            volume = row["volume"]
            if volume is None:
                continue
            hourly_volume[stamp] = float(volume)

            entered = _state_at(series_by_param, member.line_id, stamp)
            reference = _reference_at(reference_by_param, stamp)
            if entered is None or reference is None:
                continue
            delta = volume_delta(
                float(volume),
                entered,
                reference,
                float(row["pressure"]),
                float(row["temperature"]),
                is_meter=member.is_meter,
            )
            if delta is None:
                unreadable += 1
                continue
            hourly_delta[stamp] = delta

        volume_series = _fold_sum(hourly_volume, granularity, days, hour)
        delta_series = _fold_sum(hourly_delta, granularity, days, hour)

        total_volume = sum(hourly_volume.values()) if hourly_volume else None
        total_delta = sum(hourly_delta.values()) if hourly_delta else None
        lines.append(FhpVolumeLine(
            line_id=member.line_id,
            line_name=member.line_name,
            is_meter=member.is_meter,
            status="ok" if hourly_delta else "no_data",
            volume=[_round(volume_series.get(p), 3) for p in periods],
            delta=[_round(delta_series.get(p), 3) for p in periods],
            delta_pct=[
                _pct(delta_series.get(p), volume_series.get(p)) for p in periods
            ],
            total_volume=_round(total_volume, 3),
            total_delta=_round(total_delta, 3),
            total_delta_pct=_pct(total_delta, total_volume),
        ))

    if unreadable:
        warnings.append(
            f"Об'єм: {unreadable} годин не перерахувалися — стан газу поза межами "
            f"застосовності ГОСТ 30319.2"
        )

    route_delta = sum(
        line.total_delta for line in lines if line.total_delta is not None
    ) if any(line.total_delta is not None for line in lines) else None

    return FhpVolumeBlock(
        periods=[_iso(p) for p in periods],
        lines=lines,
        total_delta=_round(route_delta, 3),
    )


def _state_at(
    series_by_param: Mapping[str, "ParamSeries"], line_id: int, stamp: datetime
) -> Optional[GasState]:
    rho = series_by_param["density"].hourly.get(line_id, {}).get(stamp)
    co2 = series_by_param["co2"].hourly.get(line_id, {}).get(stamp)
    n2 = series_by_param["n2"].hourly.get(line_id, {}).get(stamp)
    if rho is None or co2 is None or n2 is None:
        return None
    return GasState(density=rho, co2=co2, n2=n2)


def _reference_at(
    reference_by_param: Mapping[str, Mapping[datetime, float]], stamp: datetime
) -> Optional[GasState]:
    rho = reference_by_param["density"].get(stamp)
    co2 = reference_by_param["co2"].get(stamp)
    n2 = reference_by_param["n2"].get(stamp)
    if rho is None or co2 is None or n2 is None:
        return None
    return GasState(density=rho, co2=co2, n2=n2)


def _fold_sum(
    hourly: Mapping[datetime, float], granularity: str, days: Sequence[date], hour: int
) -> dict:
    """Hours as they are, or SUMMED into commercial days — never averaged."""
    if granularity != "daily":
        return dict(hourly)
    out: dict[date, float] = {}
    for stamp, value in hourly.items():
        out[commercial_day.day_of(stamp, hour)] = (
            out.get(commercial_day.day_of(stamp, hour), 0.0) + value
        )
    return {d: out[d] for d in days if d in out}


def _pct(delta: Optional[float], volume: Optional[float]) -> Optional[float]:
    if delta is None or volume is None or abs(volume) < 1e-9:
        return None
    return round(delta / volume * 100.0, 4)


def _tolerance(
    code: str, tolerances: Optional[Mapping[str, float]], mode: str
) -> float:
    given = (tolerances or {}).get(code)
    if given is not None:
        return given
    param = FHP_PARAMS[code]
    return param.default_tolerance_pct if mode == "pct" else param.default_tolerance_abs


@dataclass(frozen=True)
class ParamSeries:
    """One quantity, reconstructed hourly for every line of the route."""

    hourly: dict[int, dict[datetime, float]]
    steps: dict[int, list[fhp_series.Step]]
    rejected: int


def build_param_series(
    *,
    param: FhpParam,
    members: Sequence[RouteMemberInfo],
    changes: Sequence[dict],
    seeds: Sequence[dict],
    range_from: datetime,
    range_to: datetime,
) -> ParamSeries:
    """Decode → seed → step function → hourly, per line.

    Hourly ALWAYS, whatever the report displays: the daily view folds these,
    and the volume recalculation needs the hourly composition even when the
    table shows days.
    """
    type_id = param.edit_type_id
    rejected = 0

    seed_by_line: dict[int, tuple[datetime, float]] = {}
    for row in seeds:
        if row["edit_type_id"] != type_id:
            continue
        value = decode_float(row["new_value"])
        if not is_plausible(type_id, value):
            rejected += 1
            continue
        seed_by_line[row["line_id"]] = (row["period"], value)

    rows_by_line: dict[int, list[tuple[datetime, float]]] = {}
    first_old_by_line: dict[int, float] = {}
    for row in changes:
        if row["edit_type_id"] != type_id:
            continue
        line_id = row["line_id"]
        if line_id not in first_old_by_line:
            old = decode_float(row["old_value"])
            if is_plausible(type_id, old):
                first_old_by_line[line_id] = old
        value = decode_float(row["new_value"])
        if not is_plausible(type_id, value):
            rejected += 1
            continue
        rows_by_line.setdefault(line_id, []).append((row["period"], value))

    steps_by_line: dict[int, list[fhp_series.Step]] = {}
    hourly_by_line: dict[int, dict[datetime, float]] = {}
    for member in members:
        seeded = seed_by_line.get(member.line_id)
        seed = fhp_series.seed_value(
            seeded[1] if seeded else None, first_old_by_line.get(member.line_id)
        )
        steps = fhp_series.build_steps(
            rows_by_line.get(member.line_id, []),
            seed,
            range_from,
            seed_at=seeded[0] if seeded else None,
        )
        steps_by_line[member.line_id] = steps
        hourly_by_line[member.line_id] = fhp_series.hourly_series(
            steps, range_from, range_to
        )

    return ParamSeries(hourly=hourly_by_line, steps=steps_by_line, rejected=rejected)


def _build_block(
    *,
    param: FhpParam,
    series: ParamSeries,
    members: Sequence[RouteMemberInfo],
    days: Sequence[date],
    hour: int,
    granularity: str,
    range_from: datetime,
    range_to: datetime,
    tolerance: float,
    tolerance_mode: str,
    stale_after_hours: int,
    warnings: list[str],
) -> FhpParamBlock:
    rejected = series.rejected
    steps_by_line = series.steps
    series_by_line: dict[int, dict] = {}
    hours_present: dict[date, int] = {}

    for member in members:
        hourly = series.hourly[member.line_id]
        if granularity == "daily":
            daily = fhp_series.daily_series(hourly, days, hour)
            series_by_line[member.line_id] = {d: v for d, (v, _n) in daily.items()}
            for d, (_v, n) in daily.items():
                hours_present[d] = max(hours_present.get(d, 0), n)
        else:
            series_by_line[member.line_id] = hourly

        if not series_by_line[member.line_id]:
            warnings.append(
                f"{param.label}: лінія «{member.line_name}» — немає даних за період"
            )

    periods = _periods(granularity, days, range_from, range_to)

    ref_ids = [m.line_id for m in members if m.is_reference]
    compared_ids = [m.line_id for m in members if not m.is_reference]
    # Nothing to compare against unless there is a reference AND something that
    # is not one: a route where every line is the reference has nothing to
    # compare, and reads like a route with no reference at all.
    has_reference = bool(ref_ids) and bool(compared_ids)

    reference, ref_counts = (
        fhp_series.reference_series(series_by_line, ref_ids)
        if has_reference else ({}, {})
    )
    spread = fhp_series.spread_series(series_by_line)

    if has_reference and ref_counts:
        counts = {ref_counts.get(p, 0) for p in periods if p in ref_counts}
        if len(counts) > 1:
            warnings.append(
                f"{param.label}: склад еталона змінювався протягом періоду — "
                f"порівняння спирається то на {min(counts)}, то на {max(counts)} лінію(ї)"
            )

    lines: list[FhpLineSeries] = []
    for member in members:
        series = series_by_line[member.line_id]
        stale_periods = fhp_series.staleness(
            steps_by_line[member.line_id],
            [p for p in periods if isinstance(p, datetime)],
            stale_after_hours,
        ) if granularity == "hourly" else set()

        devs = (
            fhp_series.deviations(series, reference)
            if has_reference and not member.is_reference else []
        )
        by_period = {d.period: d for d in devs}
        stats = fhp_series.line_stats(devs, tolerance, tolerance_mode) if devs else None

        lines.append(FhpLineSeries(
            line_id=member.line_id,
            line_name=member.line_name,
            calc_name=member.calc_name,
            is_reference=member.is_reference,
            status="ok" if series else "no_data",
            values=[_round(series.get(p), param.decimals) for p in periods],
            deltas=(
                [_round(by_period[p].delta, param.decimals + 1) if p in by_period
                 else None for p in periods]
                if has_reference and not member.is_reference else None
            ),
            delta_pcts=(
                [round(by_period[p].delta_pct, 3)
                 if p in by_period and by_period[p].delta_pct is not None else None
                 for p in periods]
                if has_reference and not member.is_reference else None
            ),
            stale=[p in stale_periods for p in periods],
            stats=_stats_model(stats),
        ))

    if rejected:
        warnings.append(
            f"{param.label}: відкинуто {rejected} неправдоподібних значень"
        )

    return FhpParamBlock(
        param=param.code,
        label=param.label,
        unit=param.unit,
        decimals=param.decimals,
        tolerance=tolerance,
        tolerance_mode=tolerance_mode,
        has_reference=has_reference,
        periods=[_iso(p) for p in periods],
        hours_present=(
            [hours_present.get(p, 0) for p in periods] if granularity == "daily" else None
        ),
        reference=(
            [_round(reference.get(p), param.decimals) for p in periods]
            if has_reference else None
        ),
        reference_count=(
            [ref_counts.get(p, 0) for p in periods] if has_reference else None
        ),
        spread_min=[_round(spread[p][0], param.decimals) if p in spread else None
                    for p in periods],
        spread_max=[_round(spread[p][1], param.decimals) if p in spread else None
                    for p in periods],
        spread=[_round(spread[p][2], param.decimals + 1) if p in spread else None
                for p in periods],
        lines=lines,
        rejected_changes=rejected,
    )


def _periods(
    granularity: str,
    days: Sequence[date],
    range_from: datetime,
    range_to: datetime,
) -> list:
    """The rows of the table: a continuous axis, not only the periods that
    happen to have data — a gap has to be visible as a gap."""
    if granularity == "daily":
        return list(days)
    out: list[datetime] = []
    stamp = range_from
    while stamp < range_to:
        out.append(stamp)
        stamp += timedelta(hours=1)
    return out


def _stats_model(stats) -> Optional[FhpLineStats]:
    if stats is None:
        return None
    return FhpLineStats(
        n=stats.n,
        mean_delta=stats.mean_delta,
        mean_abs_delta=stats.mean_abs_delta,
        max_abs_delta=stats.max_abs_delta,
        max_abs_delta_at=_iso(stats.max_abs_delta_at),
        mean_abs_delta_pct=stats.mean_abs_delta_pct,
        max_abs_delta_pct=stats.max_abs_delta_pct,
        max_abs_delta_pct_at=(
            _iso(stats.max_abs_delta_pct_at) if stats.max_abs_delta_pct_at else None
        ),
        out_of_tolerance=stats.out_of_tolerance,
        out_of_tolerance_share=stats.out_of_tolerance_share,
    )


def _iso(period) -> str:
    return period.isoformat()


def _round(value: Optional[float], decimals: int) -> Optional[float]:
    return None if value is None else round(value, decimals)
