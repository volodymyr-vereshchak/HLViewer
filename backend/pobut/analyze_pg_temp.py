import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import numpy as np
import pandas as pd
from scipy import stats

# ── load & prep ──────────────────────────────────────────────────────────────
raw = pd.read_csv("data/input/profile_pobut_daily_result.csv", sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','profile_no',
        'heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[11:])
date_cols = raw.columns[11:].tolist()
raw['account_id'] = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()

long = raw.melt(id_vars=['account_id','appliance_group'],
    value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date'] = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date']).copy()
long['year'] = long['date'].dt.year
long['month'] = long['date'].dt.month

monthly = (long.groupby(['account_id','appliance_group','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count')).reset_index())
monthly = monthly[monthly['n_days'] >= 20].copy()
monthly['days_in_m'] = monthly.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly['consumption'] = monthly['cons_sum'] / monthly['n_days'] * monthly['days_in_m']

# filter quality: zero-share < 50%, top 1% outlier removal
zero_share = (monthly.groupby('account_id', group_keys=False)
    .apply(lambda x: pd.Series({'zero_share': (x['cons_sum']<=0).mean()}))['zero_share'])
monthly = monthly.join(zero_share, on='account_id')
monthly = monthly[monthly['zero_share'] < 0.5].copy()
q99 = monthly.groupby(['appliance_group','month'])['consumption'].transform('quantile', 0.99)
monthly = monthly[(monthly['consumption']>0) & (monthly['consumption']<=q99)].copy()

# ── чисті групи (без ОП) ─────────────────────────────────────────────────────
PURE_GROUPS = ['ПГ', 'ВПГ', 'ПГ,ВПГ']
pure = monthly[monthly['appliance_group'].isin(PURE_GROUPS)].copy()

print(f"Чисті групи (без ОП): {pure['appliance_group'].value_counts().to_dict()}")
print()

# ── Місячні медіани і seasonal ratio ────────────────────────────────────────
results = []
for grp in PURE_GROUPS:
    grp_data = pure[pure['appliance_group'] == grp]
    if len(grp_data) == 0:
        print(f"  Група {grp}: немає даних")
        continue

    # медіана по всіх роках і споживачах для кожного місяця
    monthly_med = grp_data.groupby('month')['consumption'].median()
    ref = monthly_med.get(7, monthly_med.mean())

    print(f"\n{'='*60}")
    print(f"Група: {grp}  (N споживачів: {grp_data['account_id'].nunique()})")
    print(f"{'Місяць':<8} {'Медіана':>10} {'Ratio/Jul':>10}")
    print(f"{'-'*30}")

    for m in range(1, 13):
        med = monthly_med.get(m, np.nan)
        ratio = med / ref if (ref > 0 and not np.isnan(med)) else np.nan
        print(f"  {m:2d}     {med:>10.2f} {ratio:>10.3f}")
        results.append({'group': grp, 'month': m,
                        'median_consumption': med,
                        'seasonal_ratio': ratio})

# ── Температурна залежність ───────────────────────────────────────────────────
weather = pd.read_csv("../data/weather_2025_daily_contractual.csv")
weather['date'] = pd.to_datetime(weather['date'])
weather['year'] = weather['date'].dt.year
weather['month'] = weather['date'].dt.month
monthly_temp = weather.groupby(['year','month'])['temperature'].mean().reset_index()
monthly_temp.columns = ['year','month','avg_temp']

print(f"\n\n{'='*60}")
print("Кореляція з температурою")
print(f"{'='*60}")

corr_results = {}
for grp in PURE_GROUPS:
    grp_data = pure[pure['appliance_group'] == grp].copy()
    if len(grp_data) == 0:
        continue

    merged = grp_data.merge(monthly_temp, on=['year','month'], how='inner')
    merged = merged.dropna(subset=['consumption','avg_temp'])

    if len(merged) < 20:
        print(f"\n{grp}: замало даних для регресії ({len(merged)})")
        continue

    slope, intercept, r_val, p_val, std_err = stats.linregress(
        merged['avg_temp'], merged['consumption'])

    print(f"\nГрупа: {grp}  (N obs: {len(merged)})")
    print(f"  Pearson r    = {r_val:.3f}  (p={p_val:.4e})")
    print(f"  Slope        = {slope:.3f} м³/°C  (Intercept={intercept:.2f})")
    print(f"  Interpretation: на 1°C підвищення → {slope:.2f} м³/споживача/місяць")

    # Agregate по місяцях (медіана по споживачах)
    agg_m = merged.groupby('month').agg(
        med_cons=('consumption','median'),
        avg_t=('avg_temp','mean')).reset_index()
    r_agg, p_agg, _, _, _ = stats.linregress(agg_m['avg_t'], agg_m['med_cons'])

    print(f"  Aggr. r (по місяцях) = {r_agg:.3f}")
    corr_results[grp] = r_val

# Додаємо corr_temp до results
for r in results:
    r['corr_temp'] = corr_results.get(r['group'], np.nan)

# ── Збереження ────────────────────────────────────────────────────────────────
df_out = pd.DataFrame(results)
df_out = df_out.round({'median_consumption': 3, 'seasonal_ratio': 4, 'corr_temp': 4})
df_out.to_csv("data/profiles/pg_seasonal_ratios.csv", index=False, encoding='utf-8')
print(f"\n\nЗбережено: data/profiles/pg_seasonal_ratios.csv ({len(df_out)} рядків)")

# ── Підсумкова таблиця seasonal_ratio ────────────────────────────────────────
print(f"\n\n{'='*60}")
print("SEASONAL RATIO (відносно July=1.0)")
print(f"{'='*60}")
pivot = df_out.pivot(index='month', columns='group', values='seasonal_ratio')
# Виводимо в правильному порядку груп
cols = [c for c in PURE_GROUPS if c in pivot.columns]
pivot = pivot[cols]
print(pivot.to_string(float_format='{:.3f}'.format))

print(f"\n\nМаппінг ОП-груп → seasonal_ratio:")
print(f"  ОП,ПГ      → ПГ    ratio")
print(f"  ОП,ВПГ     → ВПГ або ПГ,ВПГ ratio")
print(f"  ОП,ПГ,ВПГ  → ПГ,ВПГ ratio")
