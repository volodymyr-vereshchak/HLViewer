"""
Аналіз кластерів споживання газу по модемних абонентах.

Будує ознакові вектори (summer_pbl, heat_m2, VPG-компонент) для ~2500 модемних
абонентів, знаходить природні кластери через KMeans, обчислює поправочні
коефіцієнти до групових медіан.

Використання:
    cd backend/pobut
    python analyze_consumption_clusters.py

Виходи:
    data/consumption_clusters.xlsx  (6 листів)
    data/cluster_scatter_global.png
    data/cluster_scatter_by_grs.png
"""
import calendar
import re
import warnings
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 200)

# ── Config ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
INPUT_DIR  = SCRIPT_DIR / 'data' / 'input'
OUTPUT_DIR = SCRIPT_DIR / 'data'

MODEM_FILE      = INPUT_DIR / 'profile_pobut_daily_result.csv'
ENRICHED_FILE   = INPUT_DIR / 'all_pobut_enriched.csv'
OUT_EXCEL       = OUTPUT_DIR / 'consumption_clusters.xlsx'
OUT_SCATTER     = OUTPUT_DIR / 'cluster_scatter_global.png'
OUT_SCATTER_GRS = OUTPUT_DIR / 'cluster_scatter_by_grs.png'

META_COLS = [
    'account_id', 'gas_off', 'alternative', 'dacha', 'appliance_group',
    'consumer_type', 'profile_no', 'heated_area', 'total_area',
    'residents', 'residents_all', 'serial_no',
]

GROUP_MERGE    = {'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'}
HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
SUMMER_CORE    = {6, 7, 8}
MIN_DAYS       = 90
MIN_MONTH_DAYS = 20
SPIKE_MULT     = 5
ZERO_THRESHOLD = 0.50
GRS_MIN_OP     = 50     # min OP subscribers per GRS for per-GRS clustering
LOW_N_THRESH   = 30     # flag cluster as low_n if below this

CT_PRIV   = 'Приватний сектор'
_DATE_PAT = re.compile(r'^\d{2}\.\d{2}\.\d{4}$')

# ══════════════════════════════════════════════════════════════════════════════════
# Block 1: Load & clean modem data
# ══════════════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("Block 1: Load & clean modem data")
print("=" * 70)

raw = pd.read_csv(MODEM_FILE, sep=';', low_memory=False, encoding='utf-8')
raw.columns = META_COLS + list(raw.columns[12:])

raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
raw['heated_area']     = pd.to_numeric(
    raw['heated_area'].astype(str).str.strip(), errors='coerce'
)
raw['consumer_type'] = raw['consumer_type'].fillna(CT_PRIV).astype(str).str.strip()
print(f"Raw subscribers: {len(raw):,}")

# Filter: active only (no gas_off, no alternative, no dacha)
active_mask = raw['gas_off'].isna() & raw['alternative'].isna() & raw['dacha'].isna()
raw = raw[active_mask].copy()
print(f"Active (no gas_off/alt/dacha): {len(raw):,}")

# Identify date columns
date_cols = [c for c in raw.columns if _DATE_PAT.match(str(c))]
print(f"Date columns: {len(date_cols)} [{date_cols[0]} .. {date_cols[-1]}]")

# Melt wide -> long
long_df = raw[
    ['account_id', 'appliance_group', 'consumer_type', 'heated_area'] + date_cols
].melt(
    id_vars=['account_id', 'appliance_group', 'consumer_type', 'heated_area'],
    value_vars=date_cols,
    var_name='date_str',
    value_name='consumption',
)
long_df['date']        = pd.to_datetime(long_df['date_str'], format='%d.%m.%Y', errors='coerce')
long_df['consumption'] = pd.to_numeric(long_df['consumption'], errors='coerce')
long_df = long_df.dropna(subset=['date']).copy()
long_df['month'] = long_df['date'].dt.month
long_df['year']  = long_df['date'].dt.year
print(f"Long format: {len(long_df):,} rows")

# Spike removal: >5× 14-day rolling median -> NaN
long_df = long_df.sort_values(['account_id', 'date']).reset_index(drop=True)
long_df['_roll_med'] = long_df.groupby('account_id')['consumption'].transform(
    lambda x: x.rolling(14, min_periods=7, center=True).median()
)
spike_mask = (
    long_df['consumption'].notna()
    & long_df['_roll_med'].notna()
    & (long_df['_roll_med'] > 0.1)
    & (long_df['consumption'] > long_df['_roll_med'] * SPIKE_MULT)
)
long_df.loc[spike_mask, 'consumption'] = np.nan
long_df.drop(columns=['_roll_med'], inplace=True)
print(f"Spikes marked NaN: {spike_mask.sum():,}")

# Drop NaN consumptions after spike removal
long_df = long_df.dropna(subset=['consumption']).copy()

# Filter: MIN_DAYS valid days
days_per = long_df.groupby('account_id')['date'].nunique()
valid_ids = set(days_per[days_per >= MIN_DAYS].index)
long_df = long_df[long_df['account_id'].isin(valid_ids)].copy()
print(f"After MIN_DAYS={MIN_DAYS}: {long_df['account_id'].nunique():,} subscribers")

# Filter: >50% zeros (inactive modem)
zero_stats = (
    long_df.groupby('account_id')['consumption']
    .agg(total='count', zeros=lambda x: (x == 0).sum())
    .reset_index()
)
zero_stats['zero_pct'] = zero_stats['zeros'] / zero_stats['total']
active_ids = set(zero_stats[zero_stats['zero_pct'] <= ZERO_THRESHOLD]['account_id'])
long_df = long_df[long_df['account_id'].isin(active_ids)].copy()
print(f"After >50% zeros filter: {long_df['account_id'].nunique():,} subscribers")

# Monthly aggregation: sum over days read, normalize to full month
long_df['days_in_m'] = long_df.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1
)
monthly_df = (
    long_df
    .groupby(['account_id', 'appliance_group', 'consumer_type', 'heated_area', 'year', 'month'])
    .agg(
        cons_sum  = ('consumption', 'sum'),
        n_days    = ('date', 'count'),
        days_in_m = ('days_in_m', 'first'),
    )
    .reset_index()
)
monthly_df = monthly_df[monthly_df['n_days'] >= MIN_MONTH_DAYS].copy()
monthly_df['cons_norm'] = monthly_df['cons_sum'] / monthly_df['n_days'] * monthly_df['days_in_m']
print(f"Monthly records: {len(monthly_df):,}, subscribers: {monthly_df['account_id'].nunique():,}")

# ══════════════════════════════════════════════════════════════════════════════════
# Block 2: GRS mapping
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Block 2: GRS mapping")
print("=" * 70)

enriched = pd.read_csv(ENRICHED_FILE, low_memory=False, encoding='utf-8')
enriched['account_id'] = pd.to_numeric(enriched['account_id'], errors='coerce')
grs_map = (
    enriched.dropna(subset=['account_id', 'grs'])
    .set_index('account_id')['grs']
    .to_dict()
)

monthly_df['grs'] = monthly_df['account_id'].map(grs_map)
before_n = monthly_df['account_id'].nunique()
monthly_df = monthly_df[monthly_df['grs'].notna()].copy()
after_n = monthly_df['account_id'].nunique()
print(f"GRS mapped: {after_n:,} subscribers ({before_n - after_n} excluded without GRS)")

# ══════════════════════════════════════════════════════════════════════════════════
# Block 3: Feature vectors
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Block 3: Feature vectors")
print("=" * 70)

# Consumer metadata (one row per subscriber — take mode of consumer_type/appliance_group)
meta = (
    monthly_df[['account_id', 'appliance_group', 'consumer_type', 'heated_area', 'grs']]
    .drop_duplicates('account_id')
    .set_index('account_id')
)

# summer_pbl: mean cons_norm in Jun/Jul/Aug
summer_monthly = monthly_df[monthly_df['month'].isin(SUMMER_CORE)]
summer_pbl_s = summer_monthly.groupby('account_id')['cons_norm'].mean().rename('summer_pbl')

# heat_m2: mean per-heating-month of (cons_norm - summer_pbl).clip(0) / heated_area
heat_monthly_df = monthly_df[monthly_df['month'].isin(HEATING_MONTHS)].copy()
heat_monthly_df = heat_monthly_df.join(summer_pbl_s, on='account_id')
heat_monthly_df['heat_net'] = (
    heat_monthly_df['cons_norm'] - heat_monthly_df['summer_pbl']
).clip(lower=0)
heat_monthly_df['heat_m2_monthly'] = np.where(
    heat_monthly_df['heated_area'] > 0,
    heat_monthly_df['heat_net'] / heat_monthly_df['heated_area'],
    np.nan,
)
heat_m2_s = (
    heat_monthly_df[heat_monthly_df['heat_m2_monthly'].notna()]
    .groupby('account_id')['heat_m2_monthly']
    .mean()
    .rename('heat_m2')
)

# Build features DataFrame
features = meta.join(summer_pbl_s).join(heat_m2_s).reset_index()

# VPG component: ОП,ПГ,ВПГ summer_pbl minus median(ОП,ПГ summer_pbl) by consumer_type
op_pg_ct_med = (
    features[features['appliance_group'] == 'ОП,ПГ']
    .groupby('consumer_type')['summer_pbl']
    .median()
)
features['op_pg_ct_med'] = features['consumer_type'].map(op_pg_ct_med)
features['vpg_component'] = np.where(
    features['appliance_group'] == 'ОП,ПГ,ВПГ',
    features['summer_pbl'] - features['op_pg_ct_med'],
    np.nan,
)
features['log_area'] = np.where(
    features['appliance_group'].str.contains('ОП', na=False),
    np.log1p(features['heated_area'].fillna(0)),
    np.nan,
)

print(f"Feature vectors: {len(features):,} consumers")
for grp, cnt in features['appliance_group'].value_counts().items():
    gdf = features[features['appliance_group'] == grp]
    spbl_ok = gdf['summer_pbl'].notna().sum()
    hm2_ok  = gdf['heat_m2'].notna().sum()
    print(f"  {grp:<20}: {cnt:>4} total, {spbl_ok:>4} with summer_pbl, {hm2_ok:>4} with heat_m2")

# ══════════════════════════════════════════════════════════════════════════════════
# Block 4: Distribution analysis (no clustering)
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Block 4: Distribution analysis")
print("=" * 70)


def dist_stats(series, prefix=''):
    s = series.dropna()
    if len(s) == 0:
        return {f'{prefix}count': 0, f'{prefix}median': np.nan, f'{prefix}q25': np.nan,
                f'{prefix}q75': np.nan, f'{prefix}iqr': np.nan, f'{prefix}mean': np.nan,
                f'{prefix}std': np.nan}
    q25, q75 = s.quantile(0.25), s.quantile(0.75)
    return {
        f'{prefix}count':  len(s),
        f'{prefix}mean':   round(float(s.mean()), 3),
        f'{prefix}median': round(float(s.median()), 3),
        f'{prefix}std':    round(float(s.std()), 3),
        f'{prefix}q25':    round(float(q25), 3),
        f'{prefix}q75':    round(float(q75), 3),
        f'{prefix}iqr':    round(float(q75 - q25), 3),
    }


dist_rows = []
for (grp, ct), gdf in features.groupby(['appliance_group', 'consumer_type']):
    row = {'appliance_group': grp, 'consumer_type': ct}
    row.update(dist_stats(gdf['summer_pbl'], 'spbl_'))
    row.update(dist_stats(gdf['heat_m2'],    'hm2_'))
    if grp == 'ОП,ПГ,ВПГ':
        row.update(dist_stats(gdf['vpg_component'], 'vpg_'))
    dist_rows.append(row)

dist_df = pd.DataFrame(dist_rows)

print("Distribution by (appliance_group, consumer_type):")
print(dist_df[['appliance_group', 'consumer_type', 'spbl_count', 'spbl_median',
               'spbl_iqr', 'hm2_count', 'hm2_median', 'hm2_iqr']].to_string(index=False))

# VPG component summary by CT
vpg_comp_df = (
    features[features['appliance_group'] == 'ОП,ПГ,ВПГ']
    .groupby('consumer_type')['vpg_component']
    .agg(['count', 'median', 'mean'])
    .reset_index()
)
if not vpg_comp_df.empty:
    print(f"\nVPG component by consumer_type:\n{vpg_comp_df.to_string(index=False)}")

# Monthly heat_m2 distribution by heating month
monthly_heat_dist_rows = []
for mo in sorted(HEATING_MONTHS):
    mo_df = heat_monthly_df[heat_monthly_df['month'] == mo].copy()
    # heat_monthly_df already carries appliance_group + consumer_type from monthly_df
    for (grp, ct), gdf in mo_df.groupby(['appliance_group', 'consumer_type']):
        if 'ОП' not in grp:
            continue
        vals = gdf['heat_m2_monthly'].dropna()
        if len(vals) == 0:
            continue
        monthly_heat_dist_rows.append({
            'month': mo, 'appliance_group': grp, 'consumer_type': ct,
            'count': len(vals),
            'median': round(float(vals.median()), 4),
            'mean':   round(float(vals.mean()), 4),
            'std':    round(float(vals.std()), 4),
        })
monthly_heat_dist_df = pd.DataFrame(monthly_heat_dist_rows)
if not monthly_heat_dist_df.empty:
    print(f"\nMonthly heat_m2 by heating month (OP groups):")
    print(monthly_heat_dist_df.to_string(index=False))

# Add monthly heat_m2 distribution rows to dist_df as separate rows (for Excel)
# Already captured in monthly_heat_dist_df

# ══════════════════════════════════════════════════════════════════════════════════
# Block 5: KMeans clustering
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Block 5: KMeans clustering")
print("=" * 70)

all_sil_rows = []


def _run_kmeans(X_scaled, n_samples, label):
    """Run KMeans for k=2..5, return (best_k, best_sil, best_labels, sil_rows)."""
    best_k, best_sil, best_labels = 2, -1.0, None
    sil_rows = []
    min_cluster_size = max(3, int(0.05 * n_samples))

    for k in range(2, 6):
        km = KMeans(n_clusters=k, n_init=20, random_state=42)
        labels = km.fit_predict(X_scaled)
        sizes = np.bincount(labels)
        if any(s < min_cluster_size for s in sizes):
            sil = -1.0
        elif n_samples < 10:
            sil = -1.0
        else:
            try:
                sil = float(silhouette_score(X_scaled, labels))
            except Exception:
                sil = -1.0
        sil_rows.append({
            'label':         label,
            'k':             k,
            'silhouette':    round(sil, 4),
            'inertia':       round(float(km.inertia_), 2),
            'cluster_sizes': str(sorted(sizes.tolist(), reverse=True)),
        })
        if sil > best_sil:
            best_sil, best_k, best_labels = sil, k, labels.copy()

    return best_k, best_sil, best_labels, sil_rows


def cluster_group(sub_df, feature_cols, label):
    """
    Cluster sub_df on feature_cols. Returns sub_df with 'cluster_global' column added.
    """
    X_df = sub_df[feature_cols].dropna()
    n = len(X_df)
    print(f"  [{label}] n={n}, features={feature_cols}", end='')

    if n < 10:
        print(" -> too few, skip")
        sub_df = sub_df.copy()
        sub_df['cluster_global'] = pd.array([-1] * len(sub_df), dtype='Int64')
        empty_sil = pd.DataFrame([{
            'label': label, 'k': k, 'silhouette': np.nan,
            'inertia': np.nan, 'cluster_sizes': '[]'
        } for k in range(2, 6)])
        return sub_df, empty_sil, -1.0

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_df.values)

    best_k, best_sil, best_labels, sil_rows = _run_kmeans(X_scaled, n, label)
    print(f" -> best_k={best_k}, sil={best_sil:.3f}")

    sub_df = sub_df.copy()
    sub_df['cluster_global'] = pd.array([-1] * len(sub_df), dtype='Int64')
    # Assign cluster labels to the non-NaN rows (by index)
    sub_df.loc[X_df.index, 'cluster_global'] = pd.array(best_labels, dtype='Int64')

    return sub_df, pd.DataFrame(sil_rows), best_sil


# --- OP groups: cluster on [summer_pbl, heat_m2] ---
op_groups  = ['ОП,ПГ', 'ОП,ПГ,ВПГ']
nop_groups = [g for g in features['appliance_group'].unique() if 'ОП' not in g]

clustered_parts = []

print("\n--- OP group clustering ---")
for grp in op_groups:
    sub = features[features['appliance_group'] == grp].copy()
    if sub.empty:
        continue
    # Separate by consumer_type to detect MKD low_n
    for ct in sub['consumer_type'].unique():
        ct_sub = sub[sub['consumer_type'] == ct]
        if len(ct_sub) < LOW_N_THRESH:
            print(f"  [{grp} | {ct}] low_n={len(ct_sub)} -> flagged, cluster anyway")

    sub_out, sil_df, best_sil = cluster_group(sub, ['summer_pbl', 'heat_m2'], f'{grp}')
    clustered_parts.append(sub_out)
    all_sil_rows.append(sil_df)

print("\n--- Non-OP group clustering (1D: summer_pbl) ---")
for grp in nop_groups:
    sub = features[features['appliance_group'] == grp].copy()
    if sub.empty:
        continue
    sub_out, sil_df, best_sil = cluster_group(sub, ['summer_pbl'], f'{grp}')
    clustered_parts.append(sub_out)
    all_sil_rows.append(sil_df)

features_clustered = pd.concat(clustered_parts, ignore_index=True)
all_sil_df = pd.concat([df for df in all_sil_rows if not df.empty], ignore_index=True)

# ══════════════════════════════════════════════════════════════════════════════════
# Block 6: Correction coefficients
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Block 6: Correction coefficients")
print("=" * 70)

cluster_summary_rows = []

for (grp, ct), grp_ct_df in features_clustered.groupby(['appliance_group', 'consumer_type']):
    is_op = 'ОП' in grp

    # Group+CT medians (over all subscribers) — reference for k_corr
    grp_ct_summer_ref = grp_ct_df['summer_pbl'].mean()
    grp_ct_heat_ref   = grp_ct_df['heat_m2'].mean() if is_op else np.nan

    # Best silhouette for this group
    grp_sil = all_sil_df[all_sil_df['label'] == grp]['silhouette'].max() \
              if not all_sil_df.empty else np.nan

    # Per cluster
    valid_clusters = grp_ct_df[grp_ct_df['cluster_global'].notna()]
    for cid, cdf in valid_clusters.groupby('cluster_global'):
        n = len(cdf)
        summer_vals = cdf['summer_pbl'].dropna()
        heat_vals   = cdf['heat_m2'].dropna()

        summer_mean = float(summer_vals.mean()) if len(summer_vals) else np.nan
        summer_med  = float(summer_vals.median()) if len(summer_vals) else np.nan
        summer_std  = float(summer_vals.std())  if len(summer_vals) > 1 else np.nan
        heat_mean   = float(heat_vals.mean())   if len(heat_vals) and is_op else np.nan
        heat_med    = float(heat_vals.median()) if len(heat_vals) and is_op else np.nan

        k_corr_summer = (summer_mean / grp_ct_summer_ref
                         if grp_ct_summer_ref and not np.isnan(grp_ct_summer_ref) else np.nan)
        k_corr_heat   = (heat_mean / grp_ct_heat_ref
                         if is_op and not np.isnan(grp_ct_heat_ref) and grp_ct_heat_ref
                         else np.nan)

        cluster_summary_rows.append({
            'appliance_group':   grp,
            'consumer_type':     ct,
            'cluster_id':        int(cid),
            'n':                 n,
            'low_n':             n < LOW_N_THRESH,
            'mean_summer_pbl':   round(summer_mean, 2) if not np.isnan(summer_mean) else np.nan,
            'median_summer_pbl': round(summer_med,  2) if not np.isnan(summer_med) else np.nan,
            'std_summer_pbl':    round(summer_std,  2) if summer_std and not np.isnan(summer_std) else np.nan,
            'mean_heat_m2':      round(heat_mean, 4) if not np.isnan(heat_mean) else np.nan,
            'median_heat_m2':    round(heat_med,  4) if not np.isnan(heat_med) else np.nan,
            'k_corr_summer':     round(k_corr_summer, 4) if not np.isnan(k_corr_summer) else np.nan,
            'k_corr_heat':       round(k_corr_heat,   4) if not np.isnan(k_corr_heat)   else np.nan,
            'best_silhouette':   round(float(grp_sil), 4) if not np.isnan(grp_sil) else np.nan,
        })

cluster_summary_df = pd.DataFrame(cluster_summary_rows)
print(cluster_summary_df[
    ['appliance_group', 'consumer_type', 'cluster_id', 'n', 'low_n',
     'mean_summer_pbl', 'k_corr_summer', 'mean_heat_m2', 'k_corr_heat']
].to_string(index=False))

# ══════════════════════════════════════════════════════════════════════════════════
# Block 7: Per-GRS clustering
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Block 7: Per-GRS clustering")
print("=" * 70)

# Check global silhouette for OP groups
op_sil_df = all_sil_df[all_sil_df['label'].str.contains('ОП', na=False)]
global_best_sil = float(op_sil_df['silhouette'].max()) if not op_sil_df.empty else -1.0
print(f"Global OP silhouette (best across k): {global_best_sil:.3f}")
if global_best_sil < 0.3:
    print("  -> Global silhouette < 0.3, per-GRS clustering is recommended")
else:
    print("  -> Global silhouette acceptable, running per-GRS for comparison")

# Count OP modem subscribers per GRS
op_grs_counts = (
    features_clustered[features_clustered['appliance_group'].str.contains('ОП', na=False)]
    .groupby('grs')['account_id'].count()
    .sort_values(ascending=False)
    .rename('n_op')
)
large_grs = op_grs_counts[op_grs_counts >= GRS_MIN_OP].index.tolist()
print(f"\nGRS with >={GRS_MIN_OP} OP subscribers: {len(large_grs)}")
for g in large_grs[:15]:
    print(f"  {g}: {op_grs_counts[g]}")

by_grs_rows       = []
grs_sil_rows      = []

for grs in large_grs:
    gdf = features_clustered[
        (features_clustered['grs'] == grs)
        & features_clustered['appliance_group'].str.contains('ОП', na=False)
    ].copy()

    X_df = gdf[['summer_pbl', 'heat_m2']].dropna()
    n = len(X_df)
    if n < 10:
        continue

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_df.values)
    best_k, best_sil, best_labels, sil_rows_grs = _run_kmeans(X_scaled, n, grs)

    grs_sil_rows.append({
        'grs':         grs,
        'n_op':        int(op_grs_counts[grs]),
        'global_sil':  round(global_best_sil, 4),
        'grs_sil':     round(best_sil, 4),
        'grs_better':  best_sil > global_best_sil,
        'best_k':      best_k,
    })

    for cid in range(best_k):
        cidx = X_df.index[best_labels == cid]
        sub  = gdf.loc[gdf.index.isin(cidx)]
        if len(sub) == 0:
            continue
        by_grs_rows.append({
            'grs':             grs,
            'cluster_id':      int(cid),
            'n':               len(sub),
            'appliance_groups': ', '.join(sub['appliance_group'].value_counts().index[:3]),
            'consumer_types':   ', '.join(sub['consumer_type'].value_counts().index[:2]),
            'mean_summer_pbl': round(float(sub['summer_pbl'].mean()), 2),
            'mean_heat_m2':    round(float(sub['heat_m2'].dropna().mean()), 4)
                               if sub['heat_m2'].notna().any() else np.nan,
            'grs_silhouette':  round(best_sil, 4),
        })

by_grs_df  = pd.DataFrame(by_grs_rows)
grs_sil_df = pd.DataFrame(grs_sil_rows)

if not grs_sil_df.empty:
    better_n = int(grs_sil_df['grs_better'].sum())
    print(f"\nGRS with better silhouette than global: {better_n}/{len(grs_sil_df)}")
    print(grs_sil_df.head(10).to_string(index=False))

# ══════════════════════════════════════════════════════════════════════════════════
# Scatter plots
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Scatter plots")
print("=" * 70)

TAB10 = plt.cm.tab10.colors


def make_scatter_global(feat_df, sil_df, out_path):
    """2-panel scatter: ОП,ПГ (left) and ОП,ПГ,ВПГ (right), colored by cluster."""
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    fig.suptitle('Gas Consumption Clusters: summer baseline vs heating intensity', fontsize=13)

    for ax, grp in zip(axes, ['ОП,ПГ', 'ОП,ПГ,ВПГ']):
        gdf = feat_df[
            (feat_df['appliance_group'] == grp)
            & feat_df['summer_pbl'].notna()
            & feat_df['heat_m2'].notna()
        ].copy()

        if gdf.empty:
            ax.set_title(f'{grp}: no data')
            ax.set_visible(False)
            continue

        # Best k and silhouette for this group
        grp_sil_sub = sil_df[sil_df['label'] == grp] if not sil_df.empty else pd.DataFrame()
        if not grp_sil_sub.empty:
            best_idx = grp_sil_sub['silhouette'].idxmax()
            best_sil_val = float(grp_sil_sub.loc[best_idx, 'silhouette'])
            best_k_val   = int(grp_sil_sub.loc[best_idx, 'k'])
        else:
            best_sil_val, best_k_val = np.nan, 0

        # Clip axes at 99th percentile to avoid outlier distortion
        x99 = float(gdf['summer_pbl'].quantile(0.99))
        y99 = float(gdf['heat_m2'].quantile(0.99))
        plot_df = gdf[
            (gdf['summer_pbl'] <= x99 * 1.2) & (gdf['heat_m2'] <= y99 * 1.2)
        ]

        grp_med_summer = float(gdf['summer_pbl'].median())
        grp_med_heat   = float(gdf['heat_m2'].median())

        clusters = sorted(plot_df['cluster_global'].dropna().unique())
        for cid in clusters:
            cdf   = plot_df[plot_df['cluster_global'] == cid]
            priv  = cdf[cdf['consumer_type'] == CT_PRIV]
            mkd   = cdf[cdf['consumer_type'] != CT_PRIV]
            color = TAB10[int(cid) % len(TAB10)]

            if len(priv):
                ax.scatter(priv['summer_pbl'], priv['heat_m2'],
                           c=[color], marker='o', alpha=0.45, s=25,
                           label=f'C{int(cid)} Приват. (n={len(priv)})',
                           edgecolors='none')
            if len(mkd):
                ax.scatter(mkd['summer_pbl'], mkd['heat_m2'],
                           c=[color], marker='^', alpha=0.70, s=45,
                           label=f'C{int(cid)} МКД (n={len(mkd)})',
                           edgecolors='k', linewidths=0.5)

        # Median lines
        ax.axvline(grp_med_summer, color='gray', linestyle='--', lw=1.0, alpha=0.7,
                   label=f'median_summer={grp_med_summer:.1f}')
        ax.axhline(grp_med_heat,   color='gray', linestyle=':',  lw=1.0, alpha=0.7,
                   label=f'median_heat_m2={grp_med_heat:.3f}')

        sil_str = f'{best_sil_val:.3f}' if not np.isnan(best_sil_val) else 'N/A'
        ax.set_title(f'{grp}   k={best_k_val}, silhouette={sil_str}', fontsize=11)
        ax.set_xlabel('summer_pbl (м³/міс)', fontsize=10)
        ax.set_ylabel('heat_m2 (м³/м²/міс)', fontsize=10)
        ax.set_xlim(left=-0.5, right=x99 * 1.05)
        ax.set_ylim(bottom=-0.01, top=y99 * 1.05)
        ax.legend(fontsize=7, loc='upper right', ncol=1)

    plt.tight_layout()
    plt.savefig(out_path, dpi=130, bbox_inches='tight')
    plt.close()
    print(f"Saved: {out_path}")


def make_scatter_by_grs(feat_df, large_grs_list, out_path, max_grs=6):
    """Per-GRS scatter: summer_pbl vs heat_m2 for top GRS by OP count."""
    op_grs_n = (
        feat_df[feat_df['appliance_group'].str.contains('ОП', na=False)]
        .groupby('grs')['account_id'].count()
        .sort_values(ascending=False)
    )
    top_grs = [g for g in op_grs_n.index if g in large_grs_list][:max_grs]
    if not top_grs:
        print("No GRS with enough OP subscribers for scatter")
        return

    ncols = min(3, len(top_grs))
    nrows = (len(top_grs) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(7 * ncols, 5 * nrows))
    if len(top_grs) == 1:
        axes = [axes]
    else:
        axes = axes.flatten()
    fig.suptitle('Кластери по ГРС (ОП-групи, summer_pbl vs heat_m2)', fontsize=12)

    for ax, grs in zip(axes, top_grs):
        gdf = feat_df[
            (feat_df['grs'] == grs)
            & feat_df['appliance_group'].str.contains('ОП', na=False)
            & feat_df['summer_pbl'].notna()
            & feat_df['heat_m2'].notna()
        ].copy()

        if gdf.empty:
            ax.set_visible(False)
            continue

        x99 = float(gdf['summer_pbl'].quantile(0.99))
        y99 = float(gdf['heat_m2'].quantile(0.99))

        for i, grp in enumerate(sorted(gdf['appliance_group'].unique())):
            sub   = gdf[gdf['appliance_group'] == grp]
            priv  = sub[sub['consumer_type'] == CT_PRIV]
            mkd   = sub[sub['consumer_type'] != CT_PRIV]
            color = TAB10[i % len(TAB10)]
            if len(priv):
                ax.scatter(priv['summer_pbl'], priv['heat_m2'], c=[color],
                           marker='o', alpha=0.55, s=30, label=f'{grp} priv')
            if len(mkd):
                ax.scatter(mkd['summer_pbl'], mkd['heat_m2'], c=[color],
                           marker='^', alpha=0.75, s=45, label=f'{grp} МКД')

        ax.set_xlabel('summer_pbl', fontsize=8)
        ax.set_ylabel('heat_m2', fontsize=8)
        ax.set_title(f'{grs[:35]} (n={len(gdf)})', fontsize=8)
        ax.set_xlim(left=-0.5, right=x99 * 1.05)
        ax.set_ylim(bottom=-0.01, top=y99 * 1.05)
        ax.legend(fontsize=7)

    for ax in axes[len(top_grs):]:
        ax.set_visible(False)

    plt.tight_layout()
    plt.savefig(out_path, dpi=130, bbox_inches='tight')
    plt.close()
    print(f"Saved: {out_path}")


make_scatter_global(features_clustered, all_sil_df, OUT_SCATTER)
make_scatter_by_grs(features_clustered, large_grs, OUT_SCATTER_GRS)

# ══════════════════════════════════════════════════════════════════════════════════
# Output: Excel
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("Output: Excel")
print("=" * 70)

# Scatter_data sheet
scatter_data = features_clustered[[
    'account_id', 'appliance_group', 'consumer_type', 'grs',
    'heated_area', 'summer_pbl', 'heat_m2', 'vpg_component', 'cluster_global',
]].copy()

# Consumer_detail: one row per subscriber, add monthly heat_m2 columns
heat_monthly_wide = heat_monthly_df[['account_id', 'year', 'month', 'heat_m2_monthly']].copy()
heat_monthly_wide = heat_monthly_wide[heat_monthly_wide['month'].isin(HEATING_MONTHS)]
# Average across years for each (account_id, month)
heat_monthly_avg = (
    heat_monthly_wide.groupby(['account_id', 'month'])['heat_m2_monthly']
    .mean()
    .reset_index()
)
heat_monthly_pivot = heat_monthly_avg.pivot_table(
    index='account_id', columns='month', values='heat_m2_monthly'
).reset_index()
heat_monthly_pivot.columns = (
    ['account_id'] + [f'heat_m2_m{int(m)}' for m in heat_monthly_pivot.columns[1:]]
)

consumer_detail = features_clustered[[
    'account_id', 'appliance_group', 'consumer_type', 'grs',
    'heated_area', 'summer_pbl', 'heat_m2', 'vpg_component',
    'log_area', 'cluster_global',
]].merge(heat_monthly_pivot, on='account_id', how='left')

# Silhouette_scores: combine OP + non-OP + GRS comparison
sil_for_excel = all_sil_df.copy()
if not grs_sil_df.empty:
    grs_sil_for_excel = grs_sil_df.rename(columns={'grs_sil': 'silhouette'})
    # Don't concat different schemas; keep separately in Distribution_Summary

with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as writer:
    cluster_summary_df.to_excel(writer, sheet_name='Cluster_Summary', index=False)

    # Distribution_Summary: group stats + monthly heat_m2
    dist_df.to_excel(writer, sheet_name='Distribution_Summary', index=False)
    if not monthly_heat_dist_df.empty:
        monthly_heat_dist_df.to_excel(
            writer, sheet_name='Monthly_heat_m2', index=False
        )

    if not by_grs_df.empty:
        by_grs_df.to_excel(writer, sheet_name='By_GRS', index=False)
    if not grs_sil_df.empty:
        grs_sil_df.to_excel(writer, sheet_name='GRS_Sil_Compare', index=False)

    scatter_data.to_excel(writer, sheet_name='Scatter_data', index=False)
    consumer_detail.to_excel(writer, sheet_name='Consumer_detail', index=False)
    all_sil_df.to_excel(writer, sheet_name='Silhouette_scores', index=False)

print(f"Excel saved: {OUT_EXCEL}")

# ══════════════════════════════════════════════════════════════════════════════════
# Summary printout
# ══════════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Total modem subscribers analyzed: {len(features_clustered):,}")
print(f"Global best OP silhouette: {global_best_sil:.3f}")
print(f"\nCluster Summary:")
print(cluster_summary_df[
    ['appliance_group', 'consumer_type', 'cluster_id', 'n',
     'mean_summer_pbl', 'k_corr_summer', 'mean_heat_m2', 'k_corr_heat',
     'best_silhouette']
].to_string(index=False))
print(f"\nOutputs:")
print(f"  {OUT_EXCEL}")
print(f"  {OUT_SCATTER}")
print(f"  {OUT_SCATTER_GRS}")
print("\n=== DONE ===")
