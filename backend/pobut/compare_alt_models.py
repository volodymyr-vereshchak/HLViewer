"""
Порівняння двох моделей:
  A: op_m2 стратифікований по (grp, consumer_type, is_alt)
  B: op_m2 стратифікований по (grp, consumer_type) — альтернативники = звичайні
Виводить зведену таблицю по ГРС × місяць.
"""
import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")
import numpy as np, pandas as pd

HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
SUMMER_MONTHS  = {5, 6, 7, 8, 9}
SKIP_GROUPS    = {'---', '222046', 'ВПГ', 'nan'}
GROUP_MERGE    = {'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'}
OP_FB          = {'ОП,ПГ': 'ОП,ПГ', 'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ'}
OP_TO_SR       = {'ОП,ПГ': 'ПГ', 'ОП,ПГ,ВПГ': 'ПГ,ВПГ'}
MIN_REF_STRAT  = 3
MIN_REF        = 5
CT_PRIV        = 'Приватний сектор'
MONTH_NAME     = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
                  'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}

# ── 1. Моdemні дані ────────────────────────────────────────────────────────────
print("Завантаження моdemних даних...")
raw = pd.read_csv("../data/profile_pobut_daily_result.csv", sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
date_cols = raw.columns[12:].tolist()
raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
raw['heated_area']     = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['heated_area']     = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['has_OP']          = raw['appliance_group'].str.contains('ОП', na=False)
modem_ids              = set(raw['account_id'].dropna().astype(int))

long = raw.melt(id_vars=['account_id','appliance_group','heated_area','has_OP'],
                value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date']        = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date','consumption']).copy()
long['year']     = long['date'].dt.year
long['month']    = long['date'].dt.month
long['days_in_m']= long.apply(lambda r: calendar.monthrange(int(r['year']),int(r['month']))[1], axis=1)

# Місячні дані
monthly_long = (long.groupby(['account_id','appliance_group','has_OP','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count'),
         days_in_m=('days_in_m','first')).reset_index())
monthly_long = monthly_long[monthly_long['n_days'] >= 20].copy()
monthly_long['consumption'] = monthly_long['cons_sum'] / monthly_long['n_days'] * monthly_long['days_in_m']

# Групові baseline
grp_sb  = monthly_long[monthly_long['month'].isin([6,7,8]) & monthly_long['has_OP']]\
            .groupby('appliance_group')['consumption'].median().to_dict()
grp_med = monthly_long[~monthly_long['has_OP']]\
            .groupby('appliance_group')['consumption'].median().to_dict()

# Seasonal ratios
sr_df = pd.read_csv('data/profiles/pg_seasonal_ratios.csv')
pg_sr = {(r['group'], int(r['month'])): r['seasonal_ratio'] for _, r in sr_df.iterrows()}
def get_sr(grp, m):
    return pg_sr.get((OP_TO_SR.get(grp, 'ПГ'), m), 1.0)

# consumer_type та is_alt для моdemних
_ap = pd.read_csv("data/input/all_pobut_enriched.csv",
                   usecols=['account_id','consumer_type','alternative'], low_memory=False)
_ap['account_id']    = pd.to_numeric(_ap['account_id'], errors='coerce')
_ap['consumer_type'] = _ap['consumer_type'].fillna(CT_PRIV)
_ap['is_alt']        = _ap['alternative'].notna()
ct_map  = _ap.set_index('account_id')['consumer_type'].to_dict()
alt_map = _ap.set_index('account_id')['is_alt'].to_dict()

long['consumer_type'] = long['account_id'].map(ct_map).fillna(CT_PRIV)
long['is_alt']        = long['account_id'].map(alt_map).fillna(False)
long['pbl']           = long.apply(lambda r: grp_sb.get(r['appliance_group'], 1.0) if r['has_OP']
                                    else grp_med.get(r['appliance_group'], 1.0), axis=1)
long['sr']            = long.apply(lambda r: get_sr(r['appliance_group'], int(r['month'])), axis=1)
long['sb_day']        = long.apply(lambda r: r['pbl']*r['sr']/r['days_in_m'] if r['has_OP']
                                    else r['pbl']/r['days_in_m'], axis=1)
long['heat_day']      = long['consumption'] - long['sb_day']

# ── 2. RT профілі ─────────────────────────────────────────────────────────────
print("Обчислення RT профілів...")
ref_op = long[long['has_OP'] & long['month'].isin(HEATING_MONTHS) & (long['heated_area'] > 0)]

def compute_hms(ref, groupby_cols):
    d = (ref.groupby(groupby_cols + ['date'])
         .apply(lambda g: pd.Series({
             'op_m2': g['heat_day'].sum() / g['heated_area'].sum(), 'n': len(g)
         }), include_groups=False).reset_index())
    d = d[d['n'] >= MIN_REF_STRAT].copy()
    d['op_m2_pos'] = d['op_m2'].clip(lower=0)
    d['year']  = d['date'].dt.year
    d['month'] = d['date'].dt.month
    return (d.groupby(groupby_cols + ['year','month'])['op_m2_pos'].sum()
            .reset_index().rename(columns={'op_m2_pos': 'heat_m2_sum'}))

# Спільні страти для обох моделей
hms_strat = compute_hms(ref_op, ['appliance_group','consumer_type','is_alt'])  # модель A
hms_ct    = compute_hms(ref_op, ['appliance_group','consumer_type'])            # обидві
hms_grp   = compute_hms(ref_op, ['appliance_group'])                            # fallback

hms_strat_lkp = hms_strat.set_index(['appliance_group','consumer_type','is_alt','year','month'])['heat_m2_sum'].to_dict()
hms_ct_lkp    = hms_ct.set_index(['appliance_group','consumer_type','year','month'])['heat_m2_sum'].to_dict()
hms_grp_lkp   = hms_grp.set_index(['appliance_group','year','month'])['heat_m2_sum'].to_dict()

def get_hms_A(grp, ct, is_alt, y, m):
    """Модель A: з окремим alt-коефіцієнтом."""
    v = hms_strat_lkp.get((grp, ct, is_alt, y, m))
    if v is not None: return v
    v = hms_ct_lkp.get((grp, ct, y, m))
    if v is not None: return v
    v = hms_ct_lkp.get((grp, CT_PRIV, y, m))
    if v is not None: return v
    v = hms_grp_lkp.get((grp, y, m))
    if v is not None: return v
    fb = OP_FB.get(grp)
    return hms_grp_lkp.get((fb, y, m), np.nan) if fb else np.nan

def get_hms_B(grp, ct, is_alt, y, m):
    """Модель B: альтернативники = звичайні (тільки consumer_type)."""
    v = hms_ct_lkp.get((grp, ct, y, m))
    if v is not None: return v
    v = hms_ct_lkp.get((grp, CT_PRIV, y, m))
    if v is not None: return v
    v = hms_grp_lkp.get((grp, y, m))
    if v is not None: return v
    fb = OP_FB.get(grp)
    return hms_grp_lkp.get((fb, y, m), np.nan) if fb else np.nan

# rate_sum (однакова для обох)
ref_nonheat = long[~(long['has_OP'] & long['month'].isin(HEATING_MONTHS)) & (long['pbl'] > 0)]
rate_daily = (ref_nonheat.groupby(['appliance_group','date'])
    .apply(lambda g: pd.Series({'rate': g['consumption'].sum()/g['pbl'].sum(), 'n': len(g)}),
           include_groups=False).reset_index())
rate_daily = rate_daily[rate_daily['n'] >= MIN_REF].copy()
rate_daily['year']  = rate_daily['date'].dt.year
rate_daily['month'] = rate_daily['date'].dt.month
rate_sum = (rate_daily.groupby(['appliance_group','year','month'])['rate'].sum()
    .reset_index().rename(columns={'rate': 'rate_sum'}))
rs_lkp = rate_sum.set_index(['appliance_group','year','month'])['rate_sum'].to_dict()

def get_rs(grp, y, m):
    v = rs_lkp.get((grp, y, m))
    if v is not None: return v
    fb = OP_FB.get(grp)
    return rs_lkp.get((fb, y, m), np.nan) if fb else np.nan

# ── 3. Не-модемні споживачі ───────────────────────────────────────────────────
print("Завантаження не-моdemних...")
all_pobut = pd.read_csv("data/input/all_pobut_enriched.csv", low_memory=False, encoding='utf-8')
all_pobut['account_id']      = pd.to_numeric(all_pobut['account_id'], errors='coerce')
all_pobut['appliance_group'] = all_pobut['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
grs_map = all_pobut.set_index('account_id')['grs'].to_dict()

non_modem = all_pobut[
    ~all_pobut['account_id'].isin(modem_ids) & all_pobut['gas_off'].isna() &
    ~all_pobut['appliance_group'].isin(SKIP_GROUPS)
].copy()
non_modem['has_OP']        = non_modem['appliance_group'].str.contains('ОП', na=False)
non_modem['is_dacha']      = non_modem['dacha'].notna()
non_modem['is_alt']        = non_modem['alternative'].notna()
non_modem['consumer_type'] = non_modem['consumer_type'].fillna(CT_PRIV)
non_modem['heated_area']   = pd.to_numeric(non_modem['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
non_modem['heated_area']   = non_modem['heated_area'].where(non_modem['heated_area'] > 0, 55.0)
non_modem['pbl'] = non_modem.apply(
    lambda r: grp_sb.get(r['appliance_group'], grp_sb.get('ОП,ПГ', 10.0)) if r['has_OP']
              else grp_med.get(r['appliance_group'], grp_med.get('ПГ', 5.0)), axis=1)

periods = sorted(set(zip(monthly_long['year'], monthly_long['month'])))
print(f"  Не-модемних: {len(non_modem):,}, Період: {periods[0]}→{periods[-1]}")

# ── 4. Предикт обох моделей ───────────────────────────────────────────────────
print("Обчислення предиктів (2 моделі)...")
records_A, records_B = [], []

for (y, m) in periods:
    is_heat = m in HEATING_MONTHS
    for _, row in non_modem.iterrows():
        grp, area, pbl = row['appliance_group'], row['heated_area'], row['pbl']
        grs, ct, ia    = row['grs'], row['consumer_type'], row['is_alt']
        if pd.isna(grs): continue
        if row['is_dacha'] and m not in SUMMER_MONTHS: continue

        base = {'grs': grs, 'appliance_group': grp, 'year': y, 'month': m}

        if row['has_OP'] and is_heat:
            sr = get_sr(grp, m)
            hA = get_hms_A(grp, ct, ia, y, m)
            hB = get_hms_B(grp, ct, ia, y, m)
            if not np.isnan(hA):
                records_A.append({**base, 'pred': max(0.0, hA*area) + pbl*sr})
            if not np.isnan(hB):
                records_B.append({**base, 'pred': max(0.0, hB*area) + pbl*sr})
        else:
            rs = get_rs(grp, y, m)
            if np.isnan(rs): continue
            vol = pbl * rs
            records_A.append({**base, 'pred': vol})
            records_B.append({**base, 'pred': vol})

pred_A = pd.DataFrame(records_A).groupby(['grs','year','month'])['pred'].sum().reset_index().rename(columns={'pred':'pred_A'})
pred_B = pd.DataFrame(records_B).groupby(['grs','year','month'])['pred'].sum().reset_index().rename(columns={'pred':'pred_B'})
print(f"  Предиктів A: {len(records_A):,} / B: {len(records_B):,}")

# ── 5. Факт моdemних ──────────────────────────────────────────────────────────
modem_monthly = monthly_long[['account_id','year','month','consumption']].copy()
modem_monthly['grs'] = modem_monthly['account_id'].map(grs_map)
modem_fact = (modem_monthly[modem_monthly['grs'].notna()]
    .groupby(['grs','year','month'])['consumption'].sum().reset_index()
    .rename(columns={'consumption':'fact_modem'}))

# ── 6. Факт з файлу (2025) ────────────────────────────────────────────────────
month_cols = [c for c in all_pobut.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_NAME]
fact_long = all_pobut[all_pobut['gas_off'].isna()][['account_id','grs'] + month_cols].melt(
    id_vars=['account_id','grs'], var_name='col', value_name='volume')
fact_long['month']  = fact_long['col'].str.split('_').str[0].map(MONTH_NAME)
fact_long['year']   = 2025
fact_long['volume'] = pd.to_numeric(fact_long['volume'], errors='coerce').fillna(0)
fact_billing = (fact_long[fact_long['grs'].notna()]
    .groupby(['grs','year','month'])['volume'].sum().reset_index()
    .rename(columns={'volume':'fact_billing'}))

# ── 7. Зведена таблиця ────────────────────────────────────────────────────────
cmp = (fact_billing
    .merge(modem_fact, on=['grs','year','month'], how='left')
    .merge(pred_A,     on=['grs','year','month'], how='left')
    .merge(pred_B,     on=['grs','year','month'], how='left'))
cmp['fact_modem'] = cmp['fact_modem'].fillna(0)
cmp['pred_A']     = cmp['pred_A'].fillna(0)
cmp['pred_B']     = cmp['pred_B'].fillna(0)
cmp['total_A']    = cmp['fact_modem'] + cmp['pred_A']
cmp['total_B']    = cmp['fact_modem'] + cmp['pred_B']
cmp['bias_A']     = ((cmp['total_A'] - cmp['fact_billing']) / cmp['fact_billing'].replace(0, np.nan) * 100).round(1)
cmp['bias_B']     = ((cmp['total_B'] - cmp['fact_billing']) / cmp['fact_billing'].replace(0, np.nan) * 100).round(1)

# ── 8. Друк: по місяцях ───────────────────────────────────────────────────────
print()
print('=' * 90)
print('ПО МІСЯЦЯХ (всі ГРС)  |  A = з alt-коеф  |  B = alt як звичайні')
print('=' * 90)
by_m = cmp.groupby(['year','month']).agg(
    fact_billing=('fact_billing','sum'), fact_modem=('fact_modem','sum'),
    total_A=('total_A','sum'), total_B=('total_B','sum')).reset_index()
by_m['bias_A'] = ((by_m['total_A']-by_m['fact_billing'])/by_m['fact_billing'].replace(0,np.nan)*100).round(1)
by_m['bias_B'] = ((by_m['total_B']-by_m['fact_billing'])/by_m['fact_billing'].replace(0,np.nan)*100).round(1)
by_m['diff']   = (by_m['bias_A'] - by_m['bias_B']).round(1)
print(f"  {'Місяць':<8} {'Факт(файл)':>13} {'Razom_A':>13} {'biasA':>7} {'Razom_B':>13} {'biasB':>7} {'A-B':>6}")
print('  ' + '-'*75)
for _, r in by_m.iterrows():
    fb = r['fact_billing']
    if fb == 0: continue
    print(f"  {int(r['year'])}-{int(r['month']):02d}   "
          f"  {fb:>12,.0f}  {r['total_A']:>12,.0f}  {r['bias_A']:>+6.1f}%"
          f"  {r['total_B']:>12,.0f}  {r['bias_B']:>+6.1f}%  {r['diff']:>+5.1f}%")

# ── 9. Друк: по ГРС (2025) ────────────────────────────────────────────────────
print()
print('=' * 105)
print('ПО ГРС (річний підсумок 2025)  |  A = з alt-коеф  |  B = alt як звичайні')
print('=' * 105)
by_g = cmp[cmp['year']==2025].groupby('grs').agg(
    fact_billing=('fact_billing','sum'), total_A=('total_A','sum'), total_B=('total_B','sum')).reset_index()
by_g['bias_A'] = ((by_g['total_A']-by_g['fact_billing'])/by_g['fact_billing'].replace(0,np.nan)*100).round(1)
by_g['bias_B'] = ((by_g['total_B']-by_g['fact_billing'])/by_g['fact_billing'].replace(0,np.nan)*100).round(1)
by_g['diff']   = (by_g['bias_A'] - by_g['bias_B']).round(1)
by_g = by_g.sort_values('fact_billing', ascending=False)
print(f"  {'ГРС':<48} {'Факт':>11} {'total_A':>11} {'biasA':>7} {'total_B':>11} {'biasB':>7} {'A-B':>6}")
print('  ' + '-'*105)
for _, r in by_g.iterrows():
    fb = r['fact_billing']
    if fb == 0: continue
    print(f"  {str(r['grs']):<48} {fb:>10,.0f}  {r['total_A']:>10,.0f}  {r['bias_A']:>+6.1f}%"
          f"  {r['total_B']:>10,.0f}  {r['bias_B']:>+6.1f}%  {r['diff']:>+5.1f}%")

# ── 10. Збереження Excel ──────────────────────────────────────────────────────
out = 'data/compare_alt_models.xlsx'
with pd.ExcelWriter(out, engine='openpyxl') as writer:
    by_m.to_excel(writer, sheet_name='По_місяцях', index=False)
    by_g.to_excel(writer, sheet_name='По_ГРС_2025', index=False)
    cmp.sort_values(['grs','year','month']).to_excel(writer, sheet_name='ГРС_місяць_деталь', index=False)
print(f"\nЗбережено: {out}")
