"""
Порівняння двох підходів для прогнозування літнього споживання ОП-груп:
  Модель A (поточна): prediction = mean(train, group, month)
  Модель B (residents): prediction = coef_residents * residents (fit_intercept=False)

Метрики рахуються лише на test-частині (80/20 split, RANDOM_STATE=42).
"""

import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 30)
pd.set_option('display.width', 250)

# ============================================================================
# Конфігурація
# ============================================================================

SCRIPT_DIR = Path(__file__).parent
INPUT_DIR = SCRIPT_DIR / 'data' / 'input'
PROFILE_FILE = INPUT_DIR / 'profile_pobut_daily_result.csv'

MIN_DAYS = 90
OUTLIER_QUANTILE = 0.995
RANDOM_STATE = 42
TEST_FRACTION = 0.2
SUMMER_MONTHS = {5, 6, 7, 8, 9}
HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}

print("=" * 80)
print("ПОРІВНЯННЯ ЛІТНІХ МОДЕЛЕЙ: mean vs residents-regression")
print("=" * 80)

# ============================================================================
# Блок 1: Завантаження даних
# ============================================================================
print("\n--- Блок 1: Завантаження ---")

raw = pd.read_csv(PROFILE_FILE, sep=';', encoding='utf-8')
print(f"Рядків: {len(raw)}")

meta_cols = ['account_id', 'gas_off', 'alternative', 'dacha',
             'appliance_group', 'profile_no', 'heated_area',
             'total_area', 'residents', 'residents_all', 'device_serial']
raw.columns = meta_cols + list(raw.columns[len(meta_cols):])

raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
raw['total_area']  = pd.to_numeric(raw['total_area'].astype(str).str.strip(), errors='coerce')
raw['residents']   = pd.to_numeric(raw['residents'], errors='coerce')

date_cols = [c for c in raw.columns if re.match(r'\d{2}\.\d{2}\.\d{4}$', str(c))]
print(f"Дат: {len(date_cols)}, діапазон: {date_cols[0]} – {date_cols[-1]}")

df = raw.melt(id_vars=meta_cols, value_vars=date_cols, var_name='date_str', value_name='consumption')
df['date']        = pd.to_datetime(df['date_str'], format='%d.%m.%Y')
df['consumption'] = pd.to_numeric(df['consumption'], errors='coerce')
df['month']       = df['date'].dt.month
df['year_month']  = df['date'].dt.to_period('M')
df['has_OP']      = df['appliance_group'].str.contains('ОП', na=False)

print(f"Записів: {len(df):,}, споживачів: {df['account_id'].nunique()}")

# ============================================================================
# Блок 2: Очищення (ідентично profile_gas_consumption.py)
# ============================================================================
print("\n--- Блок 2: Очищення ---")

df = df.dropna(subset=['consumption'])

# 2*: альтернатива / дача / відключені
flagged_ids = set(raw.loc[
    raw['gas_off'].notna() | raw['alternative'].notna() | raw['dacha'].notna(),
    'account_id'
])
df = df[~df['account_id'].isin(flagged_ids)]
print(f"2*) Альтернатива/дача: {len(flagged_ids)} видалено, лишилось: {df['account_id'].nunique()}")

# 2a: мінімум днів
days_per = df.groupby('account_id')['date'].nunique()
valid = days_per[days_per >= MIN_DAYS].index
before = df['account_id'].nunique()
df = df[df['account_id'].isin(valid)]
print(f"2a) <{MIN_DAYS} днів: {before - df['account_id'].nunique()} видалено, лишилось: {df['account_id'].nunique()}")

# 2b: нульові споживачі (>50% нулів)
zero_stats = df.groupby('account_id').agg(
    total_days=('consumption', 'count'),
    zero_days=('consumption', lambda x: (x == 0).sum()),
)
zero_stats['zero_pct'] = zero_stats['zero_days'] / zero_stats['total_days']
zero_ids = set(zero_stats[zero_stats['zero_pct'] > 0.50].index)
before = df['account_id'].nunique()
df = df[~df['account_id'].isin(zero_ids)]
print(f"2b) >50% нулів: {len(zero_ids)} видалено, лишилось: {df['account_id'].nunique()}")

# 2c: спайки (>5x rolling median 14d)
df = df.sort_values(['account_id', 'date'])
df['rolling_median'] = df.groupby('account_id')['consumption'].transform(
    lambda x: x.rolling(14, min_periods=7, center=True).median()
)
df['is_spike'] = (df['consumption'] > df['rolling_median'] * 5) & (df['rolling_median'] > 0.1)

# 2d: серії 7+ нулів підряд
def mark_zero_runs(group):
    cons = group['consumption'].values
    is_zero = (cons == 0)
    bad = np.zeros(len(cons), dtype=bool)
    run_start, run_len = None, 0
    for i in range(len(cons)):
        if is_zero[i]:
            if run_start is None:
                run_start = i
            run_len += 1
        else:
            if run_len >= 7:
                bad[run_start:run_start + run_len] = True
            run_start, run_len = None, 0
    if run_len >= 7 and run_start is not None:
        bad[run_start:run_start + run_len] = True
    return pd.Series(bad, index=group.index)

df['is_zero_run'] = df.groupby('account_id', group_keys=False).apply(mark_zero_runs)
df['is_bad'] = df['is_spike'] | df['is_zero_run']
df_clean = df[~df['is_bad']].copy()
print(f"2c/d) Аномальних днів видалено: {df['is_bad'].sum():,}")

# Перевірка мінімуму днів після очищення
clean_days = df_clean.groupby('account_id')['date'].nunique()
still_valid = clean_days[clean_days >= MIN_DAYS].index
before = df_clean['account_id'].nunique()
df_clean = df_clean[df_clean['account_id'].isin(still_valid)]
print(f"    Після очищення <{MIN_DAYS} днів: {before - df_clean['account_id'].nunique()} видалено")

# 2f: квантильні викиди по групах
df_clean['is_outlier'] = False
for grp in df_clean['appliance_group'].unique():
    mask = df_clean['appliance_group'] == grp
    thr  = df_clean.loc[mask, 'consumption'].quantile(OUTLIER_QUANTILE)
    df_clean.loc[mask & (df_clean['consumption'] > thr), 'is_outlier'] = True
df_clean = df_clean[~df_clean['is_outlier']].copy()

df_clean = df_clean.drop(columns=['rolling_median', 'is_spike', 'is_zero_run', 'is_bad', 'is_outlier'],
                          errors='ignore')
print(f"=== Після очищення: {df_clean['account_id'].nunique()} споживачів ===")

# ============================================================================
# Блок 3: Місячна агрегація
# ============================================================================
print("\n--- Блок 3: Місячна агрегація ---")

consumer_meta = raw[['account_id', 'appliance_group', 'heated_area',
                      'total_area', 'residents']].copy()

monthly = df_clean.groupby(['account_id', 'year_month']).agg(
    consumption_sum=('consumption', 'sum'),
    days_count=('consumption', 'count'),
).reset_index()
monthly = monthly.merge(consumer_meta, on='account_id', how='left')

monthly['month_num']      = monthly['year_month'].dt.month
monthly['days_in_month']  = monthly['year_month'].apply(lambda p: p.days_in_month)
monthly['consumption_norm'] = (
    monthly['consumption_sum'] / monthly['days_count'] * monthly['days_in_month']
)
monthly['has_OP']         = monthly['appliance_group'].str.contains('ОП', na=False)
monthly['is_heating']     = monthly['month_num'].isin(HEATING_MONTHS)

monthly = monthly[monthly['days_count'] >= 20].copy()
print(f"Записів: {len(monthly):,}, споживачів: {monthly['account_id'].nunique()}")

# ============================================================================
# Блок 4: Train / Test split (80/20, RANDOM_STATE=42)
# ============================================================================
print("\n--- Блок 4: Train/Test split ---")

all_consumers = monthly['account_id'].unique()
np.random.seed(RANDOM_STATE)
test_mask  = np.random.rand(len(all_consumers)) < TEST_FRACTION
test_ids   = set(all_consumers[test_mask])
train_ids  = set(all_consumers[~test_mask])
print(f"Train: {len(train_ids)}, Test: {len(test_ids)}")

train = monthly[monthly['account_id'].isin(train_ids)].copy()
test  = monthly[monthly['account_id'].isin(test_ids)].copy()

# Тільки ОП-групи, тільки літні місяці
op_groups = monthly[monthly['has_OP']]['appliance_group'].unique()
print(f"ОП-групи: {sorted(op_groups)}")

# ============================================================================
# Блок 5: Навчання моделей
# ============================================================================
print("\n--- Блок 5: Навчання ---")

# Модель A: mean(train, group, month)
model_a = {}  # (grp, month) -> mean value
for grp in op_groups:
    for m in SUMMER_MONTHS:
        mask = (
            (train['appliance_group'] == grp) &
            (train['month_num'] == m) &
            ~train['is_heating']
        )
        data = train.loc[mask, 'consumption_norm'].dropna()
        if len(data) > 0:
            model_a[(grp, m)] = data.mean()

# Модель B: coef_residents * residents (fit_intercept=False)
# Навчаємо по (grp, month) на train; residents має бути > 0
model_b = {}  # (grp, month) -> coef_residents
for grp in op_groups:
    for m in SUMMER_MONTHS:
        mask = (
            (train['appliance_group'] == grp) &
            (train['month_num'] == m) &
            ~train['is_heating']
        )
        data = train.loc[mask].dropna(subset=['consumption_norm', 'residents'])
        data = data[data['residents'] > 0]
        if len(data) < 5:
            continue
        X = data[['residents']].values
        y = data['consumption_norm'].values
        reg = LinearRegression(fit_intercept=False)
        reg.fit(X, y)
        model_b[(grp, m)] = max(0.0, reg.coef_[0])

print("Модель A ключі:", len(model_a))
print("Модель B ключі:", len(model_b))
for k, v in sorted(model_b.items()):
    print(f"  B({k[0]}, м{k[1]}): coef_res = {v:.4f}")

# ============================================================================
# Блок 6: Оцінка на test
# ============================================================================
print("\n--- Блок 6: Оцінка на test ---")

# Фільтруємо test: тільки ОП, тільки літо
test_op_summer = test[
    test['has_OP'] & ~test['is_heating']
].dropna(subset=['consumption_norm']).copy()
test_op_summer = test_op_summer[test_op_summer['consumption_norm'] > 0]

# Додаємо предикти
test_op_summer['pred_A'] = test_op_summer.apply(
    lambda r: model_a.get((r['appliance_group'], r['month_num'])), axis=1
)
test_op_summer['pred_B'] = test_op_summer.apply(
    lambda r: (model_b.get((r['appliance_group'], r['month_num']), np.nan) * r['residents'])
              if pd.notna(r['residents']) and r['residents'] > 0
              else np.nan,
    axis=1
)

# Тільки рядки де обидві моделі можуть передбачити
valid = test_op_summer.dropna(subset=['pred_A', 'pred_B']).copy()
valid = valid[(valid['pred_A'] > 0) & (valid['pred_B'] > 0)]

print(f"Test записів (ОП, літо): {len(test_op_summer):,}")
print(f"Валідних для порівняння: {len(valid):,}")

# ============================================================================
# Блок 7: Таблиця по групах та місяцях
# ============================================================================

def metrics(actual, pred_a, pred_b):
    """Повертає (n, mae_a, mae_b, mape_a, mape_b, r2_a, r2_b)."""
    n = len(actual)
    if n == 0:
        return n, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan
    mae_a = mean_absolute_error(actual, pred_a)
    mae_b = mean_absolute_error(actual, pred_b)
    nz = actual > 0.1
    mape_a = np.mean(np.abs((actual[nz] - pred_a[nz]) / actual[nz])) * 100 if nz.sum() > 0 else np.nan
    mape_b = np.mean(np.abs((actual[nz] - pred_b[nz]) / actual[nz])) * 100 if nz.sum() > 0 else np.nan
    r2_a = r2_score(actual, pred_a) if n > 1 else np.nan
    r2_b = r2_score(actual, pred_b) if n > 1 else np.nan
    return n, mae_a, mae_b, mape_a, mape_b, r2_a, r2_b


print("\n" + "=" * 100)
print("ДЕТАЛІ ПО ГРУПАХ ТА МІСЯЦЯХ")
print("=" * 100)
hdr = (f"{'Група':<18} {'Міс':>4} {'N':>6}  "
       f"{'MAE_A':>8} {'MAE_B':>8}  "
       f"{'MAPE_A':>7} {'MAPE_B':>7}  "
       f"{'R2_A':>7} {'R2_B':>7}  {'Краще':>6}")
print(hdr)
print("-" * 100)

detail_rows = []
for grp in sorted(valid['appliance_group'].unique()):
    for m in sorted(SUMMER_MONTHS):
        mask = (valid['appliance_group'] == grp) & (valid['month_num'] == m)
        sub  = valid[mask]
        if len(sub) == 0:
            continue
        act  = sub['consumption_norm'].values
        pa   = sub['pred_A'].values
        pb   = sub['pred_B'].values
        n, mae_a, mae_b, mape_a, mape_b, r2_a, r2_b = metrics(act, pa, pb)
        winner = 'B' if mae_b < mae_a else 'A'
        detail_rows.append(dict(
            group=grp, month=m, n=n,
            mae_a=mae_a, mae_b=mae_b,
            mape_a=mape_a, mape_b=mape_b,
            r2_a=r2_a, r2_b=r2_b, winner=winner
        ))
        print(f"{grp:<18} {m:>4} {n:>6}  "
              f"{mae_a:>8.2f} {mae_b:>8.2f}  "
              f"{mape_a:>6.1f}% {mape_b:>6.1f}%  "
              f"{r2_a:>7.3f} {r2_b:>7.3f}  {winner:>6}")

# ============================================================================
# Блок 8: Підсумкова таблиця по групах (всі літні місяці разом)
# ============================================================================
print("\n" + "=" * 100)
print("ПІДСУМОК ПО ГРУПАХ (всі літні місяці 5-9)")
print("=" * 100)
hdr2 = (f"{'Група':<18} {'N':>6}  "
        f"{'MAE_A':>8} {'MAE_B':>8}  "
        f"{'MAPE_A':>7} {'MAPE_B':>7}  "
        f"{'R2_A':>7} {'R2_B':>7}")
print(hdr2)
print("-" * 100)

summary_rows = []
for grp in sorted(valid['appliance_group'].unique()):
    sub  = valid[valid['appliance_group'] == grp]
    act  = sub['consumption_norm'].values
    pa   = sub['pred_A'].values
    pb   = sub['pred_B'].values
    n, mae_a, mae_b, mape_a, mape_b, r2_a, r2_b = metrics(act, pa, pb)
    summary_rows.append(dict(
        group=grp, n=n,
        mae_a=mae_a, mae_b=mae_b,
        mape_a=mape_a, mape_b=mape_b,
        r2_a=r2_a, r2_b=r2_b,
    ))
    print(f"{grp:<18} {n:>6}  "
          f"{mae_a:>8.2f} {mae_b:>8.2f}  "
          f"{mape_a:>6.1f}% {mape_b:>6.1f}%  "
          f"{r2_a:>7.3f} {r2_b:>7.3f}")

# Загальний рядок
act  = valid['consumption_norm'].values
pa   = valid['pred_A'].values
pb   = valid['pred_B'].values
n_t, mae_a_t, mae_b_t, mape_a_t, mape_b_t, r2_a_t, r2_b_t = metrics(act, pa, pb)
print("-" * 100)
print(f"{'ВСЬОГО':<18} {n_t:>6}  "
      f"{mae_a_t:>8.2f} {mae_b_t:>8.2f}  "
      f"{mape_a_t:>6.1f}% {mape_b_t:>6.1f}%  "
      f"{r2_a_t:>7.3f} {r2_b_t:>7.3f}")

# ============================================================================
# Блок 9: Висновок
# ============================================================================
print("\n" + "=" * 80)
print("ВИСНОВОК")
print("=" * 80)

b_wins = sum(1 for r in detail_rows if r['winner'] == 'B')
a_wins = sum(1 for r in detail_rows if r['winner'] == 'A')
total  = len(detail_rows)

print(f"Модель A (mean) краща: {a_wins}/{total} комбінацій (group x month)")
print(f"Модель B (residents): {b_wins}/{total} комбінацій")
print()

if mae_b_t < mae_a_t:
    diff_pct = (mae_a_t - mae_b_t) / mae_a_t * 100
    print(f">>> Модель B КРАЩА загалом: MAE {mae_b_t:.2f} vs {mae_a_t:.2f} "
          f"(покращення {diff_pct:.1f}%)")
    print("    Рекомендація: впровадити residents-регресію в profile_gas_consumption.py")
else:
    diff_pct = (mae_b_t - mae_a_t) / mae_b_t * 100
    print(f">>> Модель A КРАЩА загалом: MAE {mae_a_t:.2f} vs {mae_b_t:.2f} "
          f"(перевага {diff_pct:.1f}%)")
    print("    Рекомендація: залишити поточне середнє (mean) для літніх ОП-груп")

print()
print("Перевірте також MAPE та R² для повної картини.")
