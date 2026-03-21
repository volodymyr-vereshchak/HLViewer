"""
analyze_bias.py

Глибокий аналіз систематичних помилок по проблемних ГРС.

Питання:
  1. Які (appliance_group, consumer_type) дають найбільший внесок у bias?
  2. Чому mean(sb_i for GRS) ≠ grp_med_modem?
  3. Чи є outlier-споживачі що "тягнуть" прогноз?
  4. Де саме розрив між billing і prediction?

Рівні аналізу:
  - GRS × group × month → bias%
  - GRS: mean(sb_i) vs grp_med по групах
  - Розподіл sb_i та billing по групах всередині проблемного ГРС
  - Топ-N споживачів з найбільшим |pred - billing| за рік

Виходи:
  data/bias_analysis.xlsx
    - GRS_group_month  (головний)
    - SB_vs_grpmed
    - GRS_group_annual
    - Top_outliers
    - Summer_grp_stats
    - SB_distribution
    - Focus_GRS_detail
"""
import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from pobut_predictor import (
    PobUtPredictor, CT_PRIV,
    HEATING_MONTHS, SUMMER_MONTHS, GROUP_MERGE,
)

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
OUT_EXCEL  = 'data/bias_analysis.xlsx'

MONTH_ENG = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
    'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
MONTH_UA = {
    1: 'Січ', 2: 'Лют', 3: 'Бер', 4: 'Кві',
    5: 'Тра', 6: 'Чер', 7: 'Лип', 8: 'Сер',
    9: 'Вер', 10: 'Жов', 11: 'Лис', 12: 'Гру',
}
FOCUS_GRS = [
    'С-1(Запоріж) ГРС-1,3 кільце',
    'ГРС (2) м.Запоріжжя',
    'С-2 (Вільнянс) ГРС Вільнянськ',
]
TOP_N_OUTLIERS = 500


# ══════════════════════════════════════════════════════════════════════════════
# Subclass: expose internal state
# ══════════════════════════════════════════════════════════════════════════════
class PobUtPredictorDebug(PobUtPredictor):
    def get_grp_sb(self):   return dict(self._grp_sb)
    def get_grp_med(self):  return dict(self._grp_med)


# ══════════════════════════════════════════════════════════════════════════════
# Run Config A for 2025
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("Запуск Config A (2025)...")
p = PobUtPredictorDebug(MODEM_FILE, SUBS_FILE, mode='offline', summer_op_vpg=False)
df_raw = p.predict(years=[2025])

df_pred = df_raw[df_raw['type'] == 'pred'].copy()
grp_sb  = p.get_grp_sb()
grp_med = p.get_grp_med()

print(f"  Non-modem predictions: {len(df_pred):,} rows")
print("\n  grp_sb (modem median summer) per OP group:")
for (grp, ct), v in sorted(grp_sb.items()):
    print(f"    ({grp:12s}, {ct[:20]:20s}): {v:.3f}")
print("\n  grp_med (modem median) per non-OP group:")
for grp, v in sorted(grp_med.items()):
    print(f"    {grp}: {v:.3f}")


# ══════════════════════════════════════════════════════════════════════════════
# Load individual billing (all consumers)
# ══════════════════════════════════════════════════════════════════════════════
print("\nЗавантаження individual billing 2025...")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = (
    ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
)
ap['consumer_type'] = ap['consumer_type'].fillna(CT_PRIV)
ap['summer_baseline'] = pd.to_numeric(ap['summer_baseline'], errors='coerce')
ap['heated_area'] = pd.to_numeric(
    ap['heated_area'].astype(str).str.strip(), errors='coerce'
).fillna(55.0).clip(lower=1.0)
ap['has_OP'] = ap['appliance_group'].str.contains('ОП', na=False)

# Melt billing columns → long format
bill_rows = []
for col_name, m in MONTH_ENG.items():
    col = f'{col_name}_2025'
    if col not in ap.columns:
        continue
    ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
    tmp = ap[['account_id', 'appliance_group', 'consumer_type',
              'grs', 'summer_baseline', 'heated_area', 'has_OP', col]].copy()
    tmp = tmp.rename(columns={col: 'billing'})
    tmp['month'] = m
    tmp['year'] = 2025
    bill_rows.append(tmp)

billing_long = pd.concat(bill_rows, ignore_index=True)
billing_long = billing_long[billing_long['grs'].notna()].copy()

# Exclude modem consumers from billing side
modem_ids = p._modem_ids
billing_long = billing_long[~billing_long['account_id'].isin(modem_ids)].copy()
print(f"  Non-modem billing rows: {len(billing_long):,}")
print(f"  Unique non-modem consumers: {billing_long['account_id'].nunique():,}")


# ══════════════════════════════════════════════════════════════════════════════
# Merge predictions × billing at consumer level
# ══════════════════════════════════════════════════════════════════════════════
print("\nМерж predictions × billing...")
merged = df_pred[['account_id', 'appliance_group', 'year', 'month', 'volume', 'grs']].merge(
    billing_long[['account_id', 'month', 'billing', 'consumer_type',
                  'summer_baseline', 'heated_area', 'has_OP']],
    on=['account_id', 'month'],
    how='left',
)
merged['billing'] = merged['billing'].fillna(0)
merged['error']   = merged['volume'] - merged['billing']
merged['abs_err'] = merged['error'].abs()
merged['bias_pct'] = np.where(
    merged['billing'] > 0,
    merged['error'] / merged['billing'] * 100,
    np.nan,
)
print(f"  Merged rows: {len(merged):,}  with billing>0: {(merged['billing']>0).sum():,}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. GRS × group × month bias
# ══════════════════════════════════════════════════════════════════════════════
print("\n[1] GRS × group × month...")
ggm = (
    merged[merged['billing'] > 0]
    .groupby(['grs', 'appliance_group', 'consumer_type', 'month'])
    .agg(
        n=('account_id', 'nunique'),
        billing_sum=('billing', 'sum'),
        pred_sum=('volume', 'sum'),
    )
    .reset_index()
)
ggm['bias_pct'] = (
    (ggm['pred_sum'] - ggm['billing_sum']) / ggm['billing_sum'] * 100
).round(2)
ggm['month_ua'] = ggm['month'].map(MONTH_UA)


# ══════════════════════════════════════════════════════════════════════════════
# 2. sb_i vs grp_med per GRS × group
# ══════════════════════════════════════════════════════════════════════════════
print("[2] sb_i vs grp_med per GRS × group...")

def get_grp_anchor(grp, ct):
    if 'ОП' in str(grp):
        return grp_sb.get((grp, ct),
               grp_sb.get((grp, CT_PRIV),
               grp_sb.get(('ОП,ПГ', CT_PRIV), np.nan)))
    return grp_med.get(grp, np.nan)

sb_stats = (
    billing_long[billing_long['summer_baseline'] > 0]
    .groupby(['grs', 'appliance_group', 'consumer_type'])
    .agg(
        n_consumers=('account_id', 'nunique'),
        mean_sb =('summer_baseline', 'mean'),
        median_sb=('summer_baseline', 'median'),
        p25_sb  =('summer_baseline', lambda x: x.quantile(0.25)),
        p75_sb  =('summer_baseline', lambda x: x.quantile(0.75)),
        p90_sb  =('summer_baseline', lambda x: x.quantile(0.90)),
    )
    .reset_index()
)
sb_stats['grp_anchor_modem'] = sb_stats.apply(
    lambda r: get_grp_anchor(r['appliance_group'], r['consumer_type']), axis=1
)
sb_stats['ratio_median'] = (sb_stats['median_sb'] / sb_stats['grp_anchor_modem']).round(3)
sb_stats['ratio_mean']   = (sb_stats['mean_sb']   / sb_stats['grp_anchor_modem']).round(3)

# Console: focus GRS
print("\n  ratio = median(billing_sb) / grp_anchor_modem:")
for grs in FOCUS_GRS:
    sub = sb_stats[(sb_stats['grs'] == grs)].sort_values('n_consumers', ascending=False)
    if sub.empty:
        continue
    print(f"\n  {grs}")
    print(sub[['appliance_group', 'consumer_type', 'n_consumers',
               'median_sb', 'grp_anchor_modem', 'ratio_median', 'ratio_mean']
              ].to_string(index=False))


# ══════════════════════════════════════════════════════════════════════════════
# 3. Annual contribution per group
# ══════════════════════════════════════════════════════════════════════════════
print("\n[3] Річний внесок кожної групи у похибку...")
annual_grp = (
    merged[merged['billing'] > 0]
    .groupby(['grs', 'appliance_group', 'consumer_type'])
    .agg(
        n_consumers=('account_id', 'nunique'),
        billing_annual=('billing', 'sum'),
        pred_annual=('volume', 'sum'),
        abs_err_total=('abs_err', 'sum'),
    )
    .reset_index()
)
annual_grp['error_annual'] = annual_grp['pred_annual'] - annual_grp['billing_annual']
annual_grp['bias_pct'] = (
    annual_grp['error_annual'] / annual_grp['billing_annual'] * 100
).round(2)
annual_grp['share_billing%'] = (
    annual_grp['billing_annual'] /
    annual_grp.groupby('grs')['billing_annual'].transform('sum') * 100
).round(1)

for grs in FOCUS_GRS:
    sub = annual_grp[annual_grp['grs'] == grs].sort_values(
        'billing_annual', ascending=False
    )
    if sub.empty:
        continue
    total_err = sub['error_annual'].sum()
    print(f"\n  {grs}  (total annual error: {total_err:+,.0f} м³)")
    print(sub[['appliance_group', 'consumer_type', 'n_consumers',
               'billing_annual', 'pred_annual', 'error_annual',
               'bias_pct', 'share_billing%']].to_string(index=False))


# ══════════════════════════════════════════════════════════════════════════════
# 4. Top outlier consumers (focus GRS)
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n[4] Топ outlier споживачів (фокус-ГРС)...")
annual_consumer = (
    merged[merged['billing'] > 0]
    .groupby(['account_id', 'grs', 'appliance_group', 'consumer_type'])
    .agg(
        billing_annual=('billing', 'sum'),
        pred_annual=('volume', 'sum'),
        n_months=('month', 'count'),
        mean_sb=('summer_baseline', 'first'),
        heated_area=('heated_area', 'first'),
    )
    .reset_index()
)
annual_consumer['error_annual'] = (
    annual_consumer['pred_annual'] - annual_consumer['billing_annual']
)
annual_consumer['abs_error'] = annual_consumer['error_annual'].abs()
annual_consumer['bias_pct'] = (
    annual_consumer['error_annual'] / annual_consumer['billing_annual'] * 100
).round(1)

top_focus = (
    annual_consumer[annual_consumer['grs'].isin(FOCUS_GRS)]
    .sort_values('abs_error', ascending=False)
    .head(20)
)
print(top_focus[[
    'account_id', 'grs', 'appliance_group', 'consumer_type',
    'billing_annual', 'pred_annual', 'error_annual', 'bias_pct',
    'mean_sb', 'heated_area'
]].to_string(index=False))


# ══════════════════════════════════════════════════════════════════════════════
# 5. Summer stats per group (focus GRS)
# ══════════════════════════════════════════════════════════════════════════════
print("\n[5] Літнє споживання (Jun-Aug) — billing vs modem grp_sb...")
consumer_summer = (
    merged[
        merged['grs'].isin(FOCUS_GRS) &
        merged['month'].isin([6, 7, 8]) &
        (merged['billing'] > 0)
    ]
    .groupby(['account_id', 'grs', 'appliance_group', 'consumer_type'])['billing']
    .mean()
    .reset_index()
    .rename(columns={'billing': 'billing_summer_avg'})
)
summer_grp = (
    consumer_summer
    .groupby(['grs', 'appliance_group', 'consumer_type'])
    .agg(
        n=('account_id', 'count'),
        mean_bill_summer =('billing_summer_avg', 'mean'),
        median_bill_summer=('billing_summer_avg', 'median'),
        p90_bill_summer  =('billing_summer_avg', lambda x: x.quantile(0.9)),
    )
    .reset_index()
)
summer_grp['grp_sb_modem'] = summer_grp.apply(
    lambda r: get_grp_anchor(r['appliance_group'], r['consumer_type']), axis=1
)
summer_grp['ratio'] = (summer_grp['median_bill_summer'] / summer_grp['grp_sb_modem']).round(3)

for grs in FOCUS_GRS:
    sub = summer_grp[summer_grp['grs'] == grs].sort_values('n', ascending=False)
    if sub.empty:
        continue
    print(f"\n  {grs}")
    print(sub[['appliance_group', 'consumer_type', 'n',
               'median_bill_summer', 'grp_sb_modem', 'ratio',
               'p90_bill_summer']].to_string(index=False))


# ══════════════════════════════════════════════════════════════════════════════
# 6. SB distribution global
# ══════════════════════════════════════════════════════════════════════════════
sb_global = (
    billing_long[billing_long['summer_baseline'] > 0]
    .groupby(['appliance_group', 'consumer_type'])
    .agg(
        n         =('account_id', 'count'),
        mean      =('summer_baseline', 'mean'),
        median    =('summer_baseline', 'median'),
        p10       =('summer_baseline', lambda x: x.quantile(0.10)),
        p25       =('summer_baseline', lambda x: x.quantile(0.25)),
        p75       =('summer_baseline', lambda x: x.quantile(0.75)),
        p90       =('summer_baseline', lambda x: x.quantile(0.90)),
        p99       =('summer_baseline', lambda x: x.quantile(0.99)),
    )
    .reset_index()
)
sb_global['grp_anchor_modem'] = sb_global.apply(
    lambda r: get_grp_anchor(r['appliance_group'], r['consumer_type']), axis=1
)
sb_global['median/modem'] = (sb_global['median'] / sb_global['grp_anchor_modem']).round(3)
sb_global = sb_global.sort_values('n', ascending=False)

print("\n[6] Global SB distribution vs modem anchor:")
print(sb_global[['appliance_group', 'consumer_type', 'n',
                  'median', 'grp_anchor_modem', 'median/modem']].to_string(index=False))


# ══════════════════════════════════════════════════════════════════════════════
# Excel write
# ══════════════════════════════════════════════════════════════════════════════
print(f"\nЗберігаємо {OUT_EXCEL} ...")
with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as xw:
    ggm.to_excel(xw, sheet_name='GRS_group_month', index=False)
    sb_stats.to_excel(xw, sheet_name='SB_vs_grpmed', index=False)
    annual_grp.sort_values(['grs', 'abs_err_total'],
                           ascending=[True, False]).to_excel(
        xw, sheet_name='GRS_group_annual', index=False)
    annual_consumer.sort_values('abs_error', ascending=False).head(
        TOP_N_OUTLIERS).to_excel(xw, sheet_name='Top_outliers', index=False)
    summer_grp.to_excel(xw, sheet_name='Summer_grp_stats', index=False)
    sb_global.to_excel(xw, sheet_name='SB_distribution', index=False)
    # Focus GRS detail (consumer annual aggregate — avoid >1M rows)
    focus_annual = (
        annual_consumer[annual_consumer['grs'].isin(FOCUS_GRS)]
        .sort_values(['grs', 'appliance_group', 'abs_error'], ascending=[True, True, False])
    )
    focus_annual.to_excel(xw, sheet_name='Focus_GRS_detail', index=False)

    # Focus GRS detail monthly (consumer × month, top outliers only by annual abs_error)
    top_ids = focus_annual.nlargest(5000, 'abs_error')['account_id']
    focus_monthly = (
        merged[
            merged['grs'].isin(FOCUS_GRS) &
            merged['account_id'].isin(top_ids) &
            (merged['billing'] > 0)
        ]
        [['account_id', 'grs', 'appliance_group', 'consumer_type',
          'month', 'billing', 'volume', 'error', 'bias_pct',
          'summer_baseline', 'heated_area']]
        .rename(columns={'volume': 'pred'})
        .sort_values(['grs', 'appliance_group', 'account_id', 'month'])
    )
    focus_monthly.to_excel(xw, sheet_name='Focus_monthly_top5k', index=False)

print(f"[OK] {OUT_EXCEL}")
print("\n=== DONE ===")
