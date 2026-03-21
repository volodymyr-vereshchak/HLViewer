import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}

# ── ГРС маппінг ───────────────────────────────────────────────────────────────
grs_map = pd.read_excel("data/input/all_pobut.xlsx",
                        usecols=['Особ. рах.', 'ГРС'])
grs_map.columns = ['account_id', 'grs']
grs_map['account_id'] = pd.to_numeric(grs_map['account_id'], errors='coerce')
grs_map = grs_map.dropna(subset=['account_id']).drop_duplicates('account_id')
grs_map['grs'] = grs_map['grs'].astype(str).str.strip()

# ── load & prep ──────────────────────────────────────────────────────────────
raw = pd.read_csv("data/input/profile_pobut_daily_result.csv",
                  sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','profile_no',
        'heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[11:])
date_cols = raw.columns[11:].tolist()
raw['account_id'] = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()
raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['residents']   = pd.to_numeric(raw['residents'], errors='coerce').fillna(2.0)
raw['heated_area'] = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['residents']   = raw['residents'].where(raw['residents'] > 0, 2.0)
raw['has_OP'] = raw['appliance_group'].str.contains('ОП', na=False)

long = raw.melt(id_vars=['account_id','appliance_group','heated_area','residents','has_OP'],
    value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date'] = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date']).copy()
long['year'] = long['date'].dt.year
long['month'] = long['date'].dt.month

monthly = (long.groupby(
    ['account_id','appliance_group','heated_area','residents','has_OP','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count')).reset_index())
monthly = monthly[monthly['n_days'] >= 20].copy()
monthly['days_in_m'] = monthly.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly['consumption'] = monthly['cons_sum'] / monthly['n_days'] * monthly['days_in_m']

zero_share = (monthly.groupby('account_id', group_keys=False)
    .apply(lambda x: pd.Series({'zero_share': (x['cons_sum']<=0).mean()}))['zero_share'])
monthly = monthly.join(zero_share, on='account_id')
monthly = monthly[monthly['zero_share'] < 0.5].copy()
q99 = monthly.groupby(['appliance_group','month'])['consumption'].transform('quantile', 0.99)
monthly = monthly[(monthly['consumption']>0) & (monthly['consumption']<=q99)].copy()
monthly['is_heating'] = monthly['month'].isin(HEATING_MONTHS)

# ── Приєднуємо ГРС ──────────────────────────────────────────────────────────
monthly = monthly.merge(grs_map, on='account_id', how='left')
monthly['grs'] = monthly['grs'].fillna('Невідомо')

print(f"Споживачів: {monthly['account_id'].nunique()}")
print(f"ГРС: {monthly['grs'].nunique()}")
print(f"Записів: {len(monthly)}")

op_groups = monthly[monthly['has_OP']]['appliance_group'].unique()
OP_FB = {'ОП,ПГ':'ОП,ПГ', 'ОП,ПГ,ВПГ':'ОП,ПГ,ВПГ', 'ОП,ВПГ':'ОП,ПГ,ВПГ', 'ОП':'ОП,ПГ'}

# ══ НАВЧАННЯ НА ВСІХ ДАНИХ ══════════════════════════════════════════════════

# ── OLD: coef_area × area + coef_res × residents ─────────────────────────────
op_coefs_old = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = monthly[(monthly['appliance_group']==grp) & (monthly['month']==m) & monthly['is_heating']]
        gd = gd.dropna(subset=['consumption'])
        gd = gd[(gd['heated_area']>0) & (gd['residents']>0)]
        if len(gd) < 10: continue
        lr = LinearRegression(fit_intercept=False)
        lr.fit(gd[['heated_area','residents']].values, gd['consumption'].values)
        op_coefs_old[(grp,m)] = (max(0.0, lr.coef_[0]), max(0.0, lr.coef_[1]))

# ── Літній baseline (NEW / E / F) ─────────────────────────────────────────────
summer_all = monthly[monthly['month'].isin([6,7,8]) & monthly['has_OP']].copy()
consumer_sb = summer_all.groupby('account_id')['consumption'].median().to_dict()
group_sb = {}
for grp in op_groups:
    vals = summer_all[summer_all['appliance_group']==grp]['consumption']
    if len(vals) >= 5:
        group_sb[grp] = vals.median()

# ── NEW: op_m2 (median) ────────────────────────────────────────────────────
train_w = monthly[monthly['is_heating'] & monthly['has_OP']].copy()
train_w['sb'] = train_w.apply(
    lambda r: consumer_sb.get(r['account_id'], group_sb.get(r['appliance_group'], 0.0)), axis=1)
train_w['op_heat'] = np.maximum(0.0, train_w['consumption'] - train_w['sb'])
train_w['op_m2']   = np.where(train_w['heated_area'] > 0,
                               train_w['op_heat'] / train_w['heated_area'], np.nan)

op_m2_profile = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_w[(train_w['appliance_group']==grp) & (train_w['month']==m)].dropna(subset=['op_m2'])
        if len(gd) >= 10:
            op_m2_profile[(grp, m)] = gd['op_m2'].median()

# ── MODEL D: чистий ПГ/ВПГ baseline ──────────────────────────────────────────
pg_monthly_med  = {}
pgvpg_monthly_med = {}
for m in range(1, 13):
    pg_d = monthly[(monthly['appliance_group']=='ПГ') & (monthly['month']==m)]
    if len(pg_d) >= 5:
        pg_monthly_med[m] = pg_d['consumption'].median()
    pgvpg_d = monthly[(monthly['appliance_group']=='ПГ,ВПГ') & (monthly['month']==m)]
    if len(pgvpg_d) >= 5:
        pgvpg_monthly_med[m] = pgvpg_d['consumption'].median()

def get_clean_baseline(grp, m):
    if 'ВПГ' in grp:
        return pgvpg_monthly_med.get(m, pg_monthly_med.get(m, 14.0))
    else:
        return pg_monthly_med.get(m, 8.0)

train_d = monthly[monthly['is_heating'] & monthly['has_OP']].copy()
train_d['sb_clean'] = train_d.apply(
    lambda r: get_clean_baseline(r['appliance_group'], int(r['month'])), axis=1)
train_d['op_heat_d'] = np.maximum(0.0, train_d['consumption'] - train_d['sb_clean'])
train_d['op_m2_d']   = np.where(train_d['heated_area'] > 0,
                                  train_d['op_heat_d'] / train_d['heated_area'], np.nan)

op_m2_profile_d = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_d[(train_d['appliance_group']==grp) & (train_d['month']==m)].dropna(subset=['op_m2_d'])
        if len(gd) >= 10:
            op_m2_profile_d[(grp, m)] = gd['op_m2_d'].median()

# ── MODEL E: sum/sum op_m2 ────────────────────────────────────────────────────
op_m2_profile_e = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_w[(train_w['appliance_group']==grp) & (train_w['month']==m)].dropna(subset=['op_heat'])
        gd = gd[gd['heated_area'] > 0]
        if len(gd) >= 10:
            op_m2_profile_e[(grp, m)] = gd['op_heat'].sum() / gd['heated_area'].sum()

# ── MODEL F: seasonal ratio ───────────────────────────────────────────────────
pg_monthly_all  = monthly[monthly['appliance_group']=='ПГ'].groupby('month')['consumption'].median()
vpg_monthly_all = monthly[monthly['appliance_group']=='ПГ,ВПГ'].groupby('month')['consumption'].median()
pg_ref  = pg_monthly_all.get(7, pg_monthly_all.mean())
vpg_ref = vpg_monthly_all.get(7, vpg_monthly_all.mean())
pg_ratio  = (pg_monthly_all / pg_ref).to_dict()  if pg_ref > 0 else {}
vpg_ratio = (vpg_monthly_all / vpg_ref).to_dict() if vpg_ref > 0 else {}

def get_seasonal_ratio(grp, m):
    if 'ВПГ' in grp:
        return vpg_ratio.get(m, 1.0)
    else:
        return pg_ratio.get(m, 1.0)

# ── Non-ОП: group/month median як прогноз ────────────────────────────────────
non_op_profile = {}
for grp in monthly['appliance_group'].unique():
    if grp in op_groups:
        continue
    for m in range(1, 13):
        gd = monthly[(monthly['appliance_group']==grp) & (monthly['month']==m)]
        if len(gd) >= 5:
            non_op_profile[(grp, m)] = gd['consumption'].median()

# ══ ПЕРЕДБАЧЕННЯ ════════════════════════════════════════════════════════════
print("Обчислення передбачень...")

all_data = monthly.copy()
all_data['pred_old'] = np.nan
all_data['pred_new'] = np.nan
all_data['pred_d']   = np.nan
all_data['pred_e']   = np.nan
all_data['pred_f']   = np.nan

for idx, row in all_data.iterrows():
    grp = row['appliance_group']
    m   = int(row['month'])
    fb  = OP_FB.get(grp, 'ОП,ПГ,ВПГ')

    if row['has_OP']:
        # OLD
        key = (grp,m) if (grp,m) in op_coefs_old else (fb,m)
        if key in op_coefs_old:
            ca, cr = op_coefs_old[key]
            all_data.at[idx, 'pred_old'] = max(0.0, ca*row['heated_area'] + cr*row['residents'])

        # NEW
        key2 = (grp,m) if (grp,m) in op_m2_profile else (fb,m)
        if key2 in op_m2_profile:
            sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
            all_data.at[idx, 'pred_new'] = max(0.0, op_m2_profile[key2] * row['heated_area']) + sb

        # MODEL D
        key3 = (grp,m) if (grp,m) in op_m2_profile_d else (fb,m)
        if key3 in op_m2_profile_d:
            sb_d = get_clean_baseline(grp, m)
            all_data.at[idx, 'pred_d'] = max(0.0, op_m2_profile_d[key3] * row['heated_area']) + sb_d

        # MODEL E
        key4 = (grp,m) if (grp,m) in op_m2_profile_e else (fb,m)
        if key4 in op_m2_profile_e:
            sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
            all_data.at[idx, 'pred_e'] = max(0.0, op_m2_profile_e[key4] * row['heated_area']) + sb

        # MODEL F
        key5 = (grp,m) if (grp,m) in op_m2_profile_e else (fb,m)
        if key5 in op_m2_profile_e:
            sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
            sr = get_seasonal_ratio(grp, m)
            all_data.at[idx, 'pred_f'] = (
                max(0.0, op_m2_profile_e[key5] * row['heated_area'])
                + max(0.0, sb * sr)
            )
    else:
        # Для не-ОП: групова медіана по місяцю (однакова для всіх моделей)
        p = non_op_profile.get((grp, m), np.nan)
        all_data.at[idx, 'pred_old'] = p
        all_data.at[idx, 'pred_new'] = p
        all_data.at[idx, 'pred_d']   = p
        all_data.at[idx, 'pred_e']   = p
        all_data.at[idx, 'pred_f']   = p

print("Передбачення готові.")

# ══ АГРЕГАЦІЯ ПО ГРС ═════════════════════════════════════════════════════════
def make_agg(df, groupby_cols):
    return (df.dropna(subset=['consumption','pred_old'])
              .groupby(groupby_cols)
              .agg(
                  n_consumers=('account_id', 'nunique'),
                  actual=('consumption', 'sum'),
                  pred_old=('pred_old', 'sum'),
                  pred_new=('pred_new', 'sum'),
                  pred_d=('pred_d', 'sum'),
                  pred_e=('pred_e', 'sum'),
                  pred_f=('pred_f', 'sum'),
              ).reset_index())

def add_bias_cols(df):
    for col in ['pred_old','pred_new','pred_d','pred_e','pred_f']:
        df[f'bias_{col}'] = (df[col] / df['actual'] - 1) * 100
    return df

# ── 1. По ГРС (всі місяці разом) ─────────────────────────────────────────────
grs_total = make_agg(all_data, ['grs'])
grs_total = add_bias_cols(grs_total)
grs_total = grs_total.sort_values('actual', ascending=False)

# ── 2. По ГРС + рік/місяць ────────────────────────────────────────────────────
grs_monthly = make_agg(all_data, ['grs','year','month'])
grs_monthly = add_bias_cols(grs_monthly)
grs_monthly = grs_monthly.sort_values(['grs','year','month'])

# ── 3. По ГРС + сезон (опал./літо) ───────────────────────────────────────────
all_data['season'] = all_data['month'].apply(lambda m: 'опал.' if m in HEATING_MONTHS else 'літо')
grs_season = make_agg(all_data, ['grs','season'])
grs_season = add_bias_cols(grs_season)
grs_season = grs_season.sort_values(['grs','season'])

# ── 4. Загальна агрегація по місяцях (всі ГРС) ───────────────────────────────
total_monthly = make_agg(all_data, ['year','month'])
total_monthly = add_bias_cols(total_monthly)
total_monthly = total_monthly.sort_values(['year','month'])

# ══ ЗБЕРЕЖЕННЯ ════════════════════════════════════════════════════════════════
out_path = "data/grs_compare.xlsx"

def fmt(df):
    df = df.copy()
    for col in df.select_dtypes('float').columns:
        if 'bias' in col:
            df[col] = df[col].round(1)
        else:
            df[col] = df[col].round(0)
    return df

with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
    fmt(grs_total).to_excel(writer, sheet_name='ГРС_всього', index=False)
    fmt(grs_season).to_excel(writer, sheet_name='ГРС_сезон', index=False)
    fmt(grs_monthly).to_excel(writer, sheet_name='ГРС_помісячно', index=False)
    fmt(total_monthly).to_excel(writer, sheet_name='Загалом_по_місяцях', index=False)

print(f"\nЗбережено: {out_path}")

# ══ ВИВІД ТАБЛИЦІ ГРС TOTAL ══════════════════════════════════════════════════
print()
print("АГРЕГАТ ПО ГРС — УСІ МІСЯЦІ")
print(f"{'ГРС':<38} {'Actual':>10}  {'OLD%':>6}  {'NEW%':>6}  {'E%':>6}  {'F%':>6}  {'N':>6}")
print("="*90)

for _, row in grs_total.iterrows():
    print(f"{str(row['grs']):<38} {row['actual']:>10.0f}"
          f"  {row['bias_pred_old']:>+5.1f}%"
          f"  {row['bias_pred_new']:>+5.1f}%"
          f"  {row['bias_pred_e']:>+5.1f}%"
          f"  {row['bias_pred_f']:>+5.1f}%"
          f"  {int(row['n_consumers']):>6}")

print("="*90)
tot = all_data.dropna(subset=['consumption','pred_old'])
a   = tot['consumption'].sum()
print(f"{'РАЗОМ':<38} {a:>10.0f}"
      f"  {(tot['pred_old'].sum()/a-1)*100:>+5.1f}%"
      f"  {(tot['pred_new'].sum()/a-1)*100:>+5.1f}%"
      f"  {(tot['pred_e'].sum()/a-1)*100:>+5.1f}%"
      f"  {(tot['pred_f'].sum()/a-1)*100:>+5.1f}%")
