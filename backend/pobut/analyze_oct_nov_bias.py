"""
Анализ октябрьского и ноябрьского bias в Config K.
Ищем: outliers по ГРС, разбивка по группам, распределение ratio.
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pobut_predictor import PobUtPredictor, CT_PRIV, HEATING_MONTHS, SUMMER_MONTHS, GROUP_MERGE

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
m_ua = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',10:'Жов',11:'Лис',12:'Гру'}

SUBS_FILE = 'data/input/all_pobut_enriched.csv'
KI_HEAT   = 'data/profiles/ki_heating_consumers.csv'

P_HEAT = {1:4.3167, 2:4.3717, 3:2.7025, 4:1.3619, 10:1.1902, 11:2.7532, 12:3.9867}

# ── Load billing ──────────────────────────────────────────────────────────────
print("Loading billing...")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
if 'appliance_group' in ap.columns:
    ap['appliance_group'] = ap['appliance_group'].replace(GROUP_MERGE)

month_cols = sorted(
    [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_ENG],
    key=lambda c: MONTH_ENG[c.split('_')[0]],
)
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

# OP consumers only
op_mask = ap['appliance_group'].str.contains('ОП', na=False)
ap['heated_area'] = pd.to_numeric(ap['heated_area'], errors='coerce')
ap_op = ap[op_mask & ap['grs'].notna() & (ap['heated_area'] > 0)].copy()
print(f"OP consumers: {len(ap_op):,}")

# Long format for heating months
billing_long = []
for c in month_cols:
    m = MONTH_ENG[c.split('_')[0]]
    if m not in HEATING_MONTHS:
        continue
    tmp = ap_op[['account_id','grs','appliance_group','consumer_type','heated_area',c]].copy()
    tmp.columns = ['account_id','grs','appliance_group','consumer_type','heated_area','billing']
    tmp['month'] = m
    billing_long.append(tmp)
billing_long = pd.concat(billing_long, ignore_index=True)
billing_long['billing'] = pd.to_numeric(billing_long['billing'], errors='coerce').fillna(0).clip(lower=0)

# ── Load ki ───────────────────────────────────────────────────────────────────
print("Loading ki_heating...")
ki_h = pd.read_csv(KI_HEAT, usecols=['account_id','grs','appliance_group','consumer_type','k_i_clipped'])
ki_h['account_id'] = pd.to_numeric(ki_h['account_id'], errors='coerce')
ki_map = dict(zip(ki_h['account_id'], ki_h['k_i_clipped']))

# Fallback medians
ki_grp   = ki_h.groupby(['appliance_group','consumer_type'])['k_i_clipped'].median()
ki_global = ki_h['k_i_clipped'].median()

def get_ki_fallback(row):
    return ki_grp.get((row.appliance_group, row.consumer_type), ki_global)

# ── Compute model and ratio ───────────────────────────────────────────────────
billing_long['ki'] = billing_long['account_id'].map(ki_map)
missing = billing_long['ki'].isna()
if missing.any():
    billing_long.loc[missing, 'ki'] = billing_long[missing].apply(get_ki_fallback, axis=1)

billing_long['p_heat'] = billing_long['month'].map(P_HEAT)
billing_long['model']  = billing_long['ki'] * billing_long['heated_area'] * billing_long['p_heat']
billing_long['ratio']  = np.where(
    billing_long['model'] > 0,
    billing_long['billing'] / billing_long['model'],
    np.nan
)

# ── 1. Group × Month: median ratio ────────────────────────────────────────────
print("\n" + "="*80)
print("=== 1. Ratio billing/model по ГРУПАХ та місяцях (медіана) ===")
print("    ratio > 1 → модель недооцінює (біллінг вищий), ratio < 1 → переоцінює")
print("="*80)

bl_pos = billing_long[(billing_long['billing'] > 0) & billing_long['ratio'].notna()]
grp_m  = bl_pos.groupby(['appliance_group','consumer_type','month']).agg(
    med_ratio=('ratio','median'),
    n=('ratio','count'),
).reset_index()

for (grp, ct), sub in grp_m.groupby(['appliance_group','consumer_type']):
    sub_s = sub.set_index('month')['med_ratio']
    n_s   = sub.set_index('month')['n']
    vals  = []
    for mm in [10,11,12,1,2,3,4]:
        if mm in sub_s.index:
            vals.append(f"{sub_s[mm]:>6.3f}")
        else:
            vals.append("   ---")
    n_oct = n_s.get(10, 0)
    print(f"  {grp[:22]:22s} {ct[:6]:6s}  Жов Лис Гру Січ Лют Бер Кві")
    print(f"  {'':22s} {'':6s}  {' '.join(vals)}   (n_oct={n_oct:,})")

# ── 2. October: ratio distribution ───────────────────────────────────────────
print("\n" + "="*80)
print("=== 2. Жовтень: розподіл ratio = billing / model ===")
print("="*80)
oct_d = bl_pos[bl_pos['month'] == 10]
print(f"  n абонентів: {len(oct_d):,}")
for pct in [10, 25, 50, 75, 90, 95, 99]:
    print(f"  P{pct:2d}: {oct_d['ratio'].quantile(pct/100):.3f}")
print(f"  % ratio > 2.0: {(oct_d['ratio'] > 2.0).mean()*100:.1f}%")
print(f"  % ratio > 3.0: {(oct_d['ratio'] > 3.0).mean()*100:.1f}%")
print(f"  Середнє: {oct_d['ratio'].mean():.3f}  Медіана: {oct_d['ratio'].median():.3f}")

# Who are the high-ratio outliers?
top_outliers = oct_d[oct_d['ratio'] > 3.0]
print(f"\n  Outliers ratio>3 ({len(top_outliers):,} абонентів): по групах:")
print(top_outliers.groupby(['appliance_group','consumer_type']).agg(
    n=('ratio','count'), med_ratio=('ratio','median'), med_billing=('billing','median'),
    med_area=('heated_area','median')
).to_string())

# ── 3. GRS-level October bias ─────────────────────────────────────────────────
print("\n" + "="*80)
print("=== 3. ГРС рівень: жовтень (тільки ненульовий біллінг) ===")
print("="*80)

oct_grs = billing_long[billing_long['month'] == 10].groupby('grs').agg(
    billing=('billing','sum'),
    model=('model','sum'),
    n=('account_id','count'),
).reset_index()
oct_grs = oct_grs[oct_grs['billing'] > 0].copy()
oct_grs['bias_pct'] = (oct_grs['billing'] - oct_grs['model']) / oct_grs['billing'] * 100

total_billing = oct_grs['billing'].sum()
total_model   = oct_grs['model'].sum()
print(f"  Всього: billing={total_billing:,.0f}  model={total_model:,.0f}  "
      f"bias={(total_billing-total_model)/total_billing*100:+.1f}%")
print(f"  ГРС count: {len(oct_grs)}")
print(f"  % ГРС bias > +10%: {(oct_grs['bias_pct'] > 10).mean()*100:.0f}%")
print(f"  % ГРС bias < -10%: {(oct_grs['bias_pct'] < -10).mean()*100:.0f}%")
print(f"  Median ГРС bias: {oct_grs['bias_pct'].median():+.1f}%")

# Weighted by billing
oct_grs['w_billing'] = oct_grs['billing'] / total_billing
print(f"\n  Топ-15 ГРС з найбільшим billing (жовтень):")
print(f"  {'ГРС':45s}  {'bias%':>7s}  {'billing':>10s}  {'n':>5s}")
print("  " + "-"*72)
for _, r in oct_grs.nlargest(15, 'billing').iterrows():
    print(f"  {r.grs[:43]:43s}  {r.bias_pct:>+7.1f}%  {r.billing:>10,.0f}  {r.n:>5,}")

print(f"\n  Топ-15 ГРС з найбільшим відхиленням (bias%):")
print(f"  {'ГРС':45s}  {'bias%':>7s}  {'billing':>10s}  {'n':>5s}")
print("  " + "-"*72)
for _, r in oct_grs.reindex(oct_grs['bias_pct'].abs().nlargest(15).index).sort_values('bias_pct', ascending=False).iterrows():
    print(f"  {r.grs[:43]:43s}  {r.bias_pct:>+7.1f}%  {r.billing:>10,.0f}  {r.n:>5,}")

# ── 4. November ───────────────────────────────────────────────────────────────
print("\n" + "="*80)
print("=== 4. Листопад: аналогічний аналіз ===")
print("="*80)

nov_grs = billing_long[billing_long['month'] == 11].groupby('grs').agg(
    billing=('billing','sum'), model=('model','sum'), n=('account_id','count'),
).reset_index()
nov_grs = nov_grs[nov_grs['billing'] > 0].copy()
nov_grs['bias_pct'] = (nov_grs['billing'] - nov_grs['model']) / nov_grs['billing'] * 100

total_b = nov_grs['billing'].sum(); total_m = nov_grs['model'].sum()
print(f"  Всього: billing={total_b:,.0f}  model={total_m:,.0f}  "
      f"bias={(total_b-total_m)/total_b*100:+.1f}%")
print(f"  Median ГРС bias: {nov_grs['bias_pct'].median():+.1f}%")
print(f"  % ГРС bias > +10%: {(nov_grs['bias_pct'] > 10).mean()*100:.0f}%")
print(f"  % ГРС bias < -10%: {(nov_grs['bias_pct'] < -10).mean()*100:.0f}%")

# Distribution of Nov ratio
nov_d = bl_pos[bl_pos['month'] == 11]
print(f"\n  Nov ratio distribution:")
for pct in [10, 25, 50, 75, 90]:
    print(f"    P{pct:2d}: {nov_d['ratio'].quantile(pct/100):.3f}")

# ── 5. Cross-check: modem consumers Oct actual vs P_HEAT ─────────────────────
print("\n" + "="*80)
print("=== 5. Перевірка: модемні ОП — фактичне споживання жовтень vs P_HEAT ===")
print("="*80)
try:
    md = pd.read_csv('data/input/profile_pobut_daily_result.csv', low_memory=False)
    md.columns = [c.lower() for c in md.columns]
    if 'account_id' in md.columns:
        md['account_id'] = pd.to_numeric(md['account_id'], errors='coerce')
    # Find October actual modem consumption for OP consumers
    oct_cols = [c for c in md.columns if 'oct' in c.lower() or '10_2025' in c.lower()]
    print(f"  Oct columns found: {oct_cols}")
    if oct_cols:
        md_op = md[md['appliance_group'].str.contains('ОП', na=False)].copy() if 'appliance_group' in md.columns else md
        md_op['heated_area'] = pd.to_numeric(md_op.get('heated_area', pd.Series(dtype=float)), errors='coerce')
        md_op[oct_cols[0]] = pd.to_numeric(md_op[oct_cols[0]], errors='coerce')
        md_op_clean = md_op[(md_op['heated_area']>0) & (md_op[oct_cols[0]]>0)]
        md_op_clean['ratio_oct'] = md_op_clean[oct_cols[0]] / (md_op_clean['heated_area'] * P_HEAT[10])
        print(f"  Modem OP consumers with Oct data: {len(md_op_clean):,}")
        print(f"  Ratio actual/P_HEAT: median={md_op_clean['ratio_oct'].median():.3f}  mean={md_op_clean['ratio_oct'].mean():.3f}")
        for pct in [25, 50, 75]:
            print(f"    P{pct}: {md_op_clean['ratio_oct'].quantile(pct/100):.3f}")
except Exception as e:
    print(f"  Modem data load error: {e}")

print("\n=== DONE ===")
