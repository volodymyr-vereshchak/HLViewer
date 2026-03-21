import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
BILL_MONTHS = ['jan_2025','feb_2025','mar_2025','apr_2025','may_2025','jun_2025',
               'jul_2025','aug_2025','sep_2025','oct_2025','nov_2025','dec_2025']
MONTH_NUM   = {col: i+1 for i, col in enumerate(BILL_MONTHS)}

# ══════════════════════════════════════════════════════════════════════════════
# 1. СМАРТМЕТЕРНІ ДАНІ — факт + тренування моделей
# ══════════════════════════════════════════════════════════════════════════════
print("Завантаження смартметерних даних...")
raw = pd.read_csv("data/input/profile_pobut_daily_result.csv",
                  sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
date_cols = raw.columns[12:].tolist()
raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()
raw['consumer_type']   = raw['consumer_type'].astype(str).str.strip()
raw['heated_area']     = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['residents']       = pd.to_numeric(raw['residents'], errors='coerce').fillna(2.0)
raw['heated_area']     = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['residents']       = raw['residents'].where(raw['residents'] > 0, 2.0)
raw['has_OP']          = raw['appliance_group'].str.contains('ОП', na=False)
raw['is_mkd']          = (raw['consumer_type'] == 'Багатоквартирний сектор').astype(float)

smart_ids = set(raw['account_id'].dropna().astype(int))

long = raw.melt(id_vars=['account_id','appliance_group','is_mkd','heated_area','residents','has_OP'],
    value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date']        = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date']).copy()
long['year']  = long['date'].dt.year
long['month'] = long['date'].dt.month

monthly_sm = (long.groupby(
    ['account_id','appliance_group','is_mkd','heated_area','residents','has_OP','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count')).reset_index())
monthly_sm = monthly_sm[monthly_sm['n_days'] >= 20].copy()
monthly_sm['days_in_m'] = monthly_sm.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly_sm['consumption'] = monthly_sm['cons_sum'] / monthly_sm['n_days'] * monthly_sm['days_in_m']

zero_share = (monthly_sm.groupby('account_id', group_keys=False)
    .apply(lambda x: pd.Series({'zero_share': (x['cons_sum']<=0).mean()}))['zero_share'])
monthly_sm = monthly_sm.join(zero_share, on='account_id')
monthly_sm = monthly_sm[monthly_sm['zero_share'] < 0.5].copy()
q99 = monthly_sm.groupby(['appliance_group','month'])['consumption'].transform('quantile', 0.99)
monthly_sm = monthly_sm[(monthly_sm['consumption']>0) & (monthly_sm['consumption']<=q99)].copy()
monthly_sm['is_heating'] = monthly_sm['month'].isin(HEATING_MONTHS)

# Унікальні (year, month) — для яких генеруємо прогноз
ALL_PERIODS = sorted(monthly_sm[['year','month']].drop_duplicates().itertuples(index=False, name=None))
print(f"  Смартметерних споживачів: {monthly_sm['account_id'].nunique()}")
print(f"  Периодів: {ALL_PERIODS[0]} .. {ALL_PERIODS[-1]}  ({len(ALL_PERIODS)} міс.)")

# ── Тренуємо на ВСІХ смартметерних даних ──────────────────────────────────
op_groups = monthly_sm[monthly_sm['has_OP']]['appliance_group'].unique()
OP_FB = {'ОП,ПГ':'ОП,ПГ', 'ОП,ПГ,ВПГ':'ОП,ПГ,ВПГ', 'ОП,ВПГ':'ОП,ПГ,ВПГ', 'ОП':'ОП,ПГ'}

train = monthly_sm.copy()

# OLD
op_coefs_old = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train[(train['appliance_group']==grp)&(train['month']==m)&train['is_heating']]
        gd = gd.dropna(subset=['consumption'])
        gd = gd[(gd['heated_area']>0)&(gd['residents']>0)]
        if len(gd) < 10: continue
        lr = LinearRegression(fit_intercept=False)
        lr.fit(gd[['heated_area','residents']].values, gd['consumption'].values)
        op_coefs_old[(grp,m)] = (max(0.0,lr.coef_[0]), max(0.0,lr.coef_[1]))

# Summer baseline
summer_sm = monthly_sm[monthly_sm['month'].isin([6,7,8]) & monthly_sm['has_OP']]
consumer_sb_sm = summer_sm.groupby('account_id')['consumption'].median().to_dict()
group_sb = {}
for grp in op_groups:
    vals = summer_sm[summer_sm['appliance_group']==grp]['consumption']
    if len(vals) >= 5:
        group_sb[grp] = vals.median()

train_w = train[train['is_heating'] & train['has_OP']].copy()
train_w['sb'] = train_w['account_id'].map(consumer_sb_sm).fillna(
    train_w['appliance_group'].map(group_sb)).fillna(0.0)
train_w['op_heat'] = np.maximum(0.0, train_w['consumption'] - train_w['sb'])

# MODEL E
op_m2_profile_e = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_w[(train_w['appliance_group']==grp)&(train_w['month']==m)].dropna(subset=['op_heat'])
        gd = gd[gd['heated_area'] > 0]
        if len(gd) >= 10:
            op_m2_profile_e[(grp,m)] = gd['op_heat'].sum() / gd['heated_area'].sum()

# MODEL G
op_coefs_g = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train[(train['appliance_group']==grp)&(train['month']==m)&train['is_heating']]
        gd = gd.dropna(subset=['consumption'])
        gd = gd[(gd['heated_area']>0)&(gd['residents']>0)]
        if len(gd) < 10: continue
        lr = LinearRegression(fit_intercept=False)
        lr.fit(gd[['heated_area','residents','is_mkd']].values, gd['consumption'].values)
        op_coefs_g[(grp,m)] = (max(0.0,lr.coef_[0]), max(0.0,lr.coef_[1]), lr.coef_[2])

# Профіль не-ОП (group, month) → медіана зі смартметерів
non_op_profile = {}
for grp in monthly_sm['appliance_group'].unique():
    if monthly_sm[monthly_sm['appliance_group']==grp]['has_OP'].any():
        continue
    for m in range(1, 13):
        gd = monthly_sm[(monthly_sm['appliance_group']==grp)&(monthly_sm['month']==m)]
        if len(gd) >= 5:
            non_op_profile[(grp,m)] = gd['consumption'].median()

print(f"  Профілів ОП: OLD={len(op_coefs_old)}, E={len(op_m2_profile_e)}, G={len(op_coefs_g)}")
print(f"  Профілів не-ОП: {len(non_op_profile)}")

# ══════════════════════════════════════════════════════════════════════════════
# 2. ВСІ АБОНЕНТИ — атрибути + білінговий літній baseline
# ══════════════════════════════════════════════════════════════════════════════
print("\nЗавантаження all_pobut_enriched...")
ap = pd.read_csv("data/input/all_pobut_enriched.csv", low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip()
ap['consumer_type']   = ap['consumer_type'].astype(str).str.strip()
ap['heated_area']     = pd.to_numeric(ap['heated_area'], errors='coerce').fillna(55.0)
ap['residents']       = pd.to_numeric(ap['residents'], errors='coerce').fillna(2.0)
ap['heated_area']     = ap['heated_area'].where(ap['heated_area'] > 0, 55.0)
ap['residents']       = ap['residents'].where(ap['residents'] > 0, 2.0)
ap['is_mkd']          = (ap['consumer_type'] == 'Багатоквартирний сектор').astype(float)
ap['has_OP']          = ap['appliance_group'].str.contains('ОП', na=False)
ap['grs']             = ap['grs'].astype(str).str.strip()

# Літній baseline: середнє jun/jul/aug після очистки <= 0 і cap 99%
for col in ['jun_2025','jul_2025','aug_2025']:
    ap[col] = pd.to_numeric(ap[col], errors='coerce')
    ap.loc[ap[col] <= 0, col] = np.nan
    p99 = ap.groupby('appliance_group')[col].transform(lambda x: x.quantile(0.99))
    ap.loc[ap[col] > p99, col] = np.nan

ap['sb_billing'] = ap[['jun_2025','jul_2025','aug_2025']].mean(axis=1)
ap.loc[ap['sb_billing'] < 3.0, 'sb_billing'] = np.nan  # поріг активності

# Позначаємо смартметерних
ap['is_smart'] = ap['account_id'].isin(smart_ids)
print(f"  Всього: {len(ap):,}  |  Смартметерних: {ap['is_smart'].sum():,}  |  Решта: {(~ap['is_smart']).sum():,}")

# ══════════════════════════════════════════════════════════════════════════════
# 3. ГЕНЕРАЦІЯ ПРОГНОЗІВ
# ══════════════════════════════════════════════════════════════════════════════
print("\nГенерація прогнозів...")

# Смартметерний факт: pivot account_id × (year,month) → consumption
sm_fact = monthly_sm.set_index(['account_id','year','month'])['consumption'].to_dict()

# Для не-смартметерних ОП: білінговий або груповий summer baseline
def get_sb(acc_id, grp):
    # особистий зі смартметера
    if acc_id in consumer_sb_sm:
        return consumer_sb_sm[acc_id]
    # білінговий
    row = ap[ap['account_id'] == acc_id]
    if len(row) > 0:
        sb_b = row.iloc[0]['sb_billing']
        if not np.isnan(sb_b):
            return sb_b
    # груповий fallback
    return group_sb.get(grp, group_sb.get(OP_FB.get(grp,'ОП,ПГ'), 10.0))

records = []
non_smart = ap[~ap['is_smart']].copy()
# pre-build sb dict for non-smart OP consumers
sb_dict = {}
for _, row in non_smart[non_smart['has_OP']].iterrows():
    acc = row['account_id']
    grp = row['appliance_group']
    sb_b = row['sb_billing']
    sb_dict[acc] = sb_b if not np.isnan(sb_b) else group_sb.get(grp, group_sb.get(OP_FB.get(grp,'ОП,ПГ'), 10.0))

# --- Смартметерні: беремо факт ---
sm_data = monthly_sm[['account_id','year','month','consumption','appliance_group','has_OP']].copy()
sm_data = sm_data.merge(ap[['account_id','grs']], on='account_id', how='left')
sm_data['grs'] = sm_data['grs'].fillna('Невідомо')
sm_data['source'] = 'факт'
sm_data['pred_old'] = sm_data['consumption']
sm_data['pred_e']   = sm_data['consumption']
sm_data['pred_g']   = sm_data['consumption']
records.append(sm_data[['account_id','grs','year','month','appliance_group','has_OP',
                          'source','pred_old','pred_e','pred_g']])

# --- Не-смартметерні ---
print("  Обчислення для не-смартметерних...")
non_smart_records = []

for _, row in non_smart.iterrows():
    acc  = int(row['account_id']) if not np.isnan(row['account_id']) else -1
    grp  = row['appliance_group']
    area = row['heated_area']
    res  = row['residents']
    mkd  = row['is_mkd']
    fb   = OP_FB.get(grp, 'ОП,ПГ,ВПГ')
    grs  = row['grs']
    is_op = row['has_OP']

    for (y, m) in ALL_PERIODS:
        if is_op:
            if m in HEATING_MONTHS:
                sb = sb_dict.get(acc, group_sb.get(grp, 10.0))

                # OLD
                key = (grp,m) if (grp,m) in op_coefs_old else (fb,m)
                p_old = np.nan
                if key in op_coefs_old:
                    ca, cr = op_coefs_old[key]
                    p_old = max(0.0, ca*area + cr*res)

                # MODEL E
                key_e = (grp,m) if (grp,m) in op_m2_profile_e else (fb,m)
                p_e = np.nan
                if key_e in op_m2_profile_e:
                    p_e = max(0.0, op_m2_profile_e[key_e]*area) + sb

                # MODEL G
                key_g = (grp,m) if (grp,m) in op_coefs_g else (fb,m)
                p_g = np.nan
                if key_g in op_coefs_g:
                    ca, cr, cm = op_coefs_g[key_g]
                    p_g = max(0.0, ca*area + cr*res + cm*mkd)
            else:
                # Літо: sb як прогноз
                sb = sb_dict.get(acc, group_sb.get(grp, 10.0))
                p_old = p_e = p_g = sb

            non_smart_records.append((acc, grs, y, m, grp, True, 'прогноз', p_old, p_e, p_g))
        else:
            # Не-ОП: профіль зі смартметерів
            p = non_op_profile.get((grp,m), non_op_profile.get((fb,m), np.nan))
            non_smart_records.append((acc, grs, y, m, grp, False, 'профіль', p, p, p))

df_ns = pd.DataFrame(non_smart_records,
    columns=['account_id','grs','year','month','appliance_group','has_OP',
             'source','pred_old','pred_e','pred_g'])
records.append(df_ns)
print(f"  Не-смартметерних записів: {len(df_ns):,}")

all_pred = pd.concat(records, ignore_index=True)
print(f"  Всього записів: {len(all_pred):,}")

# ══════════════════════════════════════════════════════════════════════════════
# 4. АГРЕГАЦІЯ ПО ГРС × РІК/МІСЯЦЬ
# ══════════════════════════════════════════════════════════════════════════════
print("\nАгрегація по ГРС...")

def agg(df, by):
    return (df.dropna(subset=['pred_old'])
              .groupby(by)
              .agg(n_consumers=('account_id','nunique'),
                   pred_old=('pred_old','sum'),
                   pred_e=('pred_e','sum'),
                   pred_g=('pred_g','sum'))
              .reset_index())

# По ГРС × рік/місяць
grs_monthly = agg(all_pred, ['grs','year','month'])
grs_monthly = grs_monthly.sort_values(['grs','year','month'])

# По ГРС × сезон
all_pred['season'] = all_pred['month'].apply(lambda m: 'опал.' if m in HEATING_MONTHS else 'літо')
grs_season = agg(all_pred, ['grs','season'])
grs_season = grs_season.sort_values(['grs','season'])

# Загальна по ГРС
grs_total = agg(all_pred, ['grs'])
grs_total = grs_total.sort_values('pred_old', ascending=False)

# Загальна по рік/місяць (всі ГРС)
total_monthly = agg(all_pred, ['year','month'])
total_monthly = total_monthly.sort_values(['year','month'])

# ══════════════════════════════════════════════════════════════════════════════
# 5. ЗБЕРЕЖЕННЯ
# ══════════════════════════════════════════════════════════════════════════════
out = "data/grs_full_predict.xlsx"
with pd.ExcelWriter(out, engine='openpyxl') as writer:
    grs_total.round(0).to_excel(writer, sheet_name='ГРС_всього', index=False)
    grs_season.round(0).to_excel(writer, sheet_name='ГРС_сезон', index=False)
    grs_monthly.round(0).to_excel(writer, sheet_name='ГРС_помісячно', index=False)
    total_monthly.round(0).to_excel(writer, sheet_name='Загалом_по_місяцях', index=False)

print(f"\nЗбережено: {out}")

# ══════════════════════════════════════════════════════════════════════════════
# 6. ВИВІД ПІДСУМКУ
# ══════════════════════════════════════════════════════════════════════════════
print()
print("ЗАГАЛЬНА ПО МІСЯЦЯХ (всі ГРС)")
print(f"{'Місяць':<9} {'OLD':>12}  {'E':>12}  {'G':>12}  {'E vs OLD':>9}  {'G vs OLD':>9}")
print("="*70)
for _, r in total_monthly.iterrows():
    diff_e = (r['pred_e']/r['pred_old']-1)*100 if r['pred_old'] > 0 else 0
    diff_g = (r['pred_g']/r['pred_old']-1)*100 if r['pred_old'] > 0 else 0
    print(f"{int(r['year'])}-{int(r['month']):02d}   "
          f"{r['pred_old']:>12.0f}  {r['pred_e']:>12.0f}  {r['pred_g']:>12.0f}"
          f"  {diff_e:>+8.1f}%  {diff_g:>+8.1f}%")

print("="*70)
tot = total_monthly
print(f"{'РАЗОМ':<9} {tot['pred_old'].sum():>12.0f}  {tot['pred_e'].sum():>12.0f}  {tot['pred_g'].sum():>12.0f}")

print()
print("ПО ГРС — ВСЬОГО")
print(f"{'ГРС':<38} {'OLD':>12}  {'E':>12}  {'G':>12}  {'N':>7}")
print("="*80)
for _, r in grs_total.iterrows():
    print(f"{str(r['grs']):<38} {r['pred_old']:>12.0f}  {r['pred_e']:>12.0f}"
          f"  {r['pred_g']:>12.0f}  {int(r['n_consumers']):>7}")
