import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd, numpy as np
from pobut_predictor import GROUP_MERGE, META_COLS, CT_PRIV

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
MONTHS = list(range(1,13))
UA = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
      7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}

md = pd.read_csv('data/input/profile_pobut_daily_result.csv', sep=';', low_memory=False)
md.columns = META_COLS + list(md.columns[len(META_COLS):])
md = md[md['gas_off'].isna() & md['alternative'].isna() & md['dacha'].isna()]
modem_ids = set(pd.to_numeric(md.iloc[:,0], errors='coerce').dropna().astype(int))

ap = pd.read_csv('data/input/all_pobut_enriched.csv', low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)

bill_cols = {}
for eng, m in MONTH_ENG.items():
    col = f'{eng}_2025'
    if col in ap.columns:
        ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
        bill_cols[m] = col
mc = [bill_cols[m] for m in MONTHS]
ap['annual'] = ap[mc].sum(axis=1)

nm = ap[~ap['account_id'].isin(modem_ids) & (ap['annual'] > 0)].copy()

# Метрики
prof_cols = [f'p_{m}' for m in MONTHS]
for m in MONTHS:
    nm[f'p_{m}'] = nm[bill_cols[m]] / nm['annual']

nm['n_nonzero']  = (nm[mc] > 0).sum(axis=1)
nm['n_negative'] = (nm[mc] < 0).sum(axis=1)
nm['max_frac']   = nm[prof_cols].max(axis=1)
nm['max_month']  = nm[prof_cols].idxmax(axis=1).str.replace('p_','').astype(int).map(UA)

# Причина виключення
def reason(row):
    reasons = []
    if row['n_nonzero'] < 10:
        reasons.append(f'мало_міс({row["n_nonzero"]})')
    if row['max_frac'] >= 0.25:
        reasons.append(f'стрибок({row["max_frac"]*100:.0f}%_у_{row["max_month"]})')
    if row['n_negative'] > 0:
        reasons.append(f'від_ємні({row["n_negative"]}міс)')
    return '+'.join(reasons) if reasons else 'чистий'

clean_mask = (
    (nm['n_nonzero'] >= 10)
    & (nm['max_frac'] < 0.25)
    & (nm['n_negative'] == 0)
    & nm['grs'].notna()
)
nm['is_clean']   = clean_mask
nm['dirty_type'] = 'чистий'
nm.loc[(nm['n_nonzero'] < 10) & ~clean_mask, 'dirty_type'] = 'мало_місяців'
nm.loc[(nm['n_nonzero'] >= 10) & (nm['max_frac'] >= 0.25) & ~clean_mask, 'dirty_type'] = 'стрибок_місяця'
nm.loc[(nm['n_nonzero'] >= 10) & (nm['max_frac'] < 0.25) & (nm['n_negative'] > 0) & ~clean_mask, 'dirty_type'] = 'від_ємні'

dirty = nm[~clean_mask].copy()
clean = nm[clean_mask].copy()

print(f"Всього: {len(nm):,}  |  Чистих: {len(clean):,} ({len(clean)/len(nm)*100:.1f}%)"
      f"  |  Брудних: {len(dirty):,} ({len(dirty)/len(nm)*100:.1f}%)")

# ── 1. Типи бруду ─────────────────────────────────────────────────────────────
print('\n=== 1. Типи аномалій ===')
for t, sub in dirty.groupby('dirty_type'):
    print(f'\n  [{t}]  n={len(sub):,}  ({len(sub)/len(dirty)*100:.1f}% від брудних)')
    # Склад по групах
    for grp, cnt in sub['appliance_group'].value_counts().head(5).items():
        print(f'    {grp:<22} {cnt:>7,}  ({cnt/len(sub)*100:.1f}%)')

# ── 2. "Стрибок місяця" — детальніше ─────────────────────────────────────────
spike = dirty[dirty['dirty_type'] == 'стрибок_місяця'].copy()
print(f'\n=== 2. Стрибок місяця (n={len(spike):,}) ===')
print('  В якому місяці пік:')
for m, cnt in spike['max_month'].value_counts().items():
    bar = '#' * (cnt // 200)
    print(f'    {m}: {cnt:>6,}  ({cnt/len(spike)*100:.1f}%)  {bar}')

print('\n  Розподіл max_frac:')
for lo, hi in [(.25,.33),(.33,.40),(.40,.50),(.50,.67),(.67,1.0)]:
    n = ((spike['max_frac']>=lo)&(spike['max_frac']<hi)).sum()
    label = {(.25,.33):'чверть-рік (квартал)', (.33,.40):'~3 платежі/рік',
             (.40,.50):'~2-3 платежі', (.50,.67):'~2 платежі',
             (.67,1.0):'1 платіж/рік'}.get((lo,hi),'')
    print(f'    {lo:.2f}-{hi:.2f}: {n:>6,}  ({n/len(spike)*100:.1f}%)  <- {label}')

# Середній профіль "стрибок" по підгрупах
print('\n  Середній профіль по max_frac групах (%):')
print(f'  {"Група":20s}  ' + '  '.join(f'{UA[m]:>5}' for m in MONTHS))
for lo, hi in [(.25,.33),(.33,.50),(.50,1.0)]:
    sub = spike[(spike['max_frac']>=lo)&(spike['max_frac']<hi)]
    if len(sub) < 10: continue
    prof = [sub[f'p_{m}'].mean()*100 for m in MONTHS]
    label = f'{lo:.0%}-{hi:.0%} (n={len(sub):,})'
    print(f'  {label:20s}  ' + '  '.join(f'{v:>5.1f}' for v in prof))

# ── 3. "Мало місяців" — детальніше ──────────────────────────────────────────
few = dirty[dirty['dirty_type'] == 'мало_місяців'].copy()
print(f'\n=== 3. Мало місяців (n={len(few):,}) ===')
print('  Розподіл n_nonzero:')
for v in range(1, 10):
    n = (few['n_nonzero'] == v).sum()
    if n == 0: continue
    bar = '#' * (n // 300)
    print(f'    {v} міс: {n:>6,}  ({n/len(few)*100:.1f}%)  {bar}')

print('\n  Які місяці мають ці абоненти (в яких місяцях є платіж):')
for m in MONTHS:
    n = (few[bill_cols[m]] > 0).sum()
    bar = '#' * (n // 200)
    print(f'    {UA[m]}: {n:>6,}  ({n/len(few)*100:.1f}%)  {bar}')

# 1-місячні абоненти
one_month = few[few['n_nonzero'] == 1]
print(f'\n  1-місячні (n={len(one_month):,}) — в якому місяці платять:')
for m in MONTHS:
    n = (one_month[bill_cols[m]] > 0).sum()
    if n > 0:
        print(f'    {UA[m]}: {n:>5,}')

# ── 4. Від'ємні ───────────────────────────────────────────────────────────────
neg = dirty[dirty['dirty_type'] == 'від_ємні'].copy()
print(f'\n=== 4. Перерахунки/від_ємні (n={len(neg):,}) ===')
print('  Кількість від_ємних місяців:')
for v in range(1, 6):
    n = (neg['n_negative'] == v).sum()
    if n: print(f'    {v} від. міс: {n:>5,}')
print('  Середній профіль (%):')
prof = [neg[f'p_{m}'].mean()*100 for m in MONTHS]
print('  ' + '  '.join(f'{UA[m]}:{v:.1f}%' for m, v in zip(MONTHS, prof)))

# ── 5. Порівняння профілів чистих vs брудних ─────────────────────────────────
print('\n=== 5. Порівняння профілів: чисті vs брудні ===')
print(f'  {"":25s}  ' + '  '.join(f'{UA[m]:>5}' for m in MONTHS) + '  Опал%')
for label, sub in [('Чисті (117K)', clean), ('Брудні (всі)', dirty),
                    ('  - мало міс.', few), ('  - стрибок', spike), ('  - від_ємні', neg)]:
    if len(sub) == 0: continue
    prof = [sub[f'p_{m}'].mean()*100 for m in MONTHS]
    heat = sum(sub[f'p_{m}'].mean() for m in [1,2,3,4,10,11,12])*100
    print(f'  {label:25s}  ' + '  '.join(f'{v:>5.1f}' for v in prof) + f'  {heat:.1f}%')
