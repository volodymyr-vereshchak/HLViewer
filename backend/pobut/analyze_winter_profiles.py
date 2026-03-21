"""
analyze_winter_profiles.py

Аналіз зимових удільних профілів: м³ / м² площі.

Ідея:
  vol_i(m) = k_i × area_i × P(m)

  де:
    P(m)  — глобальний удільний профіль (м³/м²/місяць) з модемних даних
    area_i — опалювальна площа споживача
    k_i   — індивідуальний коефіцієнт масштабу з біліцнгу:
              k_i = mean(billing_i(m) / (area_i × P(m))) for m in HEATING_M

Етапи:
  1. Модемні дані → P(m) = median(consumption_i(m) / area_i)
  2. Порівняння профілів по групах (ОП,ПГ vs ОП,ПГ,ВПГ; Прив vs МКД)
  3. Чи можна мати один глобальний P(m)?
  4. Розподіл k_i по споживачах (з біліцнгу)
  5. Аутлайєри: k_i >> 1 або профіль сильно відрізняється від P(m)

Виходи:
  data/winter_profiles.xlsx
    - Profiles_by_group   (P(m) по (grp, ct))
    - Profile_global      (глобальний P(m), порівняння груп)
    - Ki_distribution     (розподіл k_i з білінгу по групах)
    - Ki_by_GRS           (медіана k_i per GRS×group→ для Config K)
    - Outliers_high_k     (k_i > threshold → комерційні/нетипові)
    - Outliers_low_corr   (профіль сильно відрізняється від P(m))
    - Profile_vs_billing  (порівняння модемного P(m) і білінгового)
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import re
import numpy as np
import pandas as pd
from scipy.stats import pearsonr

from pobut_predictor import (
    PobUtPredictor, CT_PRIV, HEATING_MONTHS, SUMMER_MONTHS,
    META_COLS, GROUP_MERGE, SKIP_GROUPS,
)

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
OUT_EXCEL  = 'data/winter_profiles.xlsx'

# Місяці опалення в порядку сезону Oct→Apr
HEATING_M   = [10, 11, 12, 1, 2, 3, 4]
SUMMER_M    = [5, 6, 7, 8, 9]
MONTH_UA    = {
    1:'Січ', 2:'Лют', 3:'Бер', 4:'Кві', 5:'Тра', 6:'Чер',
    7:'Лип', 8:'Сер', 9:'Вер', 10:'Жов', 11:'Лис', 12:'Гру',
}
MONTH_ENG   = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
}
_DATE_PAT   = re.compile(r'^\d{2}\.\d{2}\.\d{4}$')

# Пороги
MIN_AREA       = 10.0    # мін. площа м² (відкидаємо явно помилкові)
MAX_AREA       = 500.0   # макс. площа (>500 — нежитло?)
MIN_MONTHS_MD  = 3       # мін. місяців у модемних даних для споживача
K_HIGH_THRESH  = 3.0     # k_i > 3 → аутлайєр (комерційний або нетиповий)
K_LOW_THRESH   = 0.1     # k_i < 0.1 → аутлайєр (майже нульове споживання)
CORR_THRESH    = 0.80    # мін. кореляція з P(m) → нормальний профіль


# ══════════════════════════════════════════════════════════════════════════════
# 1. МОДЕМНІ ДАНІ → P(m) = median(consumption / area)
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("1. Завантаження модемних даних...")

raw = pd.read_csv(MODEM_FILE, sep=';', low_memory=False, encoding='utf-8')
raw.columns = META_COLS + list(raw.columns[12:])
raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
raw['consumer_type']   = raw['consumer_type'].fillna(CT_PRIV)
raw['heated_area']     = pd.to_numeric(
    raw['heated_area'].astype(str).str.strip(), errors='coerce'
).fillna(55.0).clip(lower=1.0)
raw['has_OP'] = raw['appliance_group'].str.contains('ОП', na=False)

modem_ids = set(raw['account_id'].dropna().astype(int))
print(f"  Модемних споживачів: {len(modem_ids):,}")

# Melt → long format
date_cols = [c for c in raw.columns if _DATE_PAT.match(str(c))]
long = raw[raw['has_OP']].melt(
    id_vars=['account_id', 'appliance_group', 'consumer_type', 'heated_area', 'has_OP'],
    value_vars=date_cols, var_name='date_str', value_name='consumption',
)
long['date']        = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date', 'consumption']).copy()
long['year']  = long['date'].dt.year
long['month'] = long['date'].dt.month

# Фільтруємо: тільки опалювальні місяці 2024-2025
long_heat = long[long['month'].isin(HEATING_M)].copy()

# Місячна агрегація (≥ 20 днів з читанням)
import calendar
long_heat['days_in_m'] = long_heat.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1
)
monthly_md = (
    long_heat.groupby(['account_id', 'appliance_group', 'consumer_type',
                       'heated_area', 'year', 'month'])
    .agg(cons_sum=('consumption', 'sum'), n_days=('date', 'count'),
         days_in_m=('days_in_m', 'first'))
    .reset_index()
)
monthly_md = monthly_md[monthly_md['n_days'] >= 20].copy()
monthly_md['consumption'] = (
    monthly_md['cons_sum'] / monthly_md['n_days'] * monthly_md['days_in_m']
)

print(f"  ОП-місяців модемних (опал., ≥20 днів): {len(monthly_md):,} "
      f"(споживачів: {monthly_md['account_id'].nunique():,})")

# ── Удільне споживання (м³/м²) ────────────────────────────────────────────
monthly_md['specific'] = monthly_md['consumption'] / monthly_md['heated_area']

# Фільтруємо площу
monthly_md_f = monthly_md[
    (monthly_md['heated_area'] >= MIN_AREA) &
    (monthly_md['heated_area'] <= MAX_AREA) &
    (monthly_md['specific'] > 0)
].copy()

print(f"  Після фільтру площі [{MIN_AREA}–{MAX_AREA} м²]: {len(monthly_md_f):,} рядків")


# ══════════════════════════════════════════════════════════════════════════════
# 2. P(m) по групах
# ══════════════════════════════════════════════════════════════════════════════
print("\n2. Профілі P(m) по групах...")

profiles_by_group = (
    monthly_md_f
    .groupby(['appliance_group', 'consumer_type', 'month'])
    ['specific']
    .agg(
        n         =('count'),
        median_sp =('median'),
        mean_sp   =('mean'),
        p10_sp    =(lambda x: x.quantile(0.10)),
        p25_sp    =(lambda x: x.quantile(0.25)),
        p75_sp    =(lambda x: x.quantile(0.75)),
        p90_sp    =(lambda x: x.quantile(0.90)),
        cv_sp     =(lambda x: x.std() / x.mean() if x.mean() > 0 else np.nan),
    )
    .reset_index()
)
profiles_by_group['month_ua'] = profiles_by_group['month'].map(MONTH_UA)

print("\n  Медіана P(m) [м³/м²] по групах (лише опалювальні місяці):")
for (grp, ct), sub in profiles_by_group.groupby(['appliance_group', 'consumer_type']):
    vals = sub.set_index('month')['median_sp'].to_dict()
    line = "  ".join(f"{MONTH_UA[m]}:{vals.get(m,np.nan):.3f}" for m in HEATING_M)
    n_total = sub['n'].sum()
    cv_avg  = sub['cv_sp'].mean()
    print(f"  ({grp:12s}, {ct[:15]:15s}) n={n_total:,}  CV_avg={cv_avg:.2f}  | {line}")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Глобальний P(m) — чи всі групи мають схожий профіль?
# ══════════════════════════════════════════════════════════════════════════════
print("\n3. Глобальний P(m)...")

# Global = усі ОП-модеми разом (без розподілу по групах)
global_profile_raw = (
    monthly_md_f.groupby('month')['specific']
    .agg(n='count', median_sp='median', mean_sp='mean',
         p25=lambda x: x.quantile(0.25),
         p75=lambda x: x.quantile(0.75))
    .reset_index()
)
global_profile_raw['month_ua'] = global_profile_raw['month'].map(MONTH_UA)

# Нормалізація: P_norm(m) = median_sp(m) / median_sp(Jan)  ← Jan = базовий місяць
jan_val = global_profile_raw.set_index('month')['median_sp'].get(1, 1.0)
global_profile_raw['P_norm'] = (global_profile_raw['median_sp'] / jan_val).round(4)

print("\n  Глобальний P(m) [м³/м²] і нормалізований (Jan=1.0):")
for _, r in global_profile_raw.sort_values('month').iterrows():
    if r['month'] not in HEATING_M:
        continue
    print(f"    {r['month_ua']:4s}: median={r['median_sp']:.4f}  norm={r['P_norm']:.3f}  "
          f"n={r['n']:,}  IQR=[{r['p25']:.3f}, {r['p75']:.3f}]")

# Порівняння груп: кореляція між профілем групи і глобальним профілем
P_global = global_profile_raw.set_index('month')['median_sp'].to_dict()
print("\n  Кореляція групового профілю з глобальним:")
group_corrs = []
for (grp, ct), sub in profiles_by_group.groupby(['appliance_group', 'consumer_type']):
    sub_heat = sub[sub['month'].isin(HEATING_M)].set_index('month')['median_sp']
    common_m = [m for m in HEATING_M if m in sub_heat.index and m in P_global]
    if len(common_m) < 4:
        continue
    x = [P_global[m] for m in common_m]
    y = [sub_heat[m] for m in common_m]
    corr, _ = pearsonr(x, y)
    ratio_to_global = np.mean([y[i]/x[i] for i in range(len(x))])
    group_corrs.append({'appliance_group': grp, 'consumer_type': ct,
                        'corr': round(corr, 3), 'ratio_to_global': round(ratio_to_global, 3)})
    print(f"    ({grp:12s}, {ct[:15]:15s}): corr={corr:.3f}  ratio={ratio_to_global:.3f}")

group_corrs_df = pd.DataFrame(group_corrs)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Індивідуальна кореляція кожного модемного споживача з P(m)
# ══════════════════════════════════════════════════════════════════════════════
print("\n4. Кореляція кожного модемного споживача з P(m)...")

consumer_profiles = []
for aid, sub in monthly_md_f[monthly_md_f['month'].isin(HEATING_M)].groupby('account_id'):
    if sub['month'].nunique() < MIN_MONTHS_MD:
        continue
    # Average across years if consumer has data for same month in multiple years
    sub_m = sub.groupby('month')['specific'].mean()
    common_m = [m for m in HEATING_M if m in sub_m.index and m in P_global]
    if len(common_m) < MIN_MONTHS_MD:
        continue
    x = [P_global[m] for m in common_m]
    y_sp = [float(sub_m.at[m]) for m in common_m]

    corr, _ = pearsonr(x, y_sp)
    # k_i = mean(specific_i / P_global) = масштаб відносно глобального профілю
    k_i = np.mean([y_sp[i] / x[i] for i in range(len(x))])

    grp  = sub['appliance_group'].iloc[0]
    ct   = sub['consumer_type'].iloc[0]
    area = sub['heated_area'].iloc[0]

    consumer_profiles.append({
        'account_id': aid, 'appliance_group': grp, 'consumer_type': ct,
        'heated_area': area, 'n_months': len(common_m),
        'corr_with_P': round(corr, 3), 'k_i_modem': round(k_i, 3),
        'specific_jan': float(sub_m.get(1, np.nan)),
        'specific_jul': float(sub_m.get(7, np.nan)),
    })

consumer_prof_df = pd.DataFrame(consumer_profiles)
print(f"  Оброблено споживачів: {len(consumer_prof_df):,}")

# Розподіл кореляцій
for grp_ct, sub in consumer_prof_df.groupby(['appliance_group', 'consumer_type']):
    corr_ok = (sub['corr_with_P'] >= CORR_THRESH).mean() * 100
    k_med   = sub['k_i_modem'].median()
    k_cv    = sub['k_i_modem'].std() / sub['k_i_modem'].mean()
    print(f"  ({grp_ct[0]:12s}, {grp_ct[1][:15]:15s}): "
          f"corr≥{CORR_THRESH}: {corr_ok:.1f}%  "
          f"k_median={k_med:.3f}  k_CV={k_cv:.2f}")

# Outliers за кореляцією (з модемів)
low_corr = consumer_prof_df[consumer_prof_df['corr_with_P'] < CORR_THRESH]
print(f"\n  Аутлайєрів за профілем (corr < {CORR_THRESH}): {len(low_corr):,} "
      f"({len(low_corr)/len(consumer_prof_df)*100:.1f}%)")
print(low_corr.groupby(['appliance_group', 'consumer_type'])[['account_id']].count()
      .rename(columns={'account_id': 'n_outliers'}).to_string())


# ══════════════════════════════════════════════════════════════════════════════
# 5. k_i З БІЛІНГУ — розподіл по групах і ГРС
# ══════════════════════════════════════════════════════════════════════════════
print("\n5. k_i з білінгових даних...")

ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)
ap['heated_area']     = pd.to_numeric(
    ap['heated_area'].astype(str).str.strip(), errors='coerce'
).fillna(55.0).clip(lower=1.0)
ap['has_OP'] = ap['appliance_group'].str.contains('ОП', na=False)

# Тільки ОП-споживачі (не-модемні)
ap_op = ap[ap['has_OP'] & ~ap['account_id'].isin(modem_ids)].copy()
ap_op = ap_op[
    (ap_op['heated_area'] >= MIN_AREA) &
    (ap_op['heated_area'] <= MAX_AREA) &
    ap_op['grs'].notna() &
    ~ap_op['appliance_group'].isin(SKIP_GROUPS)
].copy()
print(f"  ОП не-модемних (після фільтру площі): {len(ap_op):,}")

# Сума білінгу за опалювальні місяці
heat_bill_sum = pd.Series(0.0, index=ap_op.index)
heat_months_present = 0
for eng_name, m in MONTH_ENG.items():
    if m not in HEATING_M:
        continue
    col = f'{eng_name}_2025'
    if col not in ap.columns:
        continue
    vals = pd.to_numeric(ap.set_index('account_id')[col], errors='coerce').reindex(
        ap_op['account_id'].values
    ).fillna(0).values
    heat_bill_sum += vals
    heat_months_present += 1

ap_op['heat_bill_sum'] = heat_bill_sum
print(f"  Опалювальних місяців у білінгу 2025: {heat_months_present}")

# Сума P(m) × area = expected total heating consumption (м³) for "k=1"
P_heat_sum = sum(P_global.get(m, 0) for m in HEATING_M)
print(f"  Сума P(m) за опалювальні місяці: {P_heat_sum:.4f} м³/м²")

# k_i = actual_heating_sum / (area × P_heat_sum)
ap_op['expected_heat'] = ap_op['heated_area'] * P_heat_sum
ap_op['k_i'] = np.where(
    (ap_op['expected_heat'] > 0) & (ap_op['heat_bill_sum'] > 0),
    ap_op['heat_bill_sum'] / ap_op['expected_heat'],
    np.nan,
)
valid_k = ap_op[ap_op['k_i'].notna()].copy()
print(f"  Споживачів з k_i (є білінг опалення): {len(valid_k):,} "
      f"({len(valid_k)/len(ap_op)*100:.1f}%)")

print("\n  Розподіл k_i по групах:")
ki_dist = (
    valid_k.groupby(['appliance_group', 'consumer_type'])['k_i']
    .agg(
        n         =('count'),
        median_k  =('median'),
        mean_k    =('mean'),
        p10_k     =(lambda x: x.quantile(0.10)),
        p25_k     =(lambda x: x.quantile(0.25)),
        p75_k     =(lambda x: x.quantile(0.75)),
        p90_k     =(lambda x: x.quantile(0.90)),
        p99_k     =(lambda x: x.quantile(0.99)),
        frac_high =(lambda x: (x > K_HIGH_THRESH).mean()),
        frac_low  =(lambda x: (x < K_LOW_THRESH).mean()),
    )
    .reset_index()
)
for _, r in ki_dist.iterrows():
    print(f"  ({r['appliance_group']:12s}, {r['consumer_type'][:15]:15s})  n={r['n']:,}  "
          f"median={r['median_k']:.3f}  mean={r['mean_k']:.3f}  "
          f"p10={r['p10_k']:.2f} p90={r['p90_k']:.2f}  "
          f"high>{K_HIGH_THRESH}: {r['frac_high']*100:.1f}%  "
          f"low<{K_LOW_THRESH}: {r['frac_low']*100:.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 6. k_i per GRS × group (для Config K)
# ══════════════════════════════════════════════════════════════════════════════
print("\n6. k_i per GRS × group (медіана та середнє)...")

ki_by_grs = (
    valid_k.groupby(['grs', 'appliance_group', 'consumer_type'])['k_i']
    .agg(
        n=('count'),
        median_k=('median'),
        mean_k=('mean'),
        trimmed_mean_k=(lambda x: x.clip(upper=x.quantile(0.95)).mean()),
        p25_k=(lambda x: x.quantile(0.25)),
        p75_k=(lambda x: x.quantile(0.75)),
    )
    .reset_index()
)
print(f"  Всього комірок (grs, grp, ct): {len(ki_by_grs):,}")
print(f"  Унікальних ГРС: {ki_by_grs['grs'].nunique():,}")

FOCUS_GRS = [
    'С-1(Запоріж) ГРС-1,3 кільце',
    'ГРС (2) м.Запоріжжя',
    'С-2 (Вільнянс) ГРС Вільнянськ',
]
print("\n  k_i по фокус-ГРС:")
for grs in FOCUS_GRS:
    sub = ki_by_grs[ki_by_grs['grs'] == grs].sort_values('n', ascending=False)
    if sub.empty:
        continue
    print(f"  {grs}:")
    for _, r in sub.head(5).iterrows():
        print(f"    ({r['appliance_group']:12s}, {r['consumer_type'][:15]:15s})  "
              f"n={r['n']:,}  median_k={r['median_k']:.3f}  mean_k={r['mean_k']:.3f}  "
              f"trim_mean_k={r['trimmed_mean_k']:.3f}")


# ══════════════════════════════════════════════════════════════════════════════
# 7. Аутлайєри: високий k_i
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n7. Аутлайєри: k_i > {K_HIGH_THRESH} ...")

outliers_high_k = (
    valid_k[valid_k['k_i'] > K_HIGH_THRESH]
    [['account_id', 'grs', 'appliance_group', 'consumer_type',
      'heated_area', 'heat_bill_sum', 'expected_heat', 'k_i']]
    .sort_values('k_i', ascending=False)
)
print(f"  Кількість: {len(outliers_high_k):,} ({len(outliers_high_k)/len(valid_k)*100:.2f}% від усіх)")
print(f"  Топ-10:")
print(outliers_high_k.head(10).to_string(index=False))

# Внесок аутлайєрів у загальний білінговий об'єм
total_heat_bill = valid_k['heat_bill_sum'].sum()
outlier_heat_bill = outliers_high_k['heat_bill_sum'].sum()
print(f"\n  Внесок k>{K_HIGH_THRESH} у загальний опалювальний білінг: "
      f"{outlier_heat_bill:,.0f} м³ = {outlier_heat_bill/total_heat_bill*100:.2f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 8. Порівняння П(m) модем vs білінг
# ══════════════════════════════════════════════════════════════════════════════
print("\n8. Порівняння P(m): модем vs білінг...")

# Для не-модемних ОП: обчислити billing_m / area і взяти медіану
bill_profiles = []
for eng_name, m in MONTH_ENG.items():
    if m not in HEATING_M:
        continue
    col = f'{eng_name}_2025'
    if col not in ap.columns:
        continue
    vals = pd.to_numeric(ap.set_index('account_id')[col], errors='coerce').reindex(
        ap_op['account_id'].values
    ).fillna(0).values
    tmp = ap_op.copy()
    tmp['billing_m'] = vals
    tmp = tmp[tmp['billing_m'] > 0].copy()
    tmp['specific_m'] = tmp['billing_m'] / tmp['heated_area']

    bill_profiles.append({
        'month': m,
        'n': len(tmp),
        'median_sp_bill': tmp['specific_m'].median(),
        'mean_sp_bill':   tmp['specific_m'].mean(),
        'p25_bill': tmp['specific_m'].quantile(0.25),
        'p75_bill': tmp['specific_m'].quantile(0.75),
    })

bill_prof_df = pd.DataFrame(bill_profiles).sort_values('month')
bill_prof_df['P_modem'] = bill_prof_df['month'].map(P_global)
bill_prof_df['ratio_bill_modem'] = (
    bill_prof_df['median_sp_bill'] / bill_prof_df['P_modem']
).round(3)
bill_prof_df['month_ua'] = bill_prof_df['month'].map(MONTH_UA)

print("\n  Порівняння удільного профілю (м³/м²):")
print(f"  {'Місяць':>6}  {'P_modem':>9}  {'bill_med':>9}  {'ratio':>7}  {'n_bill':>8}")
for _, r in bill_prof_df.iterrows():
    if r['month'] not in HEATING_M:
        continue
    print(f"  {r['month_ua']:>6}  {r['P_modem']:>9.4f}  {r['median_sp_bill']:>9.4f}  "
          f"{r['ratio_bill_modem']:>7.3f}  {r['n']:>8,}")


# ══════════════════════════════════════════════════════════════════════════════
# Excel output
# ══════════════════════════════════════════════════════════════════════════════
print(f"\nЗберігаємо {OUT_EXCEL} ...")

# Профіль по місяцях широкий формат для груп
pivot_grp = profiles_by_group[profiles_by_group['month'].isin(HEATING_M)].pivot_table(
    index=['appliance_group', 'consumer_type'],
    columns='month',
    values='median_sp',
).round(4)
pivot_grp.columns = [f'P_{MONTH_UA[int(c)]}' for c in pivot_grp.columns]
pivot_grp = pivot_grp.reset_index()
pivot_grp['corr_with_global'] = pivot_grp.apply(
    lambda r: group_corrs_df[
        (group_corrs_df['appliance_group'] == r['appliance_group']) &
        (group_corrs_df['consumer_type'] == r['consumer_type'])
    ]['corr'].values[0] if len(group_corrs_df[
        (group_corrs_df['appliance_group'] == r['appliance_group']) &
        (group_corrs_df['consumer_type'] == r['consumer_type'])
    ]) > 0 else np.nan, axis=1
)

with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as xw:
    # Sheet 1: Profiles by group (wide)
    pivot_grp.to_excel(xw, sheet_name='Профілі_по_групах', index=False)

    # Sheet 2: Global profile + comparison
    compare_cols = bill_prof_df[['month_ua', 'P_modem', 'median_sp_bill',
                                 'mean_sp_bill', 'ratio_bill_modem',
                                 'p25_bill', 'p75_bill', 'n']].copy()
    compare_cols.to_excel(xw, sheet_name='P_модем_vs_білінг', index=False)

    # Sheet 3: All group details (long)
    profiles_by_group[profiles_by_group['month'].isin(HEATING_M)].to_excel(
        xw, sheet_name='Профілі_деталі', index=False)

    # Sheet 4: Ki distribution by group
    ki_dist.to_excel(xw, sheet_name='Ki_розподіл_група', index=False)

    # Sheet 5: Ki by GRS (for Config K)
    ki_by_grs.sort_values(['grs', 'appliance_group']).to_excel(
        xw, sheet_name='Ki_по_ГРС', index=False)

    # Sheet 6: Outliers high k
    outliers_high_k.head(2000).to_excel(xw, sheet_name='Аутлайєри_k_high', index=False)

    # Sheet 7: Consumer modem profiles + correlation
    consumer_prof_df.sort_values('corr_with_P').to_excel(
        xw, sheet_name='Модем_кореляції', index=False)

    # Sheet 8: Low-corr modem outliers
    low_corr.sort_values('corr_with_P').to_excel(
        xw, sheet_name='Аутлайєри_профіль', index=False)

print(f"[OK] {OUT_EXCEL}")
print("\n=== DONE ===")
