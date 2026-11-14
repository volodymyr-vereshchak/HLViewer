"""Cleaning, the weather model of a GRS, and the detector of metering trouble.

Pure pandas/numpy: frames in, frames out, no database. The method was worked
out on the Zaporizhzhia branch for 2025–2026 (13 GRS, 22 months) and is kept
as it was validated there; the numbers in the comments are from that study.

The series, per GRS and day, in thousand m³:

    y = inflow − non-household consumption        (households + losses)

**Cleaning.** Days with a metering fault must not teach the model what normal
looks like. Two tests, both against the other GRS of the branch, because a
cold snap moves all of them together and a fault moves one:

* shape — the day over the GRS's monthly median, against the same for the
  median GRS; a 5-day median off by more than 20 % (35 % in summer), or a
  single day off by 50 % (100 %);
* level — the day over a crude degree-day fit of two years, against the same
  ratio of the median GRS, as a 15-day median; off by more than 15 % (30 %),
  or with fewer than eight readings in those fifteen days to be judged by.

**Model.** Ordinary least squares on about twenty physical features of the
weather (see `features`). No time trend: a trend hides exactly the slow drift
the detector is for. Quality is measured leave-one-month-out, on clean days.

**Detector.** log(actual / expected) minus its median over all GRS that day —
which removes the error every GRS shares (the weather the model missed, a
power cut, a holiday) and leaves what is this GRS's own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from sklearn.linear_model import HuberRegressor, LinearRegression

#: The months heating runs in: thresholds are tighter there, where the signal
#: is strong, and loose in summer, where a village GRS is mostly noise.
HEAT_MONTHS = (10, 11, 12, 1, 2, 3, 4)

#: Fewer clean days than this and a model is not fitted at all.
MIN_TRAIN_DAYS = 200
#: A month counts towards the monthly error with at least this many clean days.
MIN_MONTH_DAYS = 15
#: Cross-GRS medians mean something from this many GRS up.
MIN_PEERS = 3

WEATHER_COLUMNS = ("T", "Tmin", "Tmax", "wind", "rad", "prec")


def is_heat(index: pd.DatetimeIndex) -> np.ndarray:
    return index.month.isin(HEAT_MONTHS)


# ── cleaning ───────────────────────────────────────────────────────────────

@dataclass
class Cleaning:
    shape: pd.DataFrame          # day off the common curve
    level: pd.DataFrame          # level off the GRS's own two years
    missing: pd.DataFrame        # no figure, or not above zero
    #: Relative deviations behind the flags, for showing why a day was dropped.
    shape_dev: pd.DataFrame
    level_dev: pd.DataFrame

    @property
    def excluded(self) -> pd.DataFrame:
        return self.shape | self.level | self.missing


def _degree_days(weather: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    return np.maximum(17.5 - weather["T"].ewm(alpha=0.5).mean(), 0).reindex(index)


def clean(y: pd.DataFrame, weather: pd.DataFrame) -> Cleaning:
    """Which days are not to be learnt from, and why."""
    y = y.where(y > 0)
    missing = y.isna()
    heat = is_heat(y.index)
    months = y.index.to_period("M")
    enough = y.shape[1] >= MIN_PEERS

    # Shape: the day against the month, against the same for the median GRS.
    index = y / y.groupby(months).transform("median")
    common = index.median(axis=1) if enough else pd.Series(1.0, index=y.index)
    ratio = np.log(index.div(common, axis=0))
    day_dev = np.exp(ratio) - 1
    run_dev = np.exp(ratio.rolling(5, center=True, min_periods=3).median()) - 1
    run_limit = pd.Series(np.where(heat, 0.20, 0.35), index=y.index)
    day_limit = pd.Series(np.where(heat, 0.50, 1.00), index=y.index)
    shape = run_dev.abs().gt(run_limit, axis=0) | day_dev.abs().gt(day_limit, axis=0)
    # The edges of a fault are faulty too.
    shape = shape | shape.shift(1, fill_value=False) | shape.shift(-1, fill_value=False)

    # Level: the day against a robust degree-day fit of the whole history.
    degree_days = _degree_days(weather, y.index).fillna(0.0)
    fitted = pd.DataFrame(index=y.index, columns=y.columns, dtype=float)
    for grs in y.columns:
        series = y[grs]
        known = series.notna()
        if known.sum() < 30:
            continue
        scale = series[known].mean()
        try:
            model = HuberRegressor(epsilon=1.35, max_iter=500).fit(
                degree_days[known].to_frame() / 10, series[known] / scale)
        except ValueError:
            continue
        fitted[grs] = series / (model.predict(degree_days.to_frame() / 10) * scale)
    relative = fitted.div(fitted.median(axis=1), axis=0) if enough else fitted
    smooth = relative.rolling(15, center=True, min_periods=8).median()
    level_limit = pd.Series(np.where(heat, 0.15, 0.30), index=y.index)
    level = (smooth - 1).abs().gt(level_limit, axis=0)
    # A reading with too few neighbours to be judged by is not trusted either:
    # a station that has all but stopped still reports a stray day now and
    # then (ГРС Димитрове after 29.03.2026 — 0.2 thousand m³ where it used to
    # pass 7), and each of those would otherwise go into the fit as a clean day.
    level = level | smooth.isna()

    return Cleaning(shape=shape & ~missing, level=level & ~missing, missing=missing,
                    shape_dev=run_dev, level_dev=smooth - 1)


# ── features ───────────────────────────────────────────────────────────────

def seasonal_norm_fit(weather: pd.DataFrame) -> list[float]:
    """The normal temperature by day of year: two harmonics, least squares."""
    known = weather["T"].dropna()
    design = _harmonics(known.index)
    return np.linalg.lstsq(design, known.values, rcond=None)[0].tolist()


def _harmonics(index: pd.DatetimeIndex) -> np.ndarray:
    day = index.dayofyear.values
    return np.c_[np.ones(len(index)),
                 *[f(2 * np.pi * k * day / 365.25) for k in (1, 2) for f in (np.sin, np.cos)]]


def _asymmetric_mean(temperature: pd.Series, warming: float = 0.4, cooling: float = 0.6) -> pd.Series:
    """What a building feels: it warms up slower than it cools down."""
    values = temperature.values
    out = np.empty(len(values))
    out[0] = values[0]
    for i in range(1, len(values)):
        rate = warming if values[i] > out[i - 1] else cooling
        out[i] = out[i - 1] + rate * (values[i] - out[i - 1])
    return pd.Series(out, index=temperature.index)


def orthodox_easter(year: int) -> date:
    """Easter Sunday by the Julian reckoning, as a Gregorian date."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    julian = date(year, month, day + 1)
    return julian + pd.Timedelta(days=13).to_pytimedelta()


def _holidays(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """Christmas to New Year (24.12–02.01), and Easter Sunday alone."""
    xmas = ((index.month == 12) & (index.day >= 24)) | ((index.month == 1) & (index.day <= 2))
    easter = {orthodox_easter(year) for year in set(index.year)}
    return xmas.astype(int), np.array([d.date() in easter for d in index], dtype=int)


def _days_since(events: pd.Series, default_start_month: int = 11) -> pd.Series:
    """Days since the last True in `events`; before any, since 1 November."""
    out = []
    last = None
    for stamp, event in zip(events.index, events.values):
        if event:
            last = stamp
        if last is not None:
            out.append((stamp - last).days)
        else:
            year = stamp.year if stamp.month >= default_start_month else stamp.year - 1
            out.append((stamp - pd.Timestamp(year, default_start_month, 1)).days)
    return pd.Series(out, index=events.index)


def features(weather: pd.DataFrame, index: pd.DatetimeIndex, norm: list[float],
             heating: pd.Series | None = None) -> pd.DataFrame:
    """The design matrix for the days in `index`.

    `weather` is daily, continuous, and starts before `index` where it can —
    the smoothed temperatures need a run-up. `heating` is 1 on the days
    district heating is on at a GRS that has it, None for one that has not:
    the two get different autumn terms.

    CW, the temperature a building responds to, is 0.85 of an asymmetric
    running mean and 0.15 of the seasonal normal — the British composite
    weather variable. Degree days are counted from 15.5 °C with kinks at 4 and
    8, each scaled by wind and sun; `pw` blends the winter terms out and the
    summer ones in around a weekly mean of 12 °C, so the two regimes meet
    without a step.
    """
    weather = weather.copy()
    weather[list(WEATHER_COLUMNS)] = weather[list(WEATHER_COLUMNS)].interpolate().ffill().bfill()
    season = pd.Series(_harmonics(weather.index) @ np.array(norm), index=weather.index)
    felt = _asymmetric_mean(weather["T"])
    composite = (0.85 * felt + 0.15 * season).reindex(index)
    felt = felt.reindex(index)
    day = weather.reindex(index)

    degree = np.maximum(15.5 - composite, 0)
    autumn = (index.month >= 7).astype(int)
    weekend = (index.dayofweek >= 5).astype(int)
    weekly = composite.rolling(7, min_periods=1).mean()
    winter = 1 / (1 + np.exp((weekly - 12) / 2))
    on = heating.reindex(index).fillna(0).astype(int) if heating is not None else None

    linear = pd.DataFrame({
        "HDD": degree, "HDDw": degree * day["wind"], "HDDr": degree * day["rad"],
        "wknd": weekend, "h4": np.maximum(degree - 4, 0), "h8": np.maximum(degree - 8, 0),
        "HDDaut": degree * autumn,
    }, index=index)
    if on is not None:
        linear["CO"] = on
        linear["HDDco"] = degree * on
    design = linear.mul(winter, axis=0).add_suffix("_w")
    design["Twarm_s"] = np.maximum(felt - 12, 0) * (1 - winter)
    design["wknd_s"] = weekend * (1 - winter)
    design["HDD_s"] = degree * (1 - winter)
    design["pw"] = winter

    def smooth(series: pd.Series, alpha: float) -> pd.Series:
        return series.ewm(alpha=alpha, adjust=False).mean()

    design["HDD_Tmax22"] = np.maximum(22 - smooth(weather["Tmax"], 0.3), 0).reindex(index)
    design["warm3"] = (weather["Tmax"] > 15).rolling(3, min_periods=1).sum().reindex(index)
    design["HDD_Tmin10"] = np.maximum(10 - smooth(weather["Tmin"], 0.5), 0).reindex(index)
    cold12 = np.maximum(12 - smooth(weather["T"], 0.3), 0).reindex(index)
    if on is not None:
        since_on = _days_since(on.diff() == 1)
        design["first30_CO"] = ((on == 1) & (since_on < 30)).astype(int)
        design["cold_preCO"] = cold12 * ((on == 0) & (index.month >= 9))
    else:
        design["cold_aut"] = cold12 * autumn
    design["hol_xmas"], design["hol_easter"] = _holidays(index)
    return design.fillna(0.0)


def heating_flag(heat_volume: pd.Series, reference: float | None = None) -> tuple[pd.Series, float]:
    """District heating on/off from what the heat networks burn.

    On when above 15 % of the January–February median. Returns the flag and
    that median, so that later days are judged against the same reference.
    """
    if reference is None:
        winter = heat_volume[heat_volume.index.month.isin([1, 2])]
        reference = float(winter.median()) if len(winter) else 0.0
    if not reference or reference <= 0:
        return pd.Series(0, index=heat_volume.index), 0.0
    return (heat_volume > reference * 0.15).astype(int), reference


# ── the model ──────────────────────────────────────────────────────────────

@dataclass
class Fitted:
    """One GRS's model: enough to predict any day from the weather."""

    columns: list[str]
    coef: list[float]
    intercept: float
    uses_heating: bool
    heating_reference: float = 0.0
    train_days: int = 0
    metrics: dict = field(default_factory=dict)

    def predict(self, design: pd.DataFrame) -> pd.Series:
        values = design[self.columns].values @ np.array(self.coef) + self.intercept
        return pd.Series(values, index=design.index).clip(lower=0.01)


def leave_one_month_out(design: pd.DataFrame, y: pd.Series, ok: pd.Series) -> pd.Series:
    """Each month predicted by a model that never saw it."""
    months = design.index.to_period("M")
    out = pd.Series(np.nan, index=design.index)
    for month in sorted(set(months)):
        train = ok & (months != month)
        test = months == month
        if train.sum() < MIN_TRAIN_DAYS:
            continue
        out[test] = LinearRegression().fit(design[train], y[train]).predict(design[test])
    return out.clip(lower=0.01)


def quality(y: pd.Series, predicted: pd.Series, ok: pd.Series) -> dict:
    """How well a model did on the clean days it did not see."""
    use = ok & predicted.notna() & y.notna()
    if use.sum() < 30:
        return {}
    frame = pd.DataFrame({"y": y[use], "p": predicted[use]})
    months = frame.index.to_period("M")
    monthly = frame.groupby(months).sum()[frame.groupby(months).size() >= MIN_MONTH_DAYS]
    bias = (monthly["y"] / monthly["p"] - 1) * 100
    heat = is_heat(frame.index)
    relative = (frame["y"] - frame["p"]).abs() / frame["y"]
    spread = ((frame["y"] - frame["y"].mean()) ** 2).sum()
    return {
        "r2": float(1 - ((frame["y"] - frame["p"]) ** 2).sum() / spread) if spread else None,
        "month_error": float(bias.abs().mean()) if len(bias) else None,
        "months": int(len(bias)),
        "months_within_6": int((bias.abs() <= 6).sum()),
        "mape_heat": float(relative[heat].mean() * 100) if heat.any() else None,
        "mape_summer": float(relative[~heat].mean() * 100) if (~heat).any() else None,
        "mae": float((frame["y"] - frame["p"]).abs().mean()),
        "mean": float(frame["y"].mean()),
        "days": int(use.sum()),
    }


def fit(design: pd.DataFrame, y: pd.Series, ok: pd.Series, uses_heating: bool,
        heating_reference: float = 0.0) -> tuple[Fitted | None, pd.Series]:
    """A model on every clean day, and its leave-one-month-out predictions."""
    train = ok & y.notna()
    if train.sum() < MIN_TRAIN_DAYS:
        return None, pd.Series(np.nan, index=design.index)
    held_out = leave_one_month_out(design, y, train)
    model = LinearRegression().fit(design[train], y[train])
    fitted = Fitted(columns=list(design.columns), coef=model.coef_.tolist(),
                    intercept=float(model.intercept_), uses_heating=uses_heating,
                    heating_reference=heating_reference, train_days=int(train.sum()),
                    metrics=quality(y, held_out, train))
    return fitted, held_out


# ── the detector ───────────────────────────────────────────────────────────

def _robust_sigma(series: pd.Series) -> float:
    series = series.dropna()
    if len(series) < 10:
        return float("nan")
    return float(1.4826 * (series - series.median()).abs().median())


@dataclass
class Detection:
    expected: pd.DataFrame       # the model, times the error all GRS share
    common: pd.Series            # that shared error, as a log ratio
    deviation: pd.DataFrame      # (actual − expected) / expected, by day
    week: pd.DataFrame           # the same over seven days
    day_alarm: pd.DataFrame
    week_alarm: pd.DataFrame
    day_limit: pd.DataFrame
    week_limit: pd.DataFrame
    level: pd.DataFrame          # slow drift: 60-day median, against the others
    month: pd.DataFrame          # deviation by calendar month
    episodes: list[dict]
    sigma: dict                  # per GRS: day/week, heat/summer, month


def detect(y: pd.DataFrame, predicted: pd.DataFrame, ok: pd.DataFrame,
           sigma: dict | None = None) -> Detection:
    """Where each GRS departs from its model more than the others do.

    `sigma` are the noise levels of an earlier run (the one the model was
    trained on); given, the thresholds stay what they were then instead of
    being re-estimated from data that may include the very fault looked for.
    """
    index = y.index
    months = index.to_period("M")
    heat = pd.Series(is_heat(index), index=index)
    ok = ok & y.notna() & predicted.notna()
    log_ratio = np.log((y / predicted).clip(lower=0.05))
    common = log_ratio.where(ok).median(axis=1).fillna(0.0) if y.shape[1] >= MIN_PEERS \
        else pd.Series(0.0, index=index)
    deviation = np.expm1(log_ratio.sub(common, axis=0))
    expected = predicted.mul(np.exp(common), axis=0)

    difference = y - expected
    week = (difference.rolling(7, min_periods=5).sum()
            / expected.rolling(7, min_periods=5).sum())
    monthly = ((y.groupby(months).sum(min_count=20) - expected.where(y.notna()).groupby(months).sum())
               / expected.where(y.notna()).groupby(months).sum())

    if sigma is None:
        local = deviation.where(ok).rolling(61, center=True, min_periods=20).median()
        day_noise = deviation - local
        week_noise = week - week.where(ok).rolling(61, center=True, min_periods=20).median()
        clean_month = ok.groupby(months).mean() > 0.8
        month_noise = monthly - monthly.where(clean_month).rolling(5, center=True, min_periods=3).median()
        sigma = {}
        for grs in y.columns:
            sigma[grs] = {
                "day_heat": _robust_sigma(day_noise.loc[ok[grs] & heat, grs]),
                "day_summer": _robust_sigma(day_noise.loc[ok[grs] & ~heat, grs]),
                "week_heat": _robust_sigma(week_noise.loc[ok[grs] & heat, grs]),
                "week_summer": _robust_sigma(week_noise.loc[ok[grs] & ~heat, grs]),
                "month": max(_nan_to(_robust_sigma(month_noise.loc[clean_month[grs], grs]), 0.03), 0.03),
            }

    def limits(kind: str, floor_heat: float, floor_summer: float) -> pd.DataFrame:
        floor = np.where(heat, floor_heat, floor_summer)
        out = {}
        for grs in y.columns:
            noise = np.where(heat, _nan_to(sigma[grs][f"{kind}_heat"], 0.0),
                             _nan_to(sigma[grs][f"{kind}_summer"], 0.0))
            out[grs] = np.maximum(3 * noise, floor)
        return pd.DataFrame(out, index=index)

    day_limit = limits("day", 0.20, 0.40)
    week_limit = limits("week", 0.10, 0.25)
    day_alarm = deviation.abs() > day_limit
    week_alarm = week.abs() > week_limit

    episodes = []
    for grs in y.columns:
        flag = week_alarm[grs]
        runs = (flag != flag.shift()).cumsum()[flag]
        for _, run in flag[flag].groupby(runs):
            start = max(run.index[0] - pd.Timedelta(days=6), index[0])
            end = run.index[-1]
            volume = difference.loc[start:end, grs].sum()
            base = expected.loc[start:end, grs].where(y.loc[start:end, grs].notna()).sum()
            episodes.append({
                "grs": grs, "start": start.date(), "end": end.date(),
                "days": (end - start).days + 1,
                "deviation": float(volume / base * 100) if base else None,
                "volume": float(volume),
                "common": float(np.expm1(common.loc[start:end].mean()) * 100),
            })

    level = np.expm1(deviation_log(log_ratio, common).where(ok)
                     .rolling(60, center=True, min_periods=20).median())
    return Detection(expected=expected, common=common, deviation=deviation, week=week,
                     day_alarm=day_alarm, week_alarm=week_alarm, day_limit=day_limit,
                     week_limit=week_limit, level=level, month=monthly, episodes=episodes,
                     sigma=sigma)


def deviation_log(log_ratio: pd.DataFrame, common: pd.Series) -> pd.DataFrame:
    return log_ratio.sub(common, axis=0)


def _nan_to(value: float, default: float) -> float:
    return default if value is None or np.isnan(value) else float(value)


def month_limit(sigma: dict, grs: str) -> float:
    """A month is off when it is beyond 2.5 σ of the GRS's months, or 8 %."""
    return max(2.5 * sigma[grs]["month"], 0.08)
