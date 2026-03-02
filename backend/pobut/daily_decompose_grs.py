"""
daily_decompose_grs.py

Декомпозиція місячного білінгу -> добові об'єми по ГРС.

Логіка для кожного абонента:
  A) Модемний      -> факт по днях (реальні дані)
  B) Чистий        -> місячний білінг(m) * w(cluster,m,d)
  C) Аномальний    -> річний білінг * pattern_share(m) * w(cluster,m,d)

w(cluster, m, d) = медіана питомої частки дня d у місяці m по модемах кластера.
Нормовано: сума w по місяцю = 1.0  -> сума декомпозиції = білінг (нульова різниця).
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pobut_predictor import GROUP_MERGE, CT_PRIV, META_COLS

MODEM_FILE    = "data/input/profile_pobut_daily_result.csv"
SUBS_FILE     = "data/input/all_pobut_enriched.csv"
CLUSTERS_FILE = "data/consumption_clusters.xlsx"
OUT_XLSX      = "data/daily_decompose_grs.xlsx"
SIM_CLEAN     = 0.85

MONTH_ENG = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
             "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}

# ── 1. Модемні добові дані ────────────────────────────────────────────────────
print("=== 1. Модемні добові профілі ===")
md = pd.read_csv(MODEM_FILE, sep=";", low_memory=False)
md.columns = META_COLS + list(md.columns[len(META_COLS):])
md["account_id"] = pd.to_numeric(md["account_id"], errors="coerce")
md = md[md["gas_off"].isna() & md["alternative"].isna() & md["dacha"].isna()].copy()

date_cols = sorted(
    [c for c in md.columns if isinstance(c, str) and c.count(".") == 2
     and len(c) == 10 and c.endswith(".2025")],
    key=lambda c: (int(c[6:]), int(c[3:5]), int(c[:2]))
)
dates_2025 = pd.to_datetime([f"2025-{c[3:5]}-{c[:2]}" for c in date_cols])
n_days = len(date_cols)

for c in date_cols:
    md[c] = pd.to_numeric(md[c], errors="coerce").fillna(0)

md_vals = md[date_cols].values.astype(np.float32)

days_by_month = {m: [i for i, c in enumerate(date_cols) if int(c[3:5]) == m]
                 for m in range(1, 13)}

md_monthly = np.array([md_vals[:, days_by_month[m]].sum(axis=1)
                       for m in range(1, 13)]).T    # (N, 12)
md["annual_modem"] = md_monthly.sum(axis=1)
max_frac = md_monthly.max(axis=1) / np.maximum(md["annual_modem"].values, 1)
active_mask = (md["annual_modem"] > 0) & (max_frac < 0.60)

md_act_df   = md[active_mask].copy().reset_index(drop=True)
md_vals_act = md_vals[active_mask]
modem_ids   = set(md_act_df["account_id"].dropna().astype(int))
print(f"  Активних модемів: {len(md_act_df):,}")

# ── 2. Кластерні дані ─────────────────────────────────────────────────────────
print("\n=== 2. Кластерні дані ===")
subs_cl = pd.read_excel(CLUSTERS_FILE, sheet_name="Subscribers")
subs_cl["account_id"] = pd.to_numeric(subs_cl["account_id"], errors="coerce")
clusters  = sorted(subs_cl["cluster"].dropna().unique().astype(int))
n_clust   = len(clusters)
cl_to_idx = {k: i for i, k in enumerate(clusters)}

patterns_df   = pd.read_excel(CLUSTERS_FILE, sheet_name="Patterns")
# Місячні колонки в Patterns sheet (перші 12 числових після 'pattern')
month_cols = [c for c in patterns_df.columns if c not in ("pattern","n_modems","op_pct","winter_pct","summer_pct")]
month_cols = month_cols[:12]
pattern_share = patterns_df[month_cols].values.astype(np.float64)   # (K, 12)
print(f"  Кластерів: {n_clust},  паттерн-колонки: {month_cols}")

md_act_df = md_act_df.merge(
    subs_cl[["account_id", "cluster"]].rename(columns={"cluster": "cl"}),
    on="account_id", how="left"
)
md_act_df["cl"] = pd.to_numeric(md_act_df["cl"], errors="coerce").fillna(1).astype(int)

# ── 3. Добові ваги паттернів w[ki, d] ────────────────────────────────────────
# Для кожного (кластер, місяць): медіана питомої частки дня по модемах.
# Нормовано: сума по днях місяця = 1.0
print("\n=== 3. Добові ваги паттернів ===")
daily_w = np.zeros((n_clust, n_days))

for ki, k in enumerate(clusters):
    mask_k = (md_act_df["cl"] == k).values
    vals_k = md_vals_act[mask_k]
    print(f"  Паттерн {k}: {mask_k.sum()} модемів")

    for m in range(1, 13):
        idx_m  = days_by_month[m]
        days_m = vals_k[:, idx_m]
        msum   = days_m.sum(axis=1)
        valid  = msum > 0
        if valid.sum() == 0:
            w = np.ones(len(idx_m)) / len(idx_m)
        else:
            shares = days_m[valid] / msum[valid, None]
            w_raw  = np.median(shares, axis=0)
            total  = w_raw.sum()
            w      = w_raw / total if total > 0 else np.ones(len(idx_m)) / len(idx_m)
        daily_w[ki, idx_m] = w

for ki in range(n_clust):
    for m in range(1, 13):
        s = daily_w[ki, days_by_month[m]].sum()
        assert abs(s - 1.0) < 1e-6, f"k={clusters[ki]}, m={m}: sum={s:.10f}"
print("  Нормування: OK (сума по кожному місяцю = 1.0)")

# ── 4. Білінг ─────────────────────────────────────────────────────────────────
print("\n=== 4. Білінг абонентів ===")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap["account_id"]      = pd.to_numeric(ap["account_id"], errors="coerce")
ap["appliance_group"] = ap["appliance_group"].astype(str).str.strip().replace(GROUP_MERGE)
ap["has_OP"]          = ap["appliance_group"].str.contains("\u041e\u041f", na=False)
ap = ap[ap["grs"].notna()].copy()

bcols = {}
for eng, m in MONTH_ENG.items():
    col = f"{eng}_2025"
    if col in ap.columns:
        ap[col] = pd.to_numeric(ap[col], errors="coerce").fillna(0)
        bcols[m] = col

mc = [bcols[m] for m in range(1, 13)]
ap["annual_billing"] = ap[mc].sum(axis=1)
ap = ap[ap["annual_billing"].abs() > 0].copy()

ap = ap.merge(subs_cl[["account_id", "cluster", "similarity"]], on="account_id", how="left")
no_cl = ap["cluster"].isna()
ap.loc[no_cl & ~ap["has_OP"], "cluster"] = 3
ap.loc[no_cl &  ap["has_OP"], "cluster"] = 1
ap["cluster"]    = ap["cluster"].astype(int)
ap["similarity"] = ap["similarity"].fillna(0.0)

ap["stype"] = "anomalous"
ap.loc[ap["account_id"].isin(modem_ids), "stype"] = "modem"
ap.loc[(ap["stype"] == "anomalous") & (ap["similarity"] >= SIM_CLEAN), "stype"] = "clean"

for t in ["modem", "clean", "anomalous"]:
    s = ap[ap["stype"] == t]
    print(f"  {t:10}: {len(s):7,}  vol={s['annual_billing'].sum()/1e6:.2f} млн м3")

# ── 5. Декомпозиція: акумуляція по ГРС ───────────────────────────────────────
print("\n=== 5. Декомпозиція -> добові об'єми по ГРС ===")
grs_list = sorted(ap["grs"].unique())
grs_idx  = {g: i for i, g in enumerate(grs_list)}
n_grs    = len(grs_list)

grs_daily      = np.zeros((n_grs, n_days), dtype=np.float64)
billing_by_grs = np.zeros(n_grs)

# Словник модемних добових масивів
modem_arr = {}
for i, row in md_act_df.iterrows():
    aid = row["account_id"]
    if pd.notna(aid):
        modem_arr[int(aid)] = md_vals_act[i].astype(np.float64)

# Numpy-масиви для швидкого доступу
bill_matrix = ap[[bcols[m] for m in range(1, 13)]].values.astype(np.float64)
annual_vec  = ap["annual_billing"].values.astype(np.float64)
cl_vec      = ap["cluster"].values.astype(int)
stype_vec   = ap["stype"].values
aid_vec     = ap["account_id"].values
grs_vec     = ap["grs"].values

for i in range(len(ap)):
    gi  = grs_idx[grs_vec[i]]
    ki  = cl_to_idx.get(cl_vec[i], 0)
    billing_by_grs[gi] += annual_vec[i]

    aid = int(aid_vec[i]) if pd.notna(aid_vec[i]) else -1

    if stype_vec[i] == "modem" and aid in modem_arr:
        # Факт
        grs_daily[gi] += modem_arr[aid]

    elif stype_vec[i] == "clean":
        # Місячний білінг * добові ваги
        for m in range(1, 13):
            mv = bill_matrix[i, m - 1]
            if mv == 0:
                continue
            idx_m = days_by_month[m]
            grs_daily[gi, idx_m] += mv * daily_w[ki, idx_m]

    else:
        # Аномальний: річний * паттерн_частка(m) * добові ваги
        ann = annual_vec[i]
        for m in range(1, 13):
            mv    = ann * pattern_share[ki, m - 1]
            idx_m = days_by_month[m]
            grs_daily[gi, idx_m] += mv * daily_w[ki, idx_m]

    if i > 0 and i % 50000 == 0:
        print(f"  [{i:>7}/{len(ap)}]")

print(f"  Оброблено {len(ap):,} абонентів")

# ── 6. Валідація ──────────────────────────────────────────────────────────────
print("\n=== 6. Валідація ===")
pred_annual = grs_daily.sum(axis=1)
tot_bill    = billing_by_grs.sum()
tot_pred    = pred_annual.sum()
print(f"  Білінг:       {tot_bill/1e6:.4f} млн м3")
print(f"  Декомпозиція: {tot_pred/1e6:.4f} млн м3")
print(f"  Різниця:      {(tot_pred - tot_bill):.2f} м3  "
      f"({(tot_pred / tot_bill - 1) * 100:.5f}%)")

diffs_pct = np.abs(pred_annual - billing_by_grs) / np.maximum(billing_by_grs, 1) * 100
print(f"\n  Максимальне відхилення по ГРС: {diffs_pct.max():.4f}%")
for i in np.argsort(diffs_pct)[::-1][:5]:
    if billing_by_grs[i] > 0:
        print(f"    {grs_list[i][:40]:40}  diff={diffs_pct[i]:.4f}%")

# ── 7. Збереження ─────────────────────────────────────────────────────────────
print(f"\n=== 7. Збереження {OUT_XLSX} ===")
date_strs = [d.strftime("%Y-%m-%d") for d in dates_2025]

df_daily = pd.DataFrame(grs_daily.round(2), index=grs_list, columns=date_strs)
df_daily.index.name = "grs"

val_df = pd.DataFrame({
    "grs":            grs_list,
    "billing_annual": np.round(billing_by_grs, 1),
    "pred_annual":    np.round(pred_annual, 1),
    "diff_m3":        np.round(pred_annual - billing_by_grs, 2),
    "diff_pct":       np.round((pred_annual / np.maximum(billing_by_grs, 1) - 1) * 100, 5),
})

total_daily = grs_daily.sum(axis=0)
df_total = pd.DataFrame({
    "date":     date_strs,
    "total_m3": np.round(total_daily, 1),
    "month":    [int(c[3:5]) for c in date_cols],
})

with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as w:
    df_daily.to_excel(w, sheet_name="GRS_daily")
    val_df.to_excel(w, sheet_name="Validation", index=False)
    df_total.to_excel(w, sheet_name="Daily_total", index=False)

print(f"  GRS_daily:   {n_grs} ГРС x {n_days} днів")
print(f"  Validation:  {len(val_df)} ГРС")
print(f"  Daily_total: загальний профіль")
print("\n=== Готово ===")
