"""Feature experiment for the GRS consumption model — read-only, nothing stored.

Runs the training pipeline of services/grs_model/service.train with the
feature function swapped, and scores each variant on held-out months:
the yearly monthly error, and October–November separately (monthly, 7-day and
the branch-wide common error). Run inside fastapi_app_v2:

    docker exec -i fastapi_app_v2 python - < feature_experiment.py
"""
import asyncio
import json
import sys
import time

import numpy as np
import pandas as pd

from backend.api.endpoints import grs_model_ep as ep
from backend.db.engine import async_session_factory
from backend.services.grs_model import core, service

BRANCH = 9
AUTUMN = (10, 11)


async def load():
    async with async_session_factory() as session:
        return await ep.load_inputs(session, BRANCH)


# ── a parameterised copy of core.features ────────────────────────────────

def make_features(base=15.5, blend=0.85, warming=0.4, cooling=0.6, switch=12.0,
                  drop=(), add=()):
    def features(weather, index, norm, heating=None):
        weather = weather.copy()
        cols = list(core.WEATHER_COLUMNS)
        weather[cols] = weather[cols].interpolate().ffill().bfill()
        season = pd.Series(core._harmonics(weather.index) @ np.array(norm), index=weather.index)
        felt_all = core._asymmetric_mean(weather["T"], warming, cooling)
        composite = (blend * felt_all + (1 - blend) * season).reindex(index)
        felt = felt_all.reindex(index)
        day = weather.reindex(index)
        degree = np.maximum(base - composite, 0)
        autumn = (index.month >= 7).astype(int)
        weekend = (index.dayofweek >= 5).astype(int)
        weekly = composite.rolling(7, min_periods=1).mean()
        winter = 1 / (1 + np.exp((weekly - switch) / 2))
        linear = pd.DataFrame({
            "HDD": degree, "HDDw": degree * day["wind"], "HDDr": degree * day["rad"],
            "wknd": weekend, "h4": np.maximum(degree - 4, 0), "h8": np.maximum(degree - 8, 0),
            "HDDaut": degree * autumn,
        }, index=index)
        design = linear.mul(winter, axis=0).add_suffix("_w")
        design["Twarm_s"] = np.maximum(felt - 12, 0) * (1 - winter)
        design["wknd_s"] = weekend * (1 - winter)
        design["HDD_s"] = degree * (1 - winter)
        design["pw"] = winter

        def smooth(series, alpha):
            return series.ewm(alpha=alpha, adjust=False).mean()

        design["HDD_Tmax22"] = np.maximum(22 - smooth(weather["Tmax"], 0.3), 0).reindex(index)
        design["warm3"] = (weather["Tmax"] > 15).rolling(3, min_periods=1).sum().reindex(index)
        design["HDD_Tmin10"] = np.maximum(10 - smooth(weather["Tmin"], 0.5), 0).reindex(index)
        cold12 = np.maximum(12 - smooth(weather["T"], 0.3), 0).reindex(index)
        design["cold_aut"] = cold12 * autumn
        mean10 = weather["T"].rolling(core.MEMORY_DAYS, min_periods=core.MEMORY_DAYS // 2).mean()
        design["HDD_m10"] = np.maximum(15.5 - mean10, 0).reindex(index)
        design["hol_xmas"], design["hol_easter"] = core._holidays(index)

        # ── candidates ──
        if "onset" in add or "onset_hdd" in add:
            # Days since the autumn's first day with a 5-day mean under 10 °C,
            # as a ramp that fades over ~3 weeks: the weeks heating is being
            # switched on house by house.
            m5 = weather["T"].rolling(5, min_periods=3).mean()
            since = pd.Series(np.nan, index=weather.index)
            for year in sorted(set(weather.index.year)):
                part = m5[(m5.index >= f"{year}-09-01") & (m5.index <= f"{year}-12-31")]
                first = part[part < 10]
                if len(first):
                    start = first.index[0]
                    span = (weather.index >= start) & (weather.index <= f"{year}-12-31")
                    since[span] = (weather.index[span] - start).days
            ramp = np.exp(-since / 21).fillna(0.0).reindex(index)
            if "onset" in add:
                design["onset"] = ramp
            if "onset_hdd" in add:
                design["onset_hdd"] = ramp * degree
        if "drop3" in add:
            design["drop3"] = np.maximum(weather["T"].shift(3) - weather["T"], 0).reindex(index)
        if "frost7" in add:
            design["frost7"] = (weather["Tmin"] < 0).rolling(7, min_periods=1).sum().reindex(index)
        for column in drop:
            design = design.drop(columns=[c for c in design.columns if c == column])
        return design.fillna(0.0)
    return features


# ── the pipeline, as service.train, and its scores ───────────────────────

def score(inputs, features):
    core_features = core.features
    core.features = features
    try:
        frames = service.build_frames(inputs)
        y = frames.y.where(frames.y > 0)
        index, names = frames.index, list(y.columns)
        cleaning = core.clean(frames.y, frames.weather[service.BRANCH_WEATHER])
        usable = ~(cleaning.shape | cleaning.missing)
        norms = {k: core.seasonal_norm_fit(w[~w["forecast"]] if (~w["forecast"]).sum() > 365 else w)
                 for k, w in frames.weather.items()}
        designs = service._designs(frames, index, norms, names)
        first_ok = ~cleaning.excluded
        alone = pd.DataFrame({n: core.fit(designs[n], y[n], first_ok[n], False)[1] for n in names})
        first_trend = service._trends(core.own_deviation(y, alone, usable), usable)
        learn = usable & ~core.off_trend(core.own_deviation(y, alone, usable), first_trend)
        model, base, tried = core.fit_pooled(designs, y * np.exp(-first_trend), learn)
        held_dev = core.own_deviation(y, base, usable)
        held_trends = service._trends(held_dev, usable)
        ok = usable & ~core.off_trend(held_dev, held_trends)
        pred = base * np.exp(held_trends)
    finally:
        core.features = core_features

    modelled = [n for n in names if n in model.own]
    year = [core.quality(y[n], pred[n], ok[n]).get("month_error") for n in modelled]
    year = float(np.nanmean([e for e in year if e is not None]))

    use = (ok & y.notna() & pred.notna())[modelled]
    months = index.to_period("M")
    aut = pd.Series(index.month.isin(AUTUMN), index=index)
    # monthly bias in Oct/Nov
    mb = []
    for n in modelled:
        f = pd.DataFrame({"y": y[n].where(use[n]), "p": pred[n].where(use[n])})
        g = f.groupby(months).agg(["sum", "count"])
        for period, row in g.iterrows():
            if period.month in AUTUMN and row[("y", "count")] >= core.MIN_MONTH_DAYS:
                mb.append(abs(row[("y", "sum")] / row[("p", "sum")] - 1) * 100)
    # 7-day error in Oct/Nov
    wk = []
    for n in modelled:
        yy, pp = y[n].where(use[n]), pred[n].where(use[n])
        r = yy.rolling(7, min_periods=5).sum() / pp.rolling(7, min_periods=5).sum() - 1
        wk.extend((r[aut].dropna().abs() * 100).tolist())
    # branch-wide common error in Oct/Nov
    lr = np.log((y[modelled] / pred[modelled]).where(use))
    common = lr.median(axis=1)[aut & (use.sum(axis=1) >= core.MIN_PEERS)].dropna()
    return {
        "year_month": round(year, 2),
        "aut_month": round(float(np.mean(mb)), 2) if mb else None,
        "aut_week": round(float(np.mean(wk)), 2) if wk else None,
        "aut_common_abs": round(float((np.expm1(common).abs() * 100).mean()), 2),
        "aut_common_bias": round(float(np.expm1(common).mean() * 100), 2),
        "strength": model.strength,
    }


VARIANTS = [("baseline", {})]
for column in ["HDD_w", "HDDw_w", "HDDr_w", "wknd_w", "h4_w", "h8_w", "HDDaut_w", "Twarm_s",
               "wknd_s", "HDD_s", "pw", "HDD_Tmax22", "warm3", "HDD_Tmin10", "cold_aut",
               "HDD_m10", "hol_xmas", "hol_easter"]:
    VARIANTS.append((f"-{column}", {"drop": (column,)}))
for candidate in ["onset", "onset_hdd", "drop3", "frost7"]:
    VARIANTS.append((f"+{candidate}", {"add": (candidate,)}))
VARIANTS += [
    ("+onset+onset_hdd", {"add": ("onset", "onset_hdd")}),
    ("base 14", {"base": 14.0}), ("base 17", {"base": 17.0}),
    ("blend 0.70", {"blend": 0.70}), ("blend 1.00", {"blend": 1.0}),
    ("smooth 0.3/0.5", {"warming": 0.3, "cooling": 0.5}),
    ("smooth 0.5/0.7", {"warming": 0.5, "cooling": 0.7}),
    ("smooth 0.2/0.4", {"warming": 0.2, "cooling": 0.4}),
    ("switch 10", {"switch": 10.0}), ("switch 14", {"switch": 14.0}),
]


def main():
    inputs = asyncio.run(load())
    only = set(sys.argv[1:])
    results = {}
    for name, params in VARIANTS:
        if only and name not in only:
            continue
        started = time.time()
        try:
            results[name] = score(inputs, make_features(**params))
        except Exception as error:  # noqa: BLE001 — keep going, report it
            results[name] = {"error": repr(error)}
        print(json.dumps({"variant": name, **results[name],
                          "seconds": round(time.time() - started)}, ensure_ascii=False), flush=True)


main()
