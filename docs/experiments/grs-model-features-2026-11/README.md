# GRS consumption model: feature experiment, 19.11.2026

Nothing in the program was changed. Decision: wait for the November 2026 1С
upload and compare the current model and the candidate on it — the first data
neither has seen.

## Why

In autumn 2026 the whole Zaporizhzhia branch ran above the model: the common
part was +30…+56 % on 10–13.10 and +7…+17 % on 11–14.11 (lines less DPD
industry, the 1С/lines ratio is 0.99–1.00, so not a scale effect). The per-GRS
deviation subtracts it, but the absolute figures (expected, forecast) were low.

## How

`feature_experiment.py` repeats `services/grs_model/service.train` exactly —
cleaning, first-pass trends, pooled fit with the strength chosen by held-out
months — with `core.features` swapped for a parameterised copy (checked equal
to the program's to 1e-13). Scores on held-out months, local copy of branch 9
(= Запорізька філія on prod), 1С data 01.2025–31.10.2026:

- `year_month` — mean monthly error over the year (the «Модель» tab figure);
- `aut_month`, `aut_week` — monthly and 7-day error in October–November;
- `aut_common_abs`, `aut_common_bias` — the branch-wide error (daily median
  over GRS) in October–November, absolute and signed.

Autumn here is three months: 10.2025, 11.2025, 10.2026.

Run inside the backend container:

    docker exec -i fastapi_app_v2 python - < feature_experiment.py

## Result

56 variants in three rounds (`results_round*.jsonl`). Best balance:
**HDD base 17 °C (was 15.5), winter/summer switch at 14 °C (was 12), no
`h8_w` kink** — `make_features(base=17.0, switch=14.0, drop=("h8_w",))`.

| Measure | Current | Candidate |
|---|---|---|
| Monthly error, year | 5.72 % | 5.41 % |
| Monthly error, Oct–Nov | 6.61 % | 4.07 % |
| 7-day error, Oct–Nov | 9.45 % | 6.35 % |
| Branch error, Oct–Nov | 13.1 % | 8.7 % |
| Branch bias, Oct–Nov | +8.2 % | +2.9 % |

By season: autumn 6.91 → 5.56, spring 4.66 → 4.30, summer 8.96 → 8.59,
winter 2.65 → 3.14 (slightly worse). By GRS: better at 11 of 13; worse at
Кільце (+0.13) and Новомиколаївка (5.72 → 9.14, a depopulating station whose
error is its trend, not the weather).

Also tried, no real gain: heating-onset features (days since first cold
spell, a 3-day temperature drop, frost days in a week); dropping any single
current feature. Dropping `HDD_w` helps autumn but costs the year (6.46 %).

Caveats: the best of 56 on the same data — part of the gain may be fitting,
autumn especially (3 months); one branch only.

## Next

When the November 2026 1С is loaded locally: score both on November alone
(train on data to 31.10, predict November), then decide. To apply: change the
15.5 base, the 12 °C switch and drop `h8` in `core.features`, bump
`PAYLOAD_VERSION`, retrain.
