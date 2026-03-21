"""Аналіз зимового завищення по категоріях."""
import sys, io, calendar, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")
import pandas as pd, numpy as np

GROUP_MERGE    = {'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'}
SKIP_GROUPS    = {'---', '222046', 'ВПГ', 'nan'}
HEATING_MONTHS = {1,2,3,4,10,11,12}
OP_TO_SR       = {'ОП,ПГ': 'ПГ', 'ОП,ПГ,ВПГ': 'ПГ,ВПГ'}
OP_FB          = {'ОП,ПГ': 'ОП,ПГ', 'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ'}
MIN_REF        = 5
MONTH_NAME     = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
                  'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}

# ── Моdemні дані ──────────────────────────────────────────────────────────────
raw = pd.read_csv('../data/profile_pobut_daily_result.csv', sep=';', low_memory=False)
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
date_cols = raw.columns[12:].tolist()
raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
raw['heated_area']     = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['heated_area']     = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['has_OP']          = raw['appliance_group'].str.contains('ОП', na=False)
modem_ids = set(raw['account_id'].dropna().astype(int))

long = raw.melt(id_vars=['account_id','appliance_group','heated_area','has_OP'],
                value_vars=date_cols, var_name='ds', value_name='c')
long['date']     = pd.to_datetime(long['ds'], format='%d.%m.%Y', errors='coerce')
long['c']        = pd.to_numeric(long['c'], errors='coerce')
long = long.dropna(subset=['date','c'])
long['month']    = long['date'].dt.month
long['year']     = long['date'].dt.year
long['days_in_m']= long.apply(lambda r: calendar.monthrange(int(r['year']),int(r['month']))[1], axis=1)

ml = long.groupby(['account_id','appliance_group','has_OP','year','month']).agg(
    cs=('c','sum'), nd=('date','count'), dim=('days_in_m','first')).reset_index()
ml = ml[ml['nd'] >= 20].copy()
ml['cons'] = ml['cs'] / ml['nd'] * ml['dim']

grp_sb  = ml[ml['month'].isin([6,7,8]) & ml['has_OP']].groupby('appliance_group')['cons'].median().to_dict()
grp_med = ml[~ml['has_OP']].groupby('appliance_group')['cons'].median().to_dict()

sr_df = pd.read_csv('data/profiles/pg_seasonal_ratios.csv')
pg_sr = {(r['group'], int(r['month'])): r['seasonal_ratio'] for _, r in sr_df.iterrows()}
def get_sr(grp, m):
    return pg_sr.get((OP_TO_SR.get(grp, 'ПГ'), m), 1.0)

long['pbl']     = long.apply(lambda r: grp_sb.get(r['appliance_group'], 1.0) if r['has_OP']
                              else grp_med.get(r['appliance_group'], 1.0), axis=1)
long['sb_day']  = long.apply(lambda r: r['pbl'] * get_sr(r['appliance_group'], int(r['month'])) / r['days_in_m']
                              if r['has_OP'] else r['pbl'] / r['days_in_m'], axis=1)
long['heat_day']= long['c'] - long['sb_day']

ref_op = long[long['has_OP'] & long['month'].isin(HEATING_MONTHS) & (long['heated_area'] > 0)]
op_m2d = (ref_op.groupby(['appliance_group','date'])
    .apply(lambda g: pd.Series({'op_m2': g['heat_day'].sum()/g['heated_area'].sum(), 'n': len(g)}),
           include_groups=False).reset_index())
op_m2d = op_m2d[op_m2d['n'] >= MIN_REF].copy()
op_m2d['op_m2_pos'] = op_m2d['op_m2'].clip(lower=0)
op_m2d['year']  = op_m2d['date'].dt.year
op_m2d['month'] = op_m2d['date'].dt.month
hms_df = (op_m2d.groupby(['appliance_group','year','month'])['op_m2_pos'].sum()
    .reset_index().rename(columns={'op_m2_pos': 'hms'}))
hms_lkp = hms_df.set_index(['appliance_group','year','month'])['hms'].to_dict()

def get_hms(grp, y, m):
    v = hms_lkp.get((grp, y, m))
    return v if v is not None else hms_lkp.get((OP_FB.get(grp), y, m), np.nan)

# ── Не-модемні споживачі ──────────────────────────────────────────────────────
ap = pd.read_csv('data/input/all_pobut_enriched.csv', low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
non_modem = ap[
    ap['gas_off'].isna() &
    ~ap['appliance_group'].isin(SKIP_GROUPS) &
    ~ap['account_id'].isin(modem_ids)
].copy()
non_modem['has_OP']   = non_modem['appliance_group'].str.contains('ОП', na=False)
non_modem['is_dacha'] = non_modem['dacha'].notna()
non_modem['is_alt']   = non_modem['alternative'].notna()
non_modem['heated_area'] = pd.to_numeric(non_modem['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
non_modem['heated_area'] = non_modem['heated_area'].where(non_modem['heated_area'] > 0, 55.0)
non_modem['pbl'] = non_modem.apply(
    lambda r: grp_sb.get(r['appliance_group'], grp_sb.get('ОП,ПГ', 10.0)) if r['has_OP']
              else grp_med.get(r['appliance_group'], grp_med.get('ПГ', 5.0)), axis=1)

# ── Порівняння предикту з білінгом по категоріях ─────────────────────────────
WINTER_MONTHS = {1: 'jan_2025', 2: 'feb_2025', 12: 'dec_2025'}

records = []
for m, col in WINTER_MONTHS.items():
    y = 2025
    for _, row in non_modem.iterrows():
        grp = row['appliance_group']
        if row['is_dacha']:
            continue  # дача взимку — не рахуємо
        if row['has_OP']:
            hv = get_hms(grp, y, m)
            if np.isnan(hv):
                continue
            pred = max(0.0, hv * row['heated_area']) + row['pbl'] * get_sr(grp, m)
        else:
            pred = row['pbl']  # rate_sum ≈ 1 для зими у не-ОП
        fact = pd.to_numeric(row.get(col, 0), errors='coerce')
        fact = fact if pd.notna(fact) else 0.0
        records.append({
            'appliance_group': grp,
            'is_alt':          row['is_alt'],
            'consumer_type':   row.get('consumer_type', ''),
            'month':           m,
            'pred':            pred,
            'fact':            fact,
        })

df = pd.DataFrame(records)

# ── Таблиця 1: по групах × is_alt ────────────────────────────────────────────
print('=' * 80)
print('ЗИМА (jan/feb/dec 2025) — RT предикт vs білінг по групах')
print('=' * 80)
agg = df.groupby(['appliance_group', 'is_alt']).agg(
    n=('pred','count'),
    pred_sum=('pred','sum'),
    fact_sum=('fact','sum')).reset_index()
agg['pred_avg'] = (agg['pred_sum'] / agg['n'] / 3).round(1)
agg['fact_avg'] = (agg['fact_sum'] / agg['n'] / 3).round(1)
agg['bias%']    = ((agg['pred_sum'] - agg['fact_sum']) / agg['fact_sum'].replace(0, np.nan) * 100).round(1)
agg = agg.sort_values('fact_sum', ascending=False)
print(f"  {'Група':<14} {'Alt':>4}  {'N':>6}  {'pred/міс':>9}  {'fact/міс':>9}  "
      f"{'pred_sum':>12}  {'fact_sum':>12}  {'bias%':>7}")
print('  ' + '-'*85)
for _, r in agg.iterrows():
    alt = 'Y' if r['is_alt'] else 'N'
    print(f"  {r['appliance_group']:<14} {alt:>4}  {int(r['n']):>6}  "
          f"{r['pred_avg']:>9.1f}  {r['fact_avg']:>9.1f}  "
          f"{r['pred_sum']:>12,.0f}  {r['fact_sum']:>12,.0f}  {r['bias%']:>6.1f}%")

# ── Таблиця 2: по consumer_type ───────────────────────────────────────────────
print()
print('=' * 80)
print('ЗИМА — по типу споживача')
print('=' * 80)
agg2 = df.groupby(['appliance_group', 'consumer_type']).agg(
    n=('pred','count'),
    pred_sum=('pred','sum'),
    fact_sum=('fact','sum')).reset_index()
agg2['pred_avg'] = (agg2['pred_sum'] / agg2['n'] / 3).round(1)
agg2['fact_avg'] = (agg2['fact_sum'] / agg2['n'] / 3).round(1)
agg2['bias%']    = ((agg2['pred_sum'] - agg2['fact_sum']) / agg2['fact_sum'].replace(0, np.nan) * 100).round(1)
agg2 = agg2[agg2['fact_sum'] > 10000].sort_values('fact_sum', ascending=False)
print(f"  {'Група':<14} {'Тип споживача':<28}  {'N':>6}  {'pred/міс':>9}  {'fact/міс':>9}  {'bias%':>7}")
print('  ' + '-'*85)
for _, r in agg2.iterrows():
    print(f"  {r['appliance_group']:<14} {str(r['consumer_type']):<28}  {int(r['n']):>6}  "
          f"{r['pred_avg']:>9.1f}  {r['fact_avg']:>9.1f}  {r['bias%']:>6.1f}%")
