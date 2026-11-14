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

#: The district-heating category: what it burns tells when heating is on.
HEAT_CATEGORY = "Тепломережі"
#: The weather a station falls back to: the mean of the branch's points.
BRANCH_WEATHER = "branch"
#: A station is offered the heating terms when heat networks take at least
#: this share of what it lets through …
HEATING_SHARE = 0.05
#: … and keeps them only if they cut its monthly error by this much.
HEATING_GAIN = 0.05
#: A station that passes less than this share of what the median station
#: does is left out — of the models and of the medians the others are judged
#: against. ГРС Калинівка lets through 0.1 thousand m³ a day where the
#: smallest real one passes 0.6: half its days fail the cleaning, and as a
#: peer it only adds noise to everybody's reference.
TINY_SHARE = 0.05
#: A station's own weather point is used if it covers this share of the days.
POINT_COVERAGE = 0.9
PAYLOAD_VERSION = 1


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
    # day no report covers it is unknown, and so is the day's remainder.
    reported = index.isin(pd.to_datetime(inputs.covered.get(CONSUMPTION, [])))
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
                  weather=weather, weather_key=weather_key, tiny=tiny)


def _heating(frames: Frames, name: str, index: pd.DatetimeIndex,
             reference: float | None = None) -> tuple[pd.Series, float]:
    """District heating on/off over `index`; the last known state carries on
    where no consumption report reaches yet."""
    flag, reference = core.heating_flag(frames.heat[name].dropna(), reference)
    return flag.reindex(index).ffill().bfill().fillna(0).astype(int), reference


# ── training ───────────────────────────────────────────────────────────────

def train(inputs: Inputs) -> tuple[dict, dict]:
    """Fit a model per station. Returns (summary for the screen, payload)."""
    frames = build_frames(inputs)
    y = frames.y.where(frames.y > 0)
    cleaning = core.clean(frames.y, frames.weather[BRANCH_WEATHER])
    ok = ~cleaning.excluded
    norms = {key: core.seasonal_norm_fit(w[~w["forecast"]] if (~w["forecast"]).sum() > 365 else w)
             for key, w in frames.weather.items()}

    held_out = pd.DataFrame(np.nan, index=frames.index, columns=y.columns)
    models: dict[str, dict] = {}
    listing = []
    for name in y.columns:
        key = frames.weather_key[name]
        weather, norm = frames.weather[key], norms[key]
        fitted, predicted = core.fit(core.features(weather, frames.index, norm), y[name], ok[name], False)
        variant = "plain"
        inflow_total = frames.inflow[name].sum()
        if fitted and inflow_total and frames.heat[name].sum() / inflow_total >= HEATING_SHARE:
            flag, reference = _heating(frames, name, frames.index)
            with_heat, predicted_heat = core.fit(
                core.features(weather, frames.index, norm, flag), y[name], ok[name], True, reference)
            if with_heat and _better(with_heat, fitted):
                fitted, predicted, variant = with_heat, predicted_heat, "heating"
        held_out[name] = predicted
        models[name] = {"weather": key, "variant": variant,
                        "model": _fitted_json(fitted) if fitted else None}
        listing.append({
            "name": name, "status": "ok" if fitted else "few_days",
            "variant": variant, "weather": key,
            "train_days": int((ok[name] & y[name].notna()).sum()),
            "excluded_share": float(cleaning.excluded[name].mean() * 100),
            "excluded": {"shape": int(cleaning.shape[name].sum()),
                         "level": int(cleaning.level[name].sum()),
                         "missing": int(cleaning.missing[name].sum())},
            **(fitted.metrics if fitted else {}),
        })

    detection = core.detect(y, held_out, ok)
    listing.extend({"name": name, "status": "tiny"} for name in frames.tiny)
    for row in listing:
        sigma = detection.sigma.get(row["name"], {})
        row["sigma_week_heat"] = _percent(sigma.get("week_heat"))
        row["sigma_week_summer"] = _percent(sigma.get("week_summer"))
        row["sigma_day_heat"] = _percent(sigma.get("day_heat"))
        row["sigma_month"] = _percent(sigma.get("month"))
        row["episodes"] = sum(e["grs"] == row["name"] for e in detection.episodes)

    trained = [r for r in listing if r["status"] == "ok"]
    if not trained:
        raise ModelError(
            f"Жодна ГРС не має {core.MIN_TRAIN_DAYS} чистих днів з погодою — "
            "моделі навчити ні на чому")
    summary = {
        "stations": listing,
        "first_day": frames.index[0].date().isoformat(),
        "last_day": frames.index[-1].date().isoformat(),
        "days": int(len(frames.index)),
        "weather_first": frames.weather[BRANCH_WEATHER].index[0].date().isoformat(),
        "weather_last": frames.weather[BRANCH_WEATHER].index[-1].date().isoformat(),
    }
    payload = {
        "version": PAYLOAD_VERSION,
        "days": [d.date().isoformat() for d in frames.index],
        "norms": norms,
        "stations": models,
        "held_out": {name: _series_json(held_out[name]) for name in y.columns},
        "sigma": _clean_json(detection.sigma),
    }
    return summary, payload


def _better(candidate: core.Fitted, base: core.Fitted) -> bool:
    new, old = candidate.metrics.get("month_error"), base.metrics.get("month_error")
    if new is None or old is None:
        return False
    return new < old * (1 - HEATING_GAIN)


def _fitted_json(model: core.Fitted) -> dict:
    return {"columns": model.columns, "coef": model.coef, "intercept": model.intercept,
            "uses_heating": model.uses_heating, "heating_reference": model.heating_reference,
            "train_days": model.train_days, "metrics": model.metrics}


def _fitted(data: dict) -> core.Fitted:
    return core.Fitted(**data)


# ── analysis ───────────────────────────────────────────────────────────────

#: How far past the last figure the forecast is shown.
FORECAST_DAYS = 16


def analyse(inputs: Inputs, payload: dict, first: date | None = None,
            last: date | None = None) -> dict:
    """What the screen shows: each station against its model, and where it
    departs from it more than the others do."""
    frames = build_frames(inputs)
    branch_weather = frames.weather[BRANCH_WEATHER]
    end = min(branch_weather.index[-1], frames.index[-1] + pd.Timedelta(days=FORECAST_DAYS))
    index = pd.date_range(frames.index[0], max(end, frames.index[-1]), freq="D")
    y = frames.y.where(frames.y > 0).reindex(index)

    cleaning = core.clean(frames.y, branch_weather)
    ok = (~cleaning.excluded).reindex(index, fill_value=False)

    trained_days = pd.to_datetime(payload["days"])
    predicted = pd.DataFrame(np.nan, index=index, columns=y.columns)
    stations = payload["stations"]
    for name in y.columns:
        entry = stations.get(name)
        if not entry or not entry.get("model"):
            continue
        model = _fitted(entry["model"])
        key = entry["weather"] if entry["weather"] in frames.weather else BRANCH_WEATHER
        weather = frames.weather[key]
        norm = payload["norms"].get(entry["weather"]) or payload["norms"][BRANCH_WEATHER]
        flag = None
        if model.uses_heating:
            flag, _ = _heating(frames, name, index, model.heating_reference)
        design = core.features(weather, index, norm, flag)
        series = model.predict(design)
        series[(index < weather.index[0]) | (index > weather.index[-1])] = np.nan
        # Inside the period the model was trained on, the figure to compare
        # with is the one made while that month was held out — not the fit,
        # which has already seen the day.
        held = pd.Series(payload["held_out"].get(name, []), index=trained_days[:len(payload["held_out"].get(name, []))], dtype=float)
        held = held.reindex(index)
        predicted[name] = held.where(held.notna(), series)

    detection = core.detect(y, predicted, ok, sigma=_sigma(payload, y.columns))

    lo = pd.Timestamp(first) if first else index[0]
    hi = pd.Timestamp(last) if last else index[-1]
    window = (index >= lo) & (index <= hi)
    days = index[window]
    forecast = branch_weather["forecast"].reindex(index).fillna(False)
    last_fact = y.notna().any(axis=1)
    last_fact_day = last_fact[last_fact].index[-1] if last_fact.any() else None

    reason = pd.DataFrame("", index=index, columns=y.columns)
    for label, flags in (("missing", cleaning.missing), ("level", cleaning.level),
                         ("shape", cleaning.shape)):
        reason = reason.mask(flags.reindex(index, fill_value=False), label)

    out_stations = []
    for name in y.columns:
        has_model = bool(stations.get(name, {}).get("model"))
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
            "day_alarm": _flags_json(detection.day_alarm.loc[window, name]),
            "week_alarm": _flags_json(detection.week_alarm.loc[window, name]),
            "excluded": [r or None for r in reason.loc[window, name]],
            "month_limit": round(core.month_limit(detection.sigma, name) * 100, 1) if has_model else None,
        })

    modelled = [n for n in y.columns if stations.get(n, {}).get("model")]
    both = y[modelled].notna() & detection.expected[modelled].notna()
    total_y = y[modelled].where(both).sum(axis=1, min_count=1)
    total_expected = detection.expected[modelled].where(both).sum(axis=1, min_count=1)
    ahead = predicted[modelled].where(~y[modelled].notna())
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
