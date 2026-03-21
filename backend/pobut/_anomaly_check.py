import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd, numpy as np
from pobut_predictor import GROUP_MERGE, META_COLS, CT_PRIV

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
MONTHS = list(range(1,13))

md = pd.read_csv('data/input/profile_pobut_daily_result.csv', sep=';', low_memory=False)
md.columns = META_COLS + list(md.columns[len(META_COLS):])
md = md[md['gas_off'].isna() & md['alternative'].isna() & md['dacha'].isna()]
modem_ids = set(pd.to_numeric(md.iloc[:,0], errors='coerce').dropna().astype(int))

ap = pd.read_csv('data/input/all_pobut_enriched.csv', low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)
ap['has_op']          = ap['appliance_group'].str.contains('ОП', na=False)

bill_cols = {}
for eng, m in MONTH_ENG.items():
    col = f'{eng}_2025'
    if col in ap.columns:
        ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
        bill_cols[m] = col
mc = [bill_cols[m] for m in MONTHS]
ap['annual'] = ap[mc].sum(axis=1)

nm = ap[~ap['account_id'].isin(modem_ids) & (ap['annual'] > 0)].copy()
print(f"Всього не-модемних з annual>0: {len(nm):,}")

# Метрики
prof_cols = [f'p_{m}' for m in MONTHS]
for m in MONTHS:
    nm[f'p_{m}'] = nm[bill_cols[m]] / nm['annual']

nm['n_nonzero']  = (nm[mc].abs() > 0).sum(axis=1)
nm['n_negative'] = (nm[mc] < 0).sum(axis=1)
nm['max_frac']   = nm[prof_cols].max(axis=1)
nm['min_frac']   = nm[prof_cols].min(axis=1)
nm['winter_pct'] = nm[[f'p_{m}' for m in [1,2,3,4,10,11,12]]].sum(axis=1) * 100
nm['summer_pct'] = nm[[f'p_{m}' for m in [6,7,8]]].sum(axis=1) * 100

print('\n--- n_nonzero ---')
for v in [12,11,10,9,8,7,6,5,4,3,2,1]:
    n = (nm['n_nonzero'] == v).sum()
    bar = '#' * (n // 3000)
    print(f'  {v:>2} mis: {n:>7,}  ({n/len(nm)*100:>5.1f}%)  {bar}')

print('\n--- max_frac ---')
for lo, hi in [(0,.10),(.10,.15),(.15,.20),(.20,.25),(.25,.33),(.33,.50),(.50,1.1)]:
    n = ((nm['max_frac'] >= lo) & (nm['max_frac'] < hi)).sum()
    bar = '#' * (n // 3000)
    print(f'  {lo:.2f}-{hi:.2f}: {n:>7,}  ({n/len(nm)*100:>5.1f}%)  {bar}')

print('\n--- n_negative ---')
for v in range(6):
    n = (nm['n_negative'] == v).sum()
    print(f'  {v} neg months: {n:>7,}  ({n/len(nm)*100:>5.1f}%)')

print('\n--- Combinations ---')
combos = [
    (6,  0.60, False, 'current (analyze_billing_profiles)'),
    (8,  0.50, False, 'annual_decompose (old)'),
    (8,  0.33, True,  'strict: 8+ months, max<33%, no neg'),
    (10, 0.25, True,  'very strict: 10+ months, max<25%, no neg'),
    (12, 0.20, True,  'ideal: all 12, max<20%, no neg'),
]
for min_nz, max_mf, no_neg, label in combos:
    mask = (nm['n_nonzero'] >= min_nz) & (nm['max_frac'] < max_mf)
    if no_neg:
        mask &= (nm['n_negative'] == 0)
    n = mask.sum()
    sub = nm[mask]
    op_n  = sub['has_op'].sum()
    npg_n = (~sub['has_op']).sum()
    print(f'  [{n:>7,} / {n/len(nm)*100:>5.1f}%]  OP={op_n:,} PG={npg_n:,}  <- {label}')

print('\n--- OP-специфика: winter_pct distribution (серед has_op) ---')
op_df = nm[nm['has_op'] & (nm['n_nonzero'] >= 6)]
for lo, hi in [(30,50),(50,60),(60,70),(70,80),(80,90),(90,100)]:
    n = ((op_df['winter_pct'] >= lo) & (op_df['winter_pct'] < hi)).sum()
    print(f'  winter {lo}-{hi}%: {n:>6,}  ({n/len(op_df)*100:>4.1f}%)')
