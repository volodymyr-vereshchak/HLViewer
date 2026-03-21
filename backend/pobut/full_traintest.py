"""
Train/test для ОП-груп: consumer-level 80/20 split.

Логіка eval відповідає продакшену:
  - Модеми (80% train) → профіль за поточний місяць
  - Передбачити решту споживачів (20% test) за той самий місяць
  - Температура вже врахована в даних модемів — окрема нормалізація не потрібна

Порівняння моделей для ОП-груп зимою:
  OLD: coef_area × area + coef_residents × residents  (LinearRegression)
  NEW: op_per_m2_median × area + personal_summer_baseline
  MODEL C: coef_area_clean × area + personal_summer_baseline
           (coef_area з регресії op_heating ~ area)
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import os
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import calendar
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
SUMMER_MONTHS  = {5, 6, 7, 8, 9}
RANDOM_STATE   = 42
TEST_SHARE     = 0.20   # 20% споживачів — тестова вибірка

# ─── 1. Load & melt smartmeter CSV ───────────────────────────────────────────
print("=" * 70)
print("CONSUMER-LEVEL TRAIN/TEST — ОП-групи")
print("(80% модемів → профіль, 20% → перевірка)")
print("=" * 70)

print("\n1. Завантаження smartmeter даних...")
raw = pd.read_csv("data/input/profile_pobut_daily_result.csv",
                  sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[11:])
date_cols = raw.columns[11:].tolist()

raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()
raw['heated_area']     = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['residents']       = pd.to_numeric(raw['residents'], errors='coerce').fillna(2.0)
raw['heated_area']     = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['residents']       = raw['residents'].where(raw['residents'] > 0, 2.0)
raw['has_OP']          = raw['appliance_group'].str.contains('ОП', na=False)

print(f"   Споживачів: {len(raw)}")
print(f"   Групи: {raw['appliance_group'].value_counts().to_dict()}")

print("   Melt до довгого формату...")
long = raw.melt(
    id_vars=['account_id','appliance_group','heated_area','residents','has_OP'],
    value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date']        = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date']).copy()

# ─── 2. Monthly aggregation ───────────────────────────────────────────────────
print("\n2. Місячна агрегація...")
long['year']  = long['date'].dt.year
long['month'] = long['date'].dt.month

monthly = (long.groupby(
               ['account_id','appliance_group','heated_area','residents','has_OP','year','month'])
           .agg(cons_sum=('consumption','sum'), n_days=('date','count'))
           .reset_index())
monthly = monthly[monthly['n_days'] >= 20].copy()
monthly['days_in_m'] = monthly.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly['consumption'] = monthly['cons_sum'] / monthly['n_days'] * monthly['days_in_m']

# Базова очистка
zero_share = (monthly.groupby('account_id', group_keys=False)
              .apply(lambda x: pd.Series({'zero_share':
                     (x['cons_sum'] <= 0).mean()}))
              ['zero_share'])
monthly = monthly.join(zero_share, on='account_id')
monthly = monthly[monthly['zero_share'] < 0.5].copy()

q99 = monthly.groupby(['appliance_group','month'])['consumption'].transform('quantile', 0.99)
monthly = monthly[(monthly['consumption'] > 0) & (monthly['consumption'] <= q99)].copy()
monthly['is_heating'] = monthly['month'].isin(HEATING_MONTHS)

print(f"   Рядків: {len(monthly):,}  Споживачів: {monthly['account_id'].nunique()}")
ym_list = sorted(monthly[['year','month']].drop_duplicates().apply(
    lambda r: f"{int(r.year)}-{int(r.month):02d}", axis=1).tolist())
print(f"   Місяці: {ym_list}")

# ─── 3. Consumer-level 80/20 split ───────────────────────────────────────────
print("\n3. Consumer-level 80/20 split...")

# Спліт окремо для ОП-груп і ПГ-груп
np.random.seed(RANDOM_STATE)

op_consumers  = monthly[monthly['has_OP']]['account_id'].unique()
pg_consumers  = monthly[~monthly['has_OP']]['account_id'].unique()

op_test_mask  = np.random.rand(len(op_consumers))  < TEST_SHARE
pg_test_mask  = np.random.rand(len(pg_consumers))  < TEST_SHARE

op_test_ids   = set(op_consumers[op_test_mask])
op_train_ids  = set(op_consumers[~op_test_mask])
pg_test_ids   = set(pg_consumers[pg_test_mask])
pg_train_ids  = set(pg_consumers[~pg_test_mask])

op_train = monthly[monthly['account_id'].isin(op_train_ids)].copy()
op_test  = monthly[monthly['account_id'].isin(op_test_ids)].copy()
pg_train = monthly[monthly['account_id'].isin(pg_train_ids)].copy()
pg_test  = monthly[monthly['account_id'].isin(pg_test_ids)].copy()

print(f"   ОП train: {len(op_train_ids)} споживачів | ОП test: {len(op_test_ids)}")
print(f"   ПГ train: {len(pg_train_ids)} споживачів | ПГ test: {len(pg_test_ids)}")

# ─── 4. Build profiles from TRAIN ────────────────────────────────────────────
print("\n4. Побудова профілів на train...")

op_groups = op_train['appliance_group'].unique()
pg_groups = {'ПГ', 'ВПГ', 'ПГ,ВПГ'}

# ── 4a. OLD model: ОП зима — регресія coef_area × area + coef_res × residents ─
op_coefs_old = {}   # (group, month) -> (coef_area, coef_res)
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = op_train[(op_train['appliance_group'] == grp) & (op_train['month'] == m)
                      & op_train['is_heating']].dropna(subset=['consumption'])
        gd = gd[(gd['heated_area'] > 0) & (gd['residents'] > 0)]
        if len(gd) < 10:
            continue
        X = gd[['heated_area','residents']].values
        y = gd['consumption'].values
        lr = LinearRegression(fit_intercept=False)
        lr.fit(X, y)
        op_coefs_old[(grp, m)] = (max(0.0, lr.coef_[0]), max(0.0, lr.coef_[1]))

print(f"   OLD: {len(op_coefs_old)} (group,month) коефіцієнтів")

# ── 4b. Літній baseline: медіана кожного споживача за місяці 6,7,8 ─────────
# Використовуємо ТІЛЬКИ train-споживачів для обрахунку групових fallback
# Для test-споживачів: їх власні літні місяці (вони не брали участь в навчанні)
summer_train = op_train[op_train['month'].isin([6, 7, 8])].copy()
summer_all   = monthly[monthly['month'].isin([6, 7, 8]) & monthly['has_OP']].copy()

# Персональний baseline: медіана літнього споживання по кожному споживачу (train + test)
consumer_summer_baseline = (
    summer_all.groupby('account_id')['consumption'].median().to_dict()
)

# Груповий fallback (тільки з train)
group_summer_overall = {}
for grp in op_groups:
    vals = summer_train[summer_train['appliance_group'] == grp]['consumption']
    if len(vals) >= 5:
        group_summer_overall[grp] = vals.median()

print(f"   Споживачів з особистим літнім baseline: {len(consumer_summer_baseline)}")
for grp, v in sorted(group_summer_overall.items()):
    print(f"   Груповий baseline {grp}: {v:.1f} m3/міс")

# ── 4c. NEW model: op_per_m2_median × area + personal_summer_baseline ─────
op_train_w = op_train[op_train['is_heating']].copy()
op_train_w['summer_base'] = op_train_w.apply(
    lambda r: consumer_summer_baseline.get(r['account_id'],
              group_summer_overall.get(r['appliance_group'], 0.0)), axis=1)
op_train_w['op_heating'] = np.maximum(0.0, op_train_w['consumption'] - op_train_w['summer_base'])
op_train_w['op_per_m2']  = np.where(
    op_train_w['heated_area'] > 0,
    op_train_w['op_heating'] / op_train_w['heated_area'],
    np.nan
)

op_per_m2_profile = {}   # (group, month) -> median op_per_m2
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = op_train_w[(op_train_w['appliance_group'] == grp) & (op_train_w['month'] == m)]
        gd = gd.dropna(subset=['op_per_m2'])
        if len(gd) >= 10:
            op_per_m2_profile[(grp, m)] = gd['op_per_m2'].median()

print(f"   NEW: {len(op_per_m2_profile)} (group,month) op_per_m2 профілів")
print(f"\n   op_per_m2 по (група, місяць) [m³/m²/міс]:")
for grp in sorted(set(g for g, m in op_per_m2_profile)):
    vals_str = "  ".join(f"[{m:02d}]={op_per_m2_profile[(grp, m)]:.3f}"
                         for m in sorted(HEATING_MONTHS) if (grp, m) in op_per_m2_profile)
    print(f"     {grp}: {vals_str}")

# ── 4d. MODEL C: регресія op_heating ~ area + personal_summer_baseline ─────
op_coefs_c = {}   # (group, month) -> coef_area_clean
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = op_train_w[(op_train_w['appliance_group'] == grp) & (op_train_w['month'] == m)]
        gd = gd.dropna(subset=['op_heating'])
        gd = gd[gd['heated_area'] > 0]
        if len(gd) < 10:
            continue
        X = gd[['heated_area']].values
        y = gd['op_heating'].values
        lr_c = LinearRegression(fit_intercept=False)
        lr_c.fit(X, y)
        op_coefs_c[(grp, m)] = max(0.0, lr_c.coef_[0])

print(f"   MODEL C: {len(op_coefs_c)} (group,month) clean area коефіцієнтів")
print(f"\n   coef_area_clean по (група, місяць):")
for grp in sorted(set(g for g, m in op_coefs_c)):
    vals_str = "  ".join(f"[{m:02d}]={op_coefs_c[(grp, m)]:.3f}"
                         for m in sorted(HEATING_MONTHS) if (grp, m) in op_coefs_c)
    print(f"     {grp}: {vals_str}")

# ── 4e. ОП літо: медіана (group, month) з train ────────────────────────────
op_summer_median = {}
for grp in op_groups:
    for m in SUMMER_MONTHS:
        gd = op_train[(op_train['appliance_group'] == grp) & (op_train['month'] == m)
                      & ~op_train['is_heating']]
        if len(gd) >= 5:
            op_summer_median[(grp, m)] = gd['consumption'].median()

# ── 4f. ПГ / ВПГ / ПГ,ВПГ: медіана (group, month) ─────────────────────────
pg_median = {}
for grp in pg_groups:
    for m in range(1, 13):
        gd = pg_train[(pg_train['appliance_group'] == grp) & (pg_train['month'] == m)]
        if len(gd) >= 5:
            pg_median[(grp, m)] = gd['consumption'].median()

print(f"\n   ПГ/ВПГ/ПГ,ВПГ: {len(pg_median)} (group,month) медіан")

# ─── 5. Predict on TEST ───────────────────────────────────────────────────────
print("\n5. Предикт на test-споживачах...")

OP_FB = {'ОП,ПГ':'ОП,ПГ', 'ОП,ПГ,ВПГ':'ОП,ПГ,ВПГ', 'ОП,ВПГ':'ОП,ПГ,ВПГ', 'ОП':'ОП,ПГ'}

# Об'єднуємо ОП + ПГ тест
test = pd.concat([op_test, pg_test], ignore_index=True)
test['predicted_old'] = np.nan
test['predicted_new'] = np.nan
test['predicted_c']   = np.nan

for idx, row in test.iterrows():
    grp   = row['appliance_group']
    m     = int(row['month'])
    is_h  = row['is_heating']
    has_op = row['has_OP']

    if has_op:
        if is_h:
            fb = OP_FB.get(grp, 'ОП,ПГ,ВПГ')

            # OLD
            key = (grp, m) if (grp, m) in op_coefs_old else (fb, m)
            if key in op_coefs_old:
                ca, cr = op_coefs_old[key]
                test.at[idx, 'predicted_old'] = max(0.0, ca * row['heated_area'] + cr * row['residents'])

            # Спільний summer_baseline для NEW і MODEL C
            s_base = consumer_summer_baseline.get(row['account_id'],
                     group_summer_overall.get(grp, 0.0))

            # NEW
            key_m2 = (grp, m) if (grp, m) in op_per_m2_profile else (fb, m)
            if key_m2 in op_per_m2_profile:
                pm2 = op_per_m2_profile[key_m2]
                test.at[idx, 'predicted_new'] = max(0.0, pm2 * row['heated_area']) + s_base

            # MODEL C
            key_c = (grp, m) if (grp, m) in op_coefs_c else (fb, m)
            if key_c in op_coefs_c:
                ca_c = op_coefs_c[key_c]
                test.at[idx, 'predicted_c'] = max(0.0, ca_c * row['heated_area']) + s_base

        else:
            # Літо: медіана групи з train
            fb = OP_FB.get(grp, 'ОП,ПГ,ВПГ')
            key = (grp, m) if (grp, m) in op_summer_median else (fb, m)
            v = op_summer_median.get(key, np.nan)
            test.at[idx, 'predicted_old'] = v
            test.at[idx, 'predicted_new'] = v
            test.at[idx, 'predicted_c']   = v

    elif grp in pg_groups:
        v = pg_median.get((grp, m), np.nan)
        test.at[idx, 'predicted_old'] = v
        test.at[idx, 'predicted_new'] = v
        test.at[idx, 'predicted_c']   = v

print(f"   Рядків з предиктами: OLD={test['predicted_old'].notna().sum():,}  "
      f"NEW={test['predicted_new'].notna().sum():,}  "
      f"MODEL C={test['predicted_c'].notna().sum():,}  / {len(test):,}")

# ─── 6. Metrics helpers ───────────────────────────────────────────────────────
def met(df_sub, col):
    mask = df_sub['consumption'].notna() & df_sub[col].notna() & (df_sub['consumption'] > 0)
    if mask.sum() < 2:
        return 0, np.nan, np.nan, np.nan
    yt = df_sub.loc[mask, 'consumption'].values
    yp = df_sub.loc[mask, col].values
    mae  = np.mean(np.abs(yt - yp))
    mape = np.mean(np.abs(yt - yp) / yt) * 100
    ss_res = np.sum((yt - yp) ** 2)
    ss_tot = np.sum((yt - yt.mean()) ** 2)
    r2   = 1 - ss_res / max(ss_tot, 1e-9)
    return mask.sum(), mae, mape, r2

# ─── 7. Per-consumer metrics (інформативно) ──────────────────────────────────
print("\n" + "=" * 70)
print("PER-CONSUMER МЕТРИКИ (test, 20% holdout)")
print("(інформативно; основне — агрегат нижче)")
print("=" * 70)

W = 20
print(f"\n  {'Категорія':<{W}} {'N':>5}  {'OLD R²':>7} {'OLD MAE':>8}  {'NEW R²':>7} {'NEW MAE':>8}  {'C R²':>7} {'C MAE':>8}")
print("  " + "-" * (W + 65))

def print_row3(label, sub):
    n, mae_o, _, r2_o = met(sub, 'predicted_old')
    _, mae_n, _, r2_n = met(sub, 'predicted_new')
    _, mae_c, _, r2_c = met(sub, 'predicted_c')
    if n == 0: return
    fmt = lambda r2: f"{r2:>7.3f}" if not np.isnan(r2) else "      —"
    print(f"  {label:<{W}} {n:>5}  {fmt(r2_o)} {mae_o:>8.2f}  {fmt(r2_n)} {mae_n:>8.2f}  {fmt(r2_c)} {mae_c:>8.2f}")

op_test_df = test[test['has_OP']].copy()

print_row3("ОП,* (ВСЬОГО)", op_test_df)
print()
for grp in sorted(op_test_df['appliance_group'].unique()):
    print_row3(f"  {grp}", op_test_df[op_test_df['appliance_group'] == grp])
print()
print_row3("  ОП,* зима", op_test_df[op_test_df['is_heating']])
print_row3("  ОП,* літо", op_test_df[~op_test_df['is_heating']])
print()
for (y, m), sub in op_test_df[op_test_df['is_heating']].groupby(['year','month']):
    print_row3(f"  {y}-{m:02d}", sub)

# ─── 8. AGGREGATE metrics — головна метрика ──────────────────────────────────
print("\n" + "=" * 70)
print("АГРЕГОВАНИЙ АНАЛІЗ — сума по всіх test-споживачах за місяць")
print("(симулює: профіль з 80% модемів → передбачити решту 220K)")
print("=" * 70)

op_w_test = op_test_df[op_test_df['is_heating']].copy()

print(f"\n  {'Місяць':<9} {'Actual':>9}  "
      f"{'OLD':>9} {'OLD%':>6}  "
      f"{'NEW':>9} {'NEW%':>6}  "
      f"{'MODEL C':>9} {'C%':>6}")
print("  " + "-" * 73)

agg_rows = []
for (y, m), sub in op_w_test.groupby(['year','month']):
    mask = sub[['consumption','predicted_old','predicted_new','predicted_c']].notna().all(axis=1)
    s = sub[mask]
    if len(s) == 0: continue
    act = s['consumption'].sum()
    old = s['predicted_old'].sum()
    new = s['predicted_new'].sum()
    mc  = s['predicted_c'].sum()
    p_old = (old / act - 1) * 100 if act > 0 else 0
    p_new = (new / act - 1) * 100 if act > 0 else 0
    p_mc  = (mc  / act - 1) * 100 if act > 0 else 0
    agg_rows.append((y, m, act, old, new, mc))
    print(f"  {y}-{m:02d}    {act:>9.0f}  "
          f"{old:>9.0f} {p_old:>+5.1f}%  "
          f"{new:>9.0f} {p_new:>+5.1f}%  "
          f"{mc:>9.0f} {p_mc:>+5.1f}%")

if agg_rows:
    acts  = np.array([r[2] for r in agg_rows])
    olds  = np.array([r[3] for r in agg_rows])
    news_ = np.array([r[4] for r in agg_rows])
    mcs   = np.array([r[5] for r in agg_rows])

    def agg_r2(a, p):
        ss_res = np.sum((a - p)**2); ss_tot = np.sum((a - a.mean())**2)
        return 1 - ss_res / max(ss_tot, 1e-9)
    def agg_mape(a, p):
        return np.mean(np.abs((a - p) / a)) * 100

    print("  " + "-" * 73)
    print(f"  {'РАЗОМ':<9} {acts.sum():>9.0f}  "
          f"{olds.sum():>9.0f} {(olds.sum()/acts.sum()-1)*100:>+5.1f}%  "
          f"{news_.sum():>9.0f} {(news_.sum()/acts.sum()-1)*100:>+5.1f}%  "
          f"{mcs.sum():>9.0f} {(mcs.sum()/acts.sum()-1)*100:>+5.1f}%")

    print(f"\n  Aggregate R² (місячні суми):")
    print(f"    OLD:     R²={agg_r2(acts, olds):.4f}  MAPE={agg_mape(acts, olds):.1f}%")
    print(f"    NEW:     R²={agg_r2(acts, news_):.4f}  MAPE={agg_mape(acts, news_):.1f}%")
    print(f"    MODEL C: R²={agg_r2(acts, mcs):.4f}  MAPE={agg_mape(acts, mcs):.1f}%")

    # По ОП-групах
    print(f"\n  По ОП-групах (загальний % зміщення по всіх місяцях):")
    for grp in sorted(op_w_test['appliance_group'].unique()):
        s = op_w_test[op_w_test['appliance_group'] == grp].copy()
        mask = s[['consumption','predicted_old','predicted_new','predicted_c']].notna().all(axis=1)
        s = s[mask]
        if len(s) == 0: continue
        a = s['consumption'].sum()
        o = s['predicted_old'].sum()
        n_ = s['predicted_new'].sum()
        c_ = s['predicted_c'].sum()
        print(f"    {grp:<14}: actual={a:>8.0f}  "
              f"OLD={o:>8.0f} ({(o/a-1)*100:>+5.1f}%)  "
              f"NEW={n_:>8.0f} ({(n_/a-1)*100:>+5.1f}%)  "
              f"MODEL C={c_:>8.0f} ({(c_/a-1)*100:>+5.1f}%)")

# ─── 9. Загальний підсумок ────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("ЗАГАЛЬНИЙ ПІДСУМОК (ОП зима, test 20%)")
print("=" * 70)
n_o, mae_o, mape_o, r2_o = met(op_w_test, 'predicted_old')
n_n, mae_n, mape_n, r2_n = met(op_w_test, 'predicted_new')
n_c, mae_c, mape_c, r2_c = met(op_w_test, 'predicted_c')
print(f"\n  {'Модель':<12} {'N':>5}  {'R²':>7}  {'MAE':>8}  {'MAPE':>7}")
print(f"  {'-'*45}")
print(f"  {'OLD':<12} {n_o:>5}  {r2_o:>7.3f}  {mae_o:>8.2f}  {mape_o:>6.1f}%")
print(f"  {'NEW':<12} {n_n:>5}  {r2_n:>7.3f}  {mae_n:>8.2f}  {mape_n:>6.1f}%")
print(f"  {'MODEL C':<12} {n_c:>5}  {r2_c:>7.3f}  {mae_c:>8.2f}  {mape_c:>6.1f}%")
print("=" * 70)
