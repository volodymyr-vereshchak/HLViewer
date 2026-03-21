"""
Порівняння покращень літнього прогнозу:

  A (baseline):  mean(train, group, month)
  B (median):    median(train, group, month)
  C (May hybrid): травень = 0.4*winter_pred_scaled + 0.6*summer_median
                  Jun-Sep = median(train, group, month)
  D (winter*scale): prediction = (coef_area*area + coef_res*res) * scale_factor(group, month)
                    де scale_factor = summer_mean / winter_mean по train
  E (best mix):  травень = Модель C, Jun-Sep = Модель D або B (хто краще по train)

Метрики рахуються тільки на test (80/20 split, RANDOM_STATE=42).
"""

import re
import warnings
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score

warnings.filterwarnings('ignore')
pd.set_option('display.width', 250)

# ============================================================================
# Конфігурація
# ============================================================================
SCRIPT_DIR = Path(__file__).parent
INPUT_DIR  = SCRIPT_DIR / 'data' / 'input'
PROFILE_FILE = INPUT_DIR / 'profile_pobut_daily_result.csv'

MIN_DAYS        = 90
OUTLIER_QUANTILE = 0.995
RANDOM_STATE    = 42
TEST_FRACTION   = 0.2
SUMMER_MONTHS   = {5, 6, 7, 8, 9}
HEATING_MONTHS  = {1, 2, 3, 4, 10, 11, 12}
MAY_WINTER_WEIGHT = 0.4   # частка зимового предикту у травні
MAY_SUMMER_WEIGHT = 0.6   # частка літнього медіани у травні

print("=" * 80)
print("ПОКРАЩЕННЯ ЛІТНЬОГО ПРОГНОЗУ: A vs B vs C vs D")
print("=" * 80)

# ============================================================================
# Блок 1: Завантаження
# ============================================================================
print("\n--- 1. Завантаження ---")
raw = pd.read_csv(PROFILE_FILE, sep=';', encoding='utf-8')
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

# ============================================================================
# Блок 2: Очищення (ідентично profile_gas_consumption.py)
# ============================================================================
print("\n--- 2. Очищення ---")
df = df.dropna(subset=['consumption'])
flagged_ids = set(raw.loc[
    raw['gas_off'].notna() | raw['alternative'].notna() | raw['dacha'].notna(),
    'account_id'
])
df = df[~df['account_id'].isin(flagged_ids)]

days_per = df.groupby('account_id')['date'].nunique()
df = df[df['account_id'].isin(days_per[days_per >= MIN_DAYS].index)]

zero_stats = df.groupby('account_id').agg(
    total_days=('consumption', 'count'),
    zero_days=('consumption', lambda x: (x == 0).sum()),
)
zero_stats['zero_pct'] = zero_stats['zero_days'] / zero_stats['total_days']
df = df[~df['account_id'].isin(zero_stats[zero_stats['zero_pct'] > 0.50].index)]

df = df.sort_values(['account_id', 'date'])
df['rolling_median'] = df.groupby('account_id')['consumption'].transform(
    lambda x: x.rolling(14, min_periods=7, center=True).median()
)
df['is_spike'] = (df['consumption'] > df['rolling_median'] * 5) & (df['rolling_median'] > 0.1)

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
df_clean = df[~(df['is_spike'] | df['is_zero_run'])].copy()

clean_days = df_clean.groupby('account_id')['date'].nunique()
df_clean = df_clean[df_clean['account_id'].isin(clean_days[clean_days >= MIN_DAYS].index)]

df_clean['is_outlier'] = False
for grp in df_clean['appliance_group'].unique():
    mask = df_clean['appliance_group'] == grp
    thr  = df_clean.loc[mask, 'consumption'].quantile(OUTLIER_QUANTILE)
    df_clean.loc[mask & (df_clean['consumption'] > thr), 'is_outlier'] = True
df_clean = df_clean[~df_clean['is_outlier']].copy()
df_clean = df_clean.drop(columns=['rolling_median','is_spike','is_zero_run','is_outlier'], errors='ignore')
print(f"Після очищення: {df_clean['account_id'].nunique()} споживачів")

# ============================================================================
# Блок 3: Місячна агрегація
# ============================================================================
print("\n--- 3. Місячна агрегація ---")
consumer_meta = raw[['account_id', 'appliance_group', 'heated_area',
                      'total_area', 'residents']].copy()

monthly = df_clean.groupby(['account_id', 'year_month']).agg(
    consumption_sum=('consumption', 'sum'),
    days_count=('consumption', 'count'),
).reset_index()
monthly = monthly.merge(consumer_meta, on='account_id', how='left')
monthly['month_num']       = monthly['year_month'].dt.month
monthly['days_in_month']   = monthly['year_month'].apply(lambda p: p.days_in_month)
monthly['consumption_norm'] = monthly['consumption_sum'] / monthly['days_count'] * monthly['days_in_month']
monthly['has_OP']          = monthly['appliance_group'].str.contains('ОП', na=False)
monthly['is_heating']      = monthly['month_num'].isin(HEATING_MONTHS)
monthly['is_summer']       = monthly['month_num'].isin(SUMMER_MONTHS)
monthly = monthly[monthly['days_count'] >= 20].copy()
print(f"Записів: {len(monthly):,}, споживачів: {monthly['account_id'].nunique()}")

# ============================================================================
# Блок 4: Train / Test split
# ============================================================================
print("\n--- 4. Train/Test split (80/20, seed=42) ---")
all_consumers = monthly['account_id'].unique()
np.random.seed(RANDOM_STATE)
test_mask = np.random.rand(len(all_consumers)) < TEST_FRACTION
test_ids  = set(all_consumers[test_mask])
train_ids = set(all_consumers[~test_mask])
train = monthly[monthly['account_id'].isin(train_ids)].copy()
test  = monthly[monthly['account_id'].isin(test_ids)].copy()
print(f"Train: {len(train_ids)} | Test: {len(test_ids)}")

op_groups = sorted(monthly[monthly['has_OP']]['appliance_group'].unique())
print(f"ОП-групи: {op_groups}")

# ============================================================================
# Блок 5: Навчання моделей A та B (mean / median)
# ============================================================================
print("\n--- 5. Навчання A (mean) і B (median) ---")

model_a = {}   # (grp, month) -> mean
model_b = {}   # (grp, month) -> median

for grp in op_groups:
    for m in SUMMER_MONTHS:
        mask = (train['appliance_group'] == grp) & (train['month_num'] == m) & train['is_summer']
        data = train.loc[mask, 'consumption_norm'].dropna()
        if len(data) > 0:
            model_a[(grp, m)] = data.mean()
            model_b[(grp, m)] = data.median()

# ============================================================================
# Блок 6: Навчання зимової регресії для моделей C і D
# ============================================================================
print("\n--- 6. Зимова регресія (для C і D) ---")

# Зимові моделі по (grp, year_month)
winter_models = {}  # (grp, ym) -> (coef_area, coef_res)
for grp in op_groups:
    gd = train[(train['appliance_group'] == grp) & train['is_heating']]
    gd = gd.dropna(subset=['consumption_norm', 'residents'])
    gd = gd[(gd['heated_area'] > 0) & (gd['residents'] > 0)]
    for ym in sorted(gd['year_month'].unique()):
        yd = gd[gd['year_month'] == ym]
        if len(yd) < 10:
            continue
        X = yd[['heated_area', 'residents']].values
        y = yd['consumption_norm'].values
        reg = LinearRegression(fit_intercept=False)
        reg.fit(X, y)
        winter_models[(grp, ym)] = (max(0.0, reg.coef_[0]), max(0.0, reg.coef_[1]))

# Середні зимові коефіцієнти по групі (агрегуємо всі зимові місяці)
avg_winter_coef = {}
for grp in op_groups:
    vals = [(ca, cr) for (g, ym), (ca, cr) in winter_models.items() if g == grp]
    if vals:
        avg_winter_coef[grp] = (
            np.mean([v[0] for v in vals]),
            np.mean([v[1] for v in vals]),
        )
        print(f"  {grp}: avg coef_area={avg_winter_coef[grp][0]:.4f}  avg coef_res={avg_winter_coef[grp][1]:.2f}")

# Scale factor: summer_mean(grp, month) / winter_mean(grp) на train
scale_d = {}  # (grp, month) -> scale
for grp in op_groups:
    w_mean = train[(train['appliance_group'] == grp) & train['is_heating']]['consumption_norm'].mean()
    if w_mean <= 0:
        continue
    for m in SUMMER_MONTHS:
        s_data = train[(train['appliance_group'] == grp) & (train['month_num'] == m) & train['is_summer']]['consumption_norm']
        if len(s_data) > 0:
            scale_d[(grp, m)] = s_data.mean() / w_mean

# May scale factor: зважений (більш зимовий для травня)
# Для моделі C: scale для "зимового компоненту" у травні
# Обираємо scale травня з scale_d, але тільки частину
scale_may_winter = {}   # grp -> scale (winter pred -> may level)
for grp in op_groups:
    scale_may_winter[grp] = scale_d.get((grp, 5), 0)

print(f"\nScale factors (summer/winter) per group per month:")
for grp in op_groups:
    vals = "  ".join(f"м{m}={scale_d.get((grp,m),0):.3f}" for m in sorted(SUMMER_MONTHS))
    print(f"  {grp}: {vals}")

# ============================================================================
# Блок 7: Функції предикту
# ============================================================================

def pred_A(grp, m, area, res):
    """Baseline: mean(group, month)."""
    return model_a.get((grp, m), np.nan)

def pred_B(grp, m, area, res):
    """Median(group, month) — стійкіша до викидів."""
    return model_b.get((grp, m), np.nan)

def pred_C(grp, m, area, res):
    """Травень: 0.4*winter_pred_scaled + 0.6*median. Jun-Sep: median."""
    if m != 5:
        return model_b.get((grp, m), np.nan)
    # Травень: зважена суміш
    med = model_b.get((grp, 5), np.nan)
    if grp not in avg_winter_coef or pd.isna(area) or pd.isna(res) or area <= 0 or res <= 0:
        return med
    ca, cr = avg_winter_coef[grp]
    scale = scale_may_winter.get(grp, 0)
    winter_part = max(0, (ca * area + cr * res) * scale)
    if np.isnan(med):
        return winter_part
    return MAY_WINTER_WEIGHT * winter_part + MAY_SUMMER_WEIGHT * med

def pred_D(grp, m, area, res):
    """winter_coef * scale_factor(group, month) — кастомне масштабування."""
    if grp not in avg_winter_coef:
        return np.nan
    if pd.isna(area) or pd.isna(res) or area <= 0 or res <= 0:
        return model_b.get((grp, m), np.nan)
    ca, cr = avg_winter_coef[grp]
    scale = scale_d.get((grp, m), np.nan)
    if np.isnan(scale):
        return np.nan
    return max(0, (ca * area + cr * res) * scale)

# ============================================================================
# Блок 8: Оцінка на test
# ============================================================================
print("\n--- 8. Оцінка на test (ОП-групи, літо) ---")

test_op = test[test['has_OP'] & test['is_summer']].dropna(subset=['consumption_norm']).copy()
test_op = test_op[test_op['consumption_norm'] > 0].copy()
print(f"Test записів: {len(test_op):,}  споживачів: {test_op['account_id'].nunique()}")

# Розрахунок предиктів
for col, fn in [('pA', pred_A), ('pB', pred_B), ('pC', pred_C), ('pD', pred_D)]:
    test_op[col] = test_op.apply(
        lambda r: fn(r['appliance_group'], r['month_num'], r['heated_area'], r['residents']), axis=1
    )

# ============================================================================
# Блок 9: Таблиця результатів
# ============================================================================

def metrics_row(actual, pred, prefix=''):
    n = len(actual)
    mask = ~np.isnan(pred)
    a = actual[mask]; p = pred[mask]
    if len(a) < 2:
        return {'n': n, f'{prefix}mae': np.nan, f'{prefix}mape': np.nan, f'{prefix}r2': np.nan}
    mae  = mean_absolute_error(a, p)
    nz   = a > 0.1
    mape = np.mean(np.abs((a[nz] - p[nz]) / a[nz])) * 100 if nz.sum() > 0 else np.nan
    r2   = r2_score(a, p)
    return {'n': mask.sum(), f'{prefix}mae': mae, f'{prefix}mape': mape, f'{prefix}r2': r2}

def winner_str(row):
    """Яка модель виграє по MAE?"""
    vals = {m: row[f'{m}mae'] for m in ['A','B','C','D'] if not np.isnan(row.get(f'{m}mae', np.nan))}
    if not vals:
        return '?'
    best = min(vals, key=vals.get)
    return best

print("\n" + "=" * 110)
print("ДЕТАЛІ ПО ГРУПАХ ТА МІСЯЦЯХ")
print("=" * 110)
hdr = (f"{'Група':<18} {'Міс':>4} {'N':>5}  "
       f"{'A_MAE':>7} {'B_MAE':>7} {'C_MAE':>7} {'D_MAE':>7}  "
       f"{'A_MAPE':>7} {'B_MAPE':>7} {'C_MAPE':>7} {'D_MAPE':>7}  "
       f"{'A_R2':>6} {'B_R2':>6} {'C_R2':>6} {'D_R2':>6}  W")
print(hdr)
print("-" * 110)

detail_rows = []
for grp in sorted(test_op['appliance_group'].unique()):
    for m in sorted(SUMMER_MONTHS):
        sub = test_op[(test_op['appliance_group'] == grp) & (test_op['month_num'] == m)]
        if len(sub) == 0:
            continue
        actual = sub['consumption_norm'].values
        row_data = {'grp': grp, 'month': m}
        for key, col in [('A','pA'),('B','pB'),('C','pC'),('D','pD')]:
            m_row = metrics_row(actual, sub[col].values, prefix=key+'_')
            row_data[f'{key}mae']  = m_row.get(f'{key}_mae', np.nan)
            row_data[f'{key}mape'] = m_row.get(f'{key}_mape', np.nan)
            row_data[f'{key}r2']   = m_row.get(f'{key}_r2', np.nan)
            row_data['n'] = m_row['n']
        # Winner by MAE
        maes = {k: row_data[f'{k}mae'] for k in 'ABCD' if not np.isnan(row_data.get(f'{k}mae', np.nan))}
        row_data['winner'] = min(maes, key=maes.get) if maes else '?'
        detail_rows.append(row_data)

        def _f(v, fmt='.2f'):
            return f'{v:{fmt}}' if not np.isnan(v) else '  ---'
        def _fp(v):
            return f'{v:5.1f}%' if not np.isnan(v) else '   ---'
        def _fr2(v):
            return f'{v:6.3f}' if not np.isnan(v) else '   ---'

        print(f"{grp:<18} {m:>4} {row_data['n']:>5}  "
              f"{_f(row_data['Amae']):>7} {_f(row_data['Bmae']):>7} {_f(row_data['Cmae']):>7} {_f(row_data['Dmae']):>7}  "
              f"{_fp(row_data['Amape']):>7} {_fp(row_data['Bmape']):>7} {_fp(row_data['Cmape']):>7} {_fp(row_data['Dmape']):>7}  "
              f"{_fr2(row_data['Ar2']):>6} {_fr2(row_data['Br2']):>6} {_fr2(row_data['Cr2']):>6} {_fr2(row_data['Dr2']):>6}  "
              f"{row_data['winner']}")

# ============================================================================
# Блок 10: Підсумок по групах
# ============================================================================
print("\n" + "=" * 110)
print("ПІДСУМОК ПО ГРУПАХ (всі літні місяці 5-9)")
print("=" * 110)
hdr2 = (f"{'Група':<18} {'N':>5}  "
        f"{'A_MAE':>7} {'B_MAE':>7} {'C_MAE':>7} {'D_MAE':>7}  "
        f"{'A_MAPE':>7} {'B_MAPE':>7} {'C_MAPE':>7} {'D_MAPE':>7}")
print(hdr2)
print("-" * 110)

summary_rows = []
for grp in sorted(test_op['appliance_group'].unique()):
    sub = test_op[test_op['appliance_group'] == grp]
    actual = sub['consumption_norm'].values
    row_data = {'grp': grp}
    for key, col in [('A','pA'),('B','pB'),('C','pC'),('D','pD')]:
        m_row = metrics_row(actual, sub[col].values, prefix=key+'_')
        row_data[f'{key}mae']  = m_row.get(f'{key}_mae', np.nan)
        row_data[f'{key}mape'] = m_row.get(f'{key}_mape', np.nan)
        row_data[f'{key}r2']   = m_row.get(f'{key}_r2', np.nan)
        row_data['n']          = m_row['n']
    summary_rows.append(row_data)
    print(f"{grp:<18} {row_data['n']:>5}  "
          f"{row_data['Amae']:>7.2f} {row_data['Bmae']:>7.2f} {row_data['Cmae']:>7.2f} {row_data['Dmae']:>7.2f}  "
          f"{row_data['Amape']:>6.1f}% {row_data['Bmape']:>6.1f}% {row_data['Cmape']:>6.1f}% {row_data['Dmape']:>6.1f}%")

# Загальний рядок
print("-" * 110)
actual_all = test_op['consumption_norm'].values
total_row = {'grp': 'ВСЬОГО'}
for key, col in [('A','pA'),('B','pB'),('C','pC'),('D','pD')]:
    m_row = metrics_row(actual_all, test_op[col].values, prefix=key+'_')
    total_row[f'{key}mae']  = m_row.get(f'{key}_mae', np.nan)
    total_row[f'{key}mape'] = m_row.get(f'{key}_mape', np.nan)
    total_row[f'{key}r2']   = m_row.get(f'{key}_r2', np.nan)
    total_row['n']          = m_row['n']

print(f"{'ВСЬОГО':<18} {total_row['n']:>5}  "
      f"{total_row['Amae']:>7.2f} {total_row['Bmae']:>7.2f} {total_row['Cmae']:>7.2f} {total_row['Dmae']:>7.2f}  "
      f"{total_row['Amape']:>6.1f}% {total_row['Bmape']:>6.1f}% {total_row['Cmape']:>6.1f}% {total_row['Dmape']:>6.1f}%")

# ============================================================================
# Блок 11: Розбивка тільки травень vs решта
# ============================================================================
print("\n" + "=" * 80)
print("РОЗБИВКА: ТРАВЕНЬ vs ЧЕРВЕНЬ-ВЕРЕСЕНЬ")
print("=" * 80)
for season_name, season_months in [("Травень", {5}), ("Червень-Вересень", {6,7,8,9})]:
    sub = test_op[test_op['month_num'].isin(season_months)]
    if len(sub) == 0:
        continue
    actual = sub['consumption_norm'].values
    print(f"\n{season_name} (N={len(sub)}):")
    for key, col in [('A','pA'),('B','pB'),('C','pC'),('D','pD')]:
        m_row = metrics_row(actual, sub[col].values, prefix=key+'_')
        mae   = m_row.get(f'{key}_mae', np.nan)
        mape  = m_row.get(f'{key}_mape', np.nan)
        r2    = m_row.get(f'{key}_r2', np.nan)
        n_val = m_row['n']
        print(f"  Модель {key}: N={n_val:>4}  MAE={mae:>7.2f}  MAPE={mape:>6.1f}%  R2={r2:>7.3f}")

# ============================================================================
# Блок 12: Підсумок і рекомендація
# ============================================================================
print("\n" + "=" * 80)
print("ПІДСУМОК І РЕКОМЕНДАЦІЯ")
print("=" * 80)

print(f"\n{'Модель':<10}  {'Опис':<45}  {'MAE':>7}  {'MAPE':>7}  {'R2':>7}")
print("-" * 85)
descs = {
    'A': 'Baseline: mean(group, month)',
    'B': 'Median(group, month)',
    'C': 'May hybrid (0.4*winter + 0.6*median) + median rest',
    'D': 'Winter_coef * scale_factor(group, month)',
}
best_mae = min(total_row[f'{k}mae'] for k in 'ABCD' if not np.isnan(total_row[f'{k}mae']))
for key in 'ABCD':
    mae  = total_row[f'{key}mae']
    mape = total_row[f'{key}mape']
    r2   = total_row[f'{key}r2']
    marker = ' <<< КРАЩА' if abs(mae - best_mae) < 0.01 else ''
    print(f"  {key}        {descs[key]:<45}  {mae:>7.2f}  {mape:>6.1f}%  {r2:>7.3f}{marker}")

mae_a = total_row['Amae']
print(f"\nПокращення відносно A (baseline):")
for key in 'BCD':
    mae = total_row[f'{key}mae']
    delta = mae_a - mae
    pct = delta / mae_a * 100
    mape_delta = total_row['Amape'] - total_row[f'{key}mape']
    print(f"  {key}: MAE {delta:+.2f} ({pct:+.1f}%)   MAPE {mape_delta:+.1f}pp")

winner_mae = min('ABCD', key=lambda k: total_row.get(f'{k}mae', float('inf')))
print(f"\nНайкраща модель по MAE: {winner_mae} — {descs[winner_mae]}")
winner_mape = min('ABCD', key=lambda k: total_row.get(f'{k}mape', float('inf')))
print(f"Найкраща модель по MAPE: {winner_mape} — {descs[winner_mape]}")

print(f"""
Що впроваджувати в profile_gas_consumption.py:
  1. summer_op_means -> обчислювати MEDIAN замість mean  (рядок ~283)
  2. Травень -> використати гібрид C: 0.4*winter_scaled + 0.6*median
     (потребує avg_winter_coef по групі та scale_factor для травня)
""")
