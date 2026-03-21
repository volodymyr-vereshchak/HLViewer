"""
Аналіз bias травень (-16.5%) і вересень (-10.25%).
Шукаємо: яка категорія споживачів, чи ОП ще гріє в травні, чи ПГ профіль неточний.
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pobut_predictor import PobUtPredictor, CT_PRIV, HEATING_MONTHS, SUMMER_MONTHS, GROUP_MERGE

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
m_ua = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',7:'Лип',
        8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}

SUBS_FILE     = 'data/input/all_pobut_enriched.csv'
MODEM_FILE    = 'data/input/profile_pobut_daily_result.csv'
P_NONHEAT_CSV = 'data/profiles/p_modem_nonheat.csv'
KI_HEAT_CSV   = 'data/profiles/ki_heating_consumers.csv'
KI_NONHEAT_CSV= 'data/profiles/ki_nonheat_consumers.csv'

print("Loading billing...")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
ap['heated_area'] = pd.to_numeric(ap['heated_area'], errors='coerce')
if 'appliance_group' in ap.columns:
    ap['appliance_group'] = ap['appliance_group'].replace(GROUP_MERGE)

month_cols = sorted(
    [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_ENG],
    key=lambda c: MONTH_ENG[c.split('_')[0]],
)
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

# Long billing
billing_long = []
for c in month_cols:
    m = MONTH_ENG[c.split('_')[0]]
    dacha_col = 'is_dacha' if 'is_dacha' in ap.columns else 'alternative'
    cols = ['account_id','grs','appliance_group','consumer_type','heated_area', dacha_col, c]
    cols = [x for x in cols if x in ap.columns]
    tmp = ap[ap['grs'].notna()][cols].copy()
    tmp = tmp.rename(columns={dacha_col: 'is_dacha', c: 'billing'})
    if 'is_dacha' not in tmp.columns:
        tmp['is_dacha'] = False
    tmp['month'] = m
    billing_long.append(tmp)
billing_long = pd.concat(billing_long, ignore_index=True)
billing_long['billing'] = pd.to_numeric(billing_long['billing'], errors='coerce').fillna(0).clip(lower=0)
billing_long['is_dacha'] = billing_long['is_dacha'].fillna(0).astype(bool)

# ── 1. Billing breakdown by group for May and September ──────────────────────
print("\n" + "="*70)
print("=== 1. Структура білінгу: травень і вересень по групах ===")
print("="*70)

for mo in [5, 9]:
    sub = billing_long[(billing_long['month'] == mo) & (billing_long['billing'] > 0)]
    total = sub['billing'].sum()
    print(f"\n  {m_ua[mo]} 2025: всього білінг = {total:,.0f} м³")
    grp = sub.groupby(['appliance_group','consumer_type']).agg(
        billing=('billing','sum'),
        n=('account_id','count'),
        med_bill=('billing','median'),
    ).reset_index().sort_values('billing', ascending=False)
    grp['pct'] = grp['billing'] / total * 100
    print(f"  {'Група':25s} {'Сектор':10s}  {'billing М³':>12s}  {'%':>6s}  {'n':>6s}  {'median':>8s}")
    print("  " + "-"*72)
    for _, r in grp.iterrows():
        print(f"  {r.appliance_group[:23]:23s}  {r.consumer_type[:9]:9s}  "
              f"{r.billing:>12,.0f}  {r.pct:>5.1f}%  {r.n:>6,}  {r.med_bill:>8.1f}")

# ── 2. P_nonheat profiles: what do we predict for May/Sep? ───────────────────
print("\n" + "="*70)
print("=== 2. P_nonheat профілі для травня і вересня ===")
print("="*70)
p_nh = pd.read_csv(P_NONHEAT_CSV)
print(p_nh.to_string(index=False))

# ── 3. Modem actual: May/Sep consumption breakdown ───────────────────────────
print("\n" + "="*70)
print("=== 3. Modемні дані: фактичне споживання травень/вересень/червень-серпень ===")
print("="*70)

md = pd.read_csv(MODEM_FILE, sep=';', low_memory=False)
md['heated_area'] = pd.to_numeric(md['heated_area'], errors='coerce')
date_cols = [c for c in md.columns if '.' in c and len(c)==10]
months_map = {}
for col in date_cols:
    d, m_s, y = col.split('.')
    months_map.setdefault((int(y), int(m_s)), []).append(col)

if 'appliance_group' in md.columns:
    md['appliance_group'] = md['appliance_group'].replace(GROUP_MERGE)

FOCUS_MONTHS = [5, 6, 7, 8, 9]
FOCUS_GROUPS = ['ОП,ПГ,ВПГ','ОП,ПГ','ПГ,ВПГ','ПГ']

for grp in FOCUS_GROUPS:
    sub_md = md[(md['appliance_group']==grp) & (md['heated_area']>0)].copy()
    if len(sub_md) < 5:
        continue
    print(f"\n  {grp} (n={len(sub_md)}):")
    print(f"    {'Міс':5s}  {'median спец.':>13s}  {'mean спец.':>10s}  {'% active':>9s}")
    for mo in FOCUS_MONTHS:
        k2025 = (2025, mo)
        if k2025 not in months_map:
            continue
        vals = sub_md[months_map[k2025]].apply(pd.to_numeric, errors='coerce').sum(axis=1)
        spec = vals / sub_md['heated_area']
        active_pct = (vals > 0).mean() * 100
        print(f"    {m_ua[mo]:5s}  {spec[vals>0].median():>13.4f}  "
              f"{spec[vals>0].mean():>10.4f}  {active_pct:>8.1f}%")

# ── 4. ОП consumers in May: do they still have heating billing? ───────────────
print("\n" + "="*70)
print("=== 4. ОП споживачі: розподіл майського білінгу ===")
print("    (підозра: частина ще гріється в травні) ===")
print("="*70)

op_may = billing_long[
    (billing_long['month'] == 5) &
    (billing_long['appliance_group'].str.contains('ОП', na=False)) &
    (billing_long['billing'] > 0) &
    (billing_long['heated_area'] > 0)
].copy()

print(f"  ОП споживачі з біллінгом > 0 в травні: {len(op_may):,}")
# What's the distribution? If still heating: billing ~ area × P_HEAT_apr
P_HEAT_APR = 1.3619
op_may['ratio_vs_heat'] = op_may['billing'] / (op_may['heated_area'] * P_HEAT_APR)
print(f"  Відношення billing / (area × P_HEAT_apr):")
for pct in [10, 25, 50, 75, 90]:
    print(f"    P{pct:2d}: {op_may['ratio_vs_heat'].quantile(pct/100):.3f}")
print(f"  % з ratio > 0.5 (ймовірно ще гріються): {(op_may['ratio_vs_heat']>0.5).mean()*100:.1f}%")
print(f"  % з ratio < 0.1 (мінімальне споживання): {(op_may['ratio_vs_heat']<0.1).mean()*100:.1f}%")

# Monthly profile of OP consumers across year
print(f"\n  Динаміка білінгу ОП по місяцях (медіана на 1 абонента):")
for mo in range(1, 13):
    sub = billing_long[
        (billing_long['month'] == mo) &
        (billing_long['appliance_group'].str.contains('ОП', na=False)) &
        (billing_long['billing'] > 0) &
        (billing_long['heated_area'] > 0)
    ]
    if sub.empty: continue
    med = sub['billing'].median()
    tot = sub['billing'].sum()
    n   = len(sub)
    # specific consumption median
    spec = (sub['billing'] / sub['heated_area']).median()
    print(f"    {m_ua[mo]:5s}  median={med:>7.1f} м³  spec={spec:>6.4f} м³/м²  n={n:,}")

# ── 5. Comparison: model vs billing for non-OP consumers May/Sep ─────────────
print("\n" + "="*70)
print("=== 5. ПГ та ПГ,ВПГ споживачі: model vs billing травень/вересень ===")
print("="*70)

ki_nh = pd.read_csv(KI_NONHEAT_CSV, usecols=['account_id','appliance_group','consumer_type','k_i_clipped'])
ki_nh['account_id'] = pd.to_numeric(ki_nh['account_id'], errors='coerce')
ki_map = dict(zip(ki_nh['account_id'], ki_nh['k_i_clipped']))
ki_grp = ki_nh.groupby(['appliance_group','consumer_type'])['k_i_clipped'].median()

p_nh_dict = {
    (r['appliance_group'], r['consumer_type'], int(r['month'])): float(r['P_modem_median'])
    for _, r in p_nh.iterrows()
}

for mo in [5, 9]:
    print(f"\n  {m_ua[mo]}:")
    sub = billing_long[
        (billing_long['month'] == mo) &
        (~billing_long['appliance_group'].str.contains('ОП', na=False)) &
        (billing_long['billing'] > 0)
    ].copy()
    sub['ki'] = sub['account_id'].map(ki_map)
    sub['ki'] = sub['ki'].fillna(sub.apply(
        lambda r: ki_grp.get((r.appliance_group, r.consumer_type), 1.0), axis=1))
    sub['p'] = sub.apply(
        lambda r: p_nh_dict.get((r.appliance_group, r.consumer_type, mo), 0.0), axis=1)
    sub['model'] = sub['ki'] * sub['p']
    sub = sub[sub['model'] > 0]

    for grp_name, g in sub.groupby(['appliance_group','consumer_type']):
        total_bill  = g['billing'].sum()
        total_model = g['model'].sum()
        bias_pct    = (total_model - total_bill) / total_bill * 100
        med_p = p_nh_dict.get((grp_name[0], grp_name[1], mo), 0)
        print(f"    {grp_name[0]:15s} {grp_name[1][:10]:10s}  "
              f"billing={total_bill:>10,.0f}  model={total_model:>10,.0f}  "
              f"bias={bias_pct:>+7.2f}%  P(m)={med_p:.3f}")

print("\n=== DONE ===")
