"""From what is stored to a trained model, and from a model to what the screen shows.

Everything here works on plain in-memory inputs (`Inputs`), so it can be run
and tested without a database; `load` is the one function that reads one.

Training and analysis are two steps on purpose. A model is trained when an
operator presses the button, and stays as it is until the button is pressed
again — so that a fault which begins tomorrow is measured against a model
that has not already learnt it. Analysis runs on every read: it takes the
stored model and whatever data there is now, including days uploaded after
the training and the days ahead that only have a weather forecast.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import core
from .parse import INFLOW, CONSUMPTION, INFLOW_CATEGORY

#: The district-heating category (kept apart in the frames for reference).
HEAT_CATEGORY = "Тепломережі"
#: The weather a station falls back to: the mean of the branch's points.
BRANCH_WEATHER = "branch"
#: A station that passes less than this share of what the median station
#: does is left out — of the models and of the medians the others are judged
#: against. ГРС Калинівка lets through 0.1 thousand m³ a day where the
#: smallest real one passes 0.6: half its days fail the cleaning, and as a
#: peer it only adds noise to everybody's reference.
TINY_SHARE = 0.05
#: A station's own weather point is used if it covers this share of the days.
POINT_COVERAGE = 0.9
#: 2: one pooled model for the branch and a trend per station.
#: 3: the analysis compares with the model trained on everything.
#: 4: HDD_m10 among the features.
PAYLOAD_VERSION = 4
#: The forecast follows what the whole branch has been doing: the mean of
#: the deviation all stations share over this many last days with facts …
BRANCH_DAYS = 7
#: … carried into the days ahead, fading by this much a day. Tried on
#: 2025–2026 (7/3/14 days, 1.0–0.7): one day ahead 4.7 → 3.4 %, three
#: 4.7 → 3.6 %, a week 4.7 → 4.3 %; spring about as without it.
BRANCH_DECAY = 0.8


class ModelError(ValueError):
    """A sentence for the operator: why there is nothing to train or show."""


@dataclass
class Station:
    id: int
    name: str
    enabled: bool = True
    weather_point_id: int | None = None


@dataclass
class Inputs:
    stations: list[Station]
    #: (station id, day, category, m³)
    volumes: list[tuple[int, date, str, float]]
    #: kind → the days some uploaded report covers
    covered: dict[str, list[date]]
    #: point id → daily weather (core.WEATHER_COLUMNS, plus `forecast`)
    weather: dict[int, pd.DataFrame] = field(default_factory=dict)


@dataclass
class Frames:
    index: pd.DatetimeIndex            # every day from the first figure to the last
    y: pd.DataFrame                    # thousand m³, NaN where unknown
    inflow: pd.DataFrame               # m³
    other: pd.DataFrame                # non-household, m³
    heat: pd.DataFrame                 # heat networks, m³
    weather: dict[str, pd.DataFrame]   # weather key → continuous daily weather
    weather_key: dict[str, str]        # station name → weather key
    tiny: list[str] = field(default_factory=list)   # left out as negligible
    #: False: no consumption report was ever uploaded, and the model is of
    #: the whole inflow.
    with_consumption: bool = True


def weather_frame(rows) -> pd.DataFrame:
    """Rows of weather_daily → a frame in the model's column names."""
    frame = pd.DataFrame(
        [(pd.Timestamp(r.day), r.t_mean, r.t_min, r.t_max, r.wind_max, r.radiation,
          r.precipitation, r.source == "forecast") for r in rows],
        columns=["day", *core.WEATHER_COLUMNS, "forecast"],
    )
    if frame.empty:
        return frame.set_index("day")
    frame = frame.dropna(subset=["T"]).drop_duplicates("day").set_index("day").sort_index()
    return frame


def _continuous(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per calendar day, gaps inside filled in."""
    frame = frame.asfreq("D")
    values = list(core.WEATHER_COLUMNS)
    frame[values] = frame[values].astype(float).interpolate().ffill().bfill()
    frame["forecast"] = frame["forecast"].fillna(False).astype(bool)
    return frame


def build_frames(inputs: Inputs) -> Frames:
    stations = [s for s in inputs.stations if s.enabled]
    if not stations or not inputs.volumes:
        raise ModelError("Немає даних: завантажте звіти 1С — надходження по ГРС і споживання")
    name_of = {s.id: s.name for s in stations}
    long = pd.DataFrame(
        [(pd.Timestamp(day), name_of[sid], category, volume)
         for sid, day, category, volume in inputs.volumes if sid in name_of],
        columns=["day", "grs", "category", "volume"],
    )
    if long.empty:
        raise ModelError("Немає даних по увімкнених ГРС")
    index = pd.date_range(long["day"].min(), long["day"].max(), freq="D")
    names = sorted(name_of.values())

    def table(mask) -> pd.DataFrame:
        part = long[mask]
        if part.empty:
            return pd.DataFrame(np.nan, index=index, columns=names)
        return (part.pivot_table(index="day", columns="grs", values="volume", aggfunc="sum")
                .reindex(index=index, columns=names))

    is_inflow = long["category"] == INFLOW_CATEGORY
    inflow = table(is_inflow)
    other = table(~is_inflow)
    heat = table(long["category"] == HEAT_CATEGORY)

    # Nothing stored for a day a consumption report covers is a zero; for a
    # day no report covers it is unknown, and so is the day's remainder. A
    # branch with no consumption report at all is modelled on its inflow:
    # what is not households then is in the model too, which is coarser but
    # still catches a metering shift.
    with_consumption = bool(inputs.covered.get(CONSUMPTION))
    if with_consumption:
        reported = index.isin(pd.to_datetime(inputs.covered[CONSUMPTION]))
    else:
        reported = np.ones(len(index), dtype=bool)
    other = other.fillna(0.0).where(pd.Series(reported, index=index), axis=0)
    heat = heat.fillna(0.0).where(pd.Series(reported, index=index), axis=0)
    y = (inflow - other) / 1000

    means = y.where(y > 0).mean()
    tiny = sorted(means[(means < TINY_SHARE * means.median()) | means.isna()].index)         if len(means) >= core.MIN_PEERS else []
    if tiny:
        keep = [n for n in names if n not in tiny]
        y, inflow, other, heat = y[keep], inflow[keep], other[keep], heat[keep]

    frames = {pid: f for pid, f in inputs.weather.items() if len(f)}
    if not frames:
        raise ModelError(
            "Немає погоди для цієї філії: додайте точки й завантажте погоду в "
            "«Аналітика → Точки погоди»")
    mean = (pd.concat([f[list(core.WEATHER_COLUMNS)] for f in frames.values()])
            .groupby(level=0).mean())
    mean["forecast"] = pd.concat([f["forecast"] for f in frames.values()]).groupby(level=0).max()
    weather = {BRANCH_WEATHER: _continuous(mean)}
    weather_key = {}
    for station in stations:
        own = frames.get(station.weather_point_id) if station.weather_point_id else None
        if own is not None and len(own.index.intersection(index)) >= POINT_COVERAGE * len(index):
            key = f"point:{station.weather_point_id}"
            weather.setdefault(key, _continuous(own))
        else:
            key = BRANCH_WEATHER
        weather_key[station.name] = key

    # A day the weather does not reach cannot be explained by it.
    first, last = weather[BRANCH_WEATHER].index[0], weather[BRANCH_WEATHER].index[-1]
    y = y.where(pd.Series((index >= first) & (index <= last), index=index), axis=0)
    if y.notna().sum().sum() == 0:
        raise ModelError(
            f"Погода є лише за {first:%d.%m.%Y}–{last:%d.%m.%Y}, а дані ГРС — за інший "
            "період. Довантажте погоду за період даних")
    return Frames(index=index, y=y, inflow=inflow, other=other, heat=heat,
                  weather=weather, weather_key=weather_key, tiny=tiny,
                  with_consumption=with_consumption)


# ── training ───────────────────────────────────────────────────────────────

def _designs(frames: Frames, index: pd.DatetimeIndex, norms: dict, names) -> dict[str, pd.DataFrame]:
    return {name: core.features(frames.weather[frames.weather_key[name]], index,
                                norms[frames.weather_key[name]]) for name in names}


def _trends(deviation: pd.DataFrame, usable: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({name: core.trend(deviation[name], usable[name]) for name in deviation.columns})


def train(inputs: Inputs) -> tuple[dict, dict]:
    """Fit the branch's model. Returns (summary for the screen, payload).

    Two passes. The first fits every station on its own, as the cleaning
    leaves it, only to see where each station's level has moved; the second
    fits all of them together to the volume with that trend taken out.
    """
    frames = build_frames(inputs)
    y = frames.y.where(frames.y > 0)
    index, names = frames.index, list(y.columns)
    cleaning = core.clean(frames.y, frames.weather[BRANCH_WEATHER])
    usable = ~(cleaning.shape | cleaning.missing)
    norms = {key: core.seasonal_norm_fit(w[~w["forecast"]] if (~w["forecast"]).sum() > 365 else w)
             for key, w in frames.weather.items()}
    designs = _designs(frames, index, norms, names)

    # Pass 1: each station alone, for its trend.
    first_ok = ~cleaning.excluded
    alone = pd.DataFrame({n: core.fit(designs[n], y[n], first_ok[n], False)[1] for n in names})
    first_trend = _trends(core.own_deviation(y, alone, usable), usable)
    learn = usable & ~core.off_trend(core.own_deviation(y, alone, usable), first_trend)

    # Pass 2: the branch together, the trend taken out.
    model, base, tried = core.fit_pooled(designs, y * np.exp(-first_trend), learn)
    if model is None:
        raise ModelError(
            f"Жодна ГРС не має {core.MIN_TRAIN_DAYS} чистих днів з погодою — "
            "моделі навчити ні на чому")

    # Quality is measured on months the model did not see: an honest figure
    # of how it does on the days ahead.
    held_deviation = core.own_deviation(y, base, usable)
    held_trends = _trends(held_deviation, usable)
    held_ok = usable & ~core.off_trend(held_deviation, held_trends)
    held_predicted = base * np.exp(held_trends)

    # What the screen compares with is the model trained on everything —
    # the one that forecasts too. On history it has seen each month, but a
    # month is a twentieth of what it learnt from, and the days a fault
    # spoiled were left out of the training: those still stand out. The
    # noise behind the alarm limits is taken from the same model, so the
    # limits match the deviations they judge.
    full = pd.DataFrame({n: model.predict(n, designs[n]) if n in model.own else np.nan * y[n]
                         for n in names})
    deviation = core.own_deviation(y, full, usable)
    trends = _trends(deviation, usable)
    ok = usable & ~core.off_trend(deviation, trends)
    predicted = full * np.exp(trends)
    detection = core.detect(y, predicted, ok)

    listing = []
    for name in names:
        if name not in model.own:
            listing.append({"name": name, "status": "few_days"})
            continue
        level = np.expm1(trends[name])
        known = level[y[name].notna()]
        listing.append({
            "name": name, "status": "ok", "weather": frames.weather_key[name],
            "train_days": int((learn[name] & y[name].notna()).sum()),
            "excluded_share": float((~ok[name] & y[name].notna()).sum() / max(y[name].notna().sum(), 1) * 100),
            "excluded": {"shape": int((cleaning.shape[name] & ~cleaning.missing[name]).sum()),
                         "level": int((~ok[name] & usable[name]).sum()),
                         "missing": int(cleaning.missing[name].sum())},
            "trend_now": _percent(known.iloc[-1]) if len(known) else None,
            "trend_min": _percent(known.min()) if len(known) else None,
            "trend_max": _percent(known.max()) if len(known) else None,
            "features": model.weights(name, designs[name], learn[name] & y[name].notna()),
            **core.quality(y[name], held_predicted[name], held_ok[name]),
        })
    for row in listing:
        sigma = detection.sigma.get(row["name"], {})
        row["sigma_week_heat"] = _percent(sigma.get("week_heat"))
        row["sigma_week_summer"] = _percent(sigma.get("week_summer"))
        row["sigma_day_heat"] = _percent(sigma.get("day_heat"))
        row["sigma_month"] = _percent(sigma.get("month"))
        row["episodes"] = sum(e["grs"] == row["name"] for e in detection.episodes)
    listing.extend({"name": name, "status": "tiny"} for name in frames.tiny)

    summary = {
        "stations": listing,
        "first_day": index[0].date().isoformat(),
        "last_day": index[-1].date().isoformat(),
        "days": int(len(index)),
        "weather_first": frames.weather[BRANCH_WEATHER].index[0].date().isoformat(),
        "weather_last": frames.weather[BRANCH_WEATHER].index[-1].date().isoformat(),
        "with_consumption": frames.with_consumption,
        "pooling": {"strength": model.strength,
                    "tried": {str(k): _round(v, 2) for k, v in tried.items()}},
        "trend_window": core.TREND_WINDOW,
    }
    payload = {
        "version": PAYLOAD_VERSION,
        "days": [d.date().isoformat() for d in index],
        "norms": norms,
        "weather_key": {n: frames.weather_key[n] for n in model.own},
        "model": model.to_json(),
        "sigma": _clean_json(detection.sigma),
    }
    return summary, payload


# ── analysis ───────────────────────────────────────────────────────────────

def analyse(inputs: Inputs, payload: dict, first: date | None = None,
            last: date | None = None) -> dict:
    """What the screen shows: each station against its model, and where it
    departs from it more than the others do."""
    frames = build_frames(inputs)
    branch_weather = frames.weather[BRANCH_WEATHER]
    # Up to where the weather reaches — the forecast ahead of today. The 1С
    # figures may stop weeks earlier; the model goes on, to be compared with
    # the lines that are polled every day.
    end = branch_weather.index[-1]
    index = pd.date_range(frames.index[0], max(end, frames.index[-1]), freq="D")
    y = frames.y.where(frames.y > 0).reindex(index)

    cleaning = core.clean(frames.y, branch_weather)
    if payload.get("version") != PAYLOAD_VERSION:
        raise ModelError("Модель навчено попередньою версією програми — навчіть її знову")

    model = core.Pooled.from_json(payload["model"])
    base = pd.DataFrame(np.nan, index=index, columns=y.columns)
    for name in y.columns:
        if name not in model.own:
            continue
        key = payload["weather_key"].get(name, BRANCH_WEATHER)
        key = key if key in frames.weather else BRANCH_WEATHER
        weather = frames.weather[key]
        norm = payload["norms"].get(key) or payload["norms"][BRANCH_WEATHER]
        series = model.predict(name, core.features(weather, index, norm))
        series[(index < weather.index[0]) | (index > weather.index[-1])] = np.nan
        base[name] = series

    usable = (~(cleaning.shape | cleaning.missing)).reindex(index, fill_value=False) & y.notna()
    deviation = core.own_deviation(y, base, usable)
    trends = _trends(deviation, usable)
    ok = usable & ~core.off_trend(deviation, trends)
    predicted = base * np.exp(trends)

    detection = core.detect(y, predicted, ok, sigma=_sigma(payload, y.columns))

    lo = pd.Timestamp(first) if first else index[0]
    hi = pd.Timestamp(last) if last else index[-1]
    window = (index >= lo) & (index <= hi)
    days = index[window]
    forecast = branch_weather["forecast"].reindex(index).fillna(False)
    last_fact = y.notna().any(axis=1)
    last_fact_day = last_fact[last_fact].index[-1] if last_fact.any() else None

    # The days ahead: the model, moved by what the whole branch has been
    # doing lately — when every station has been running 30 % over its model
    # for a week (heating switched on in a warm October week), so will the
    # next days, less and less the further ahead. One branch figure, so no
    # station's own fault is carried forward.
    judged = y.notna().sum(axis=1) >= core.MIN_PEERS
    recent = detection.common[judged & (index <= (last_fact_day or index[0]))].tail(BRANCH_DAYS)
    branch_level = float(recent.mean()) if len(recent) else 0.0
    ahead_days = pd.Series(np.nan, index=index)
    if last_fact_day is not None:
        after = index > last_fact_day
        ahead_days[after] = (index[after] - last_fact_day).days
    correction = (branch_level * BRANCH_DECAY ** ahead_days).fillna(0.0)
    ahead_mask = pd.DataFrame(np.repeat(ahead_days.notna().values[:, None], len(y.columns), axis=1),
                              index=index, columns=y.columns)
    detection.expected = detection.expected.mask(ahead_mask, predicted.mul(np.exp(correction), axis=0))

    reason = pd.DataFrame("", index=index, columns=y.columns)
    for label, flags in (("missing", cleaning.missing.reindex(index, fill_value=False)),
                         ("level", ~ok & usable),
                         ("shape", cleaning.shape.reindex(index, fill_value=False))):
        reason = reason.mask(flags, label)

    out_stations = []
    for name in y.columns:
        has_model = name in model.own
        out_stations.append({
            "name": name,
            "has_model": has_model,
            "y": _series_json(y.loc[window, name]),
            "expected": _series_json(detection.expected.loc[window, name]),
            "model": _series_json(predicted.loc[window, name]),
            "deviation": _series_json(detection.deviation.loc[window, name] * 100, 1),
            "week": _series_json(detection.week.loc[window, name] * 100, 1),
            "week_limit": _series_json(detection.week_limit.loc[window, name] * 100, 1),
            "level": _series_json(detection.level.loc[window, name] * 100, 1),
            "trend": _series_json(np.expm1(trends.loc[window, name]) * 100, 1),
            "day_alarm": _flags_json(detection.day_alarm.loc[window, name]),
            "week_alarm": _flags_json(detection.week_alarm.loc[window, name]),
            "excluded": [r or None for r in reason.loc[window, name]],
            "month_limit": round(core.month_limit(detection.sigma, name) * 100, 1) if has_model else None,
        })

    modelled = [n for n in y.columns if n in model.own]
    both = y[modelled].notna() & detection.expected[modelled].notna()
    total_y = y[modelled].where(both).sum(axis=1, min_count=1)
    total_expected = detection.expected[modelled].where(both).sum(axis=1, min_count=1)
    ahead = detection.expected[modelled].where(~y[modelled].notna())
    total_ahead = ahead.sum(axis=1, min_count=len(modelled)) if modelled else pd.Series(np.nan, index=index)

    months = detection.month
    month_rows = []
    for period in months.index:
        if period.end_time < lo or period.start_time > hi:
            continue
        month_rows.append({
            "month": str(period),
            "values": {n: _round(months.loc[period, n] * 100, 1) for n in modelled},
        })

    episodes = [
        {**e, "start": e["start"].isoformat(), "end": e["end"].isoformat(),
         "deviation": _round(e["deviation"], 1), "volume": _round(e["volume"], 1),
         "common": _round(e["common"], 1),
         "open": bool(last_fact_day is not None and pd.Timestamp(e["end"]) >= last_fact_day)}
        for e in detection.episodes
        if pd.Timestamp(e["end"]) >= lo and pd.Timestamp(e["start"]) <= hi
    ]
    return {
        "days": [d.date().isoformat() for d in days],
        "forecast": _flags_json(forecast[window]),
        "last_fact_day": last_fact_day.date().isoformat() if last_fact_day is not None else None,
        "trained_first": payload["days"][0], "trained_last": payload["days"][-1],
        "common": _series_json(np.expm1(detection.common[window]) * 100, 1),
        # What the forecast was moved by on each day ahead, % (0 on days with facts).
        "branch_correction": _series_json(np.expm1(correction[window]) * 100, 1),
        "branch_level": _round(np.expm1(branch_level) * 100, 1),
        "stations": out_stations,
        "total": {"y": _series_json(total_y[window]), "expected": _series_json(total_expected[window]),
                  "ahead": _series_json(total_ahead[window])},
        "months": month_rows,
        "episodes": sorted(episodes, key=lambda e: (e["start"], e["grs"])),
    }


def _sigma(payload: dict, names) -> dict | None:
    """The noise levels of the training run, for the stations that have them.
    A station that appeared after the training has none; then the detector
    falls back to estimating all of them from what there is now."""
    stored = payload.get("sigma") or {}
    if not all(n in stored for n in names):
        return None
    return {n: {k: (np.nan if v is None else v) for k, v in stored[n].items()} for n in names}


# ── JSON helpers ───────────────────────────────────────────────────────────

def _round(value, digits: int = 3):
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    try:
        if not np.isfinite(value):
            return None
    except TypeError:
        return None
    return round(float(value), digits)


def _series_json(series: pd.Series, digits: int = 3) -> list:
    return [_round(v, digits) for v in series.values]


def _flags_json(series: pd.Series) -> list:
    return [bool(v) for v in series.values]


def _percent(value):
    return _round(None if value is None else value * 100, 2)


def _clean_json(value):
    if isinstance(value, dict):
        return {k: _clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_json(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    return value
