"""
Holdout-тест: 100 потребителей с ОП.
Профили строятся БЕЗ них, потом тестируем на каждом отдельно и суммарно.
"""

import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import r2_score, mean_absolute_error

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 30)
pd.set_option('display.width', 250)

SCRIPT_DIR = Path(__file__).parent
INPUT_DIR = SCRIPT_DIR / 'data' / 'input'
OUTPUT_DIR = SCRIPT_DIR / 'data' / 'profiles'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PROFILE_FILE = INPUT_DIR / 'profile_pobut_daily_result.csv'
MIN_DAYS = 90
OUTLIER_QUANTILE = 0.995
RANDOM_STATE = 42
N_HOLDOUT = 100

print("=" * 80)
print("HOLDOUT TEST: 100 ОП потребителей")
print("=" * 80)

# ============================================================================
# Блок 1: Загрузка данных (копия из основного скрипта)
# ============================================================================
print("\nБлок 1: Загрузка...")

raw = pd.read_csv(PROFILE_FILE, sep=';', encoding='utf-8')
meta_cols = ['account_id', 'appliance_group', 'profile_no', 'heated_area',
             'total_area', 'residents', 'residents_all', 'device_serial']
raw.columns = meta_cols + list(raw.columns[len(meta_cols):])

raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
raw['total_area'] = pd.to_numeric(raw['total_area'].astype(str).str.strip(), errors='coerce')
raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce')
raw['residents_all'] = pd.to_numeric(raw['residents_all'], errors='coerce')

date_cols = [c for c in raw.columns if re.match(r'\d{2}\.\d{2}\.\d{4}$', str(c))]
df = raw.melt(id_vars=meta_cols, value_vars=date_cols, var_name='date_str', value_name='consumption')
df['date'] = pd.to_datetime(df['date_str'], format='%d.%m.%Y')
df['consumption'] = pd.to_numeric(df['consumption'], errors='coerce')
df['month'] = df['date'].dt.month
df['year'] = df['date'].dt.year
df['year_month'] = df['date'].dt.to_period('M')

df['has_PG'] = df['appliance_group'].str.contains(r'(?<!В)ПГ', regex=True, na=False)
df['has_VPG'] = df['appliance_group'].str.contains('ВПГ', na=False)
df['has_OP'] = df['appliance_group'].str.contains('ОП', na=False)

print(f"  Записей: {len(df):,}, потребителей: {df['account_id'].nunique()}")

# ============================================================================
# Блок 2: Очистка (идентично основному скрипту)
# ============================================================================
print("\nБлок 2: Очистка...")

df = df.dropna(subset=['consumption'])

days_per_consumer = df.groupby('account_id')['date'].nunique()
valid_consumers = days_per_consumer[days_per_consumer >= MIN_DAYS].index
df = df[df['account_id'].isin(valid_consumers)]

zero_stats = df.groupby('account_id').agg(
    total_days=('consumption', 'count'),
    zero_days=('consumption', lambda x: (x == 0).sum()),
)
zero_stats['zero_pct'] = zero_stats['zero_days'] / zero_stats['total_days']
zero_consumers = set(zero_stats[zero_stats['zero_pct'] > 0.50].index)
df = df[~df['account_id'].isin(zero_consumers)]

df = df.sort_values(['account_id', 'date'])
df['rolling_median'] = df.groupby('account_id')['consumption'].transform(
    lambda x: x.rolling(14, min_periods=7, center=True).median()
)
df['is_spike'] = (df['consumption'] > df['rolling_median'] * 5) & (df['rolling_median'] > 0.1)


def mark_zero_runs(group):
    cons = group['consumption'].values
    is_zero = (cons == 0)
    bad = np.zeros(len(cons), dtype=bool)
    run_start = None
    run_len = 0
    for i in range(len(cons)):
        if is_zero[i]:
            if run_start is None:
                run_start = i
            run_len += 1
        else:
            if run_len >= 7:
                bad[run_start:run_start + run_len] = True
            run_start = None
            run_len = 0
    if run_len >= 7 and run_start is not None:
        bad[run_start:run_start + run_len] = True
    return pd.Series(bad, index=group.index)


df['is_zero_run'] = df.groupby('account_id', group_keys=False).apply(mark_zero_runs)
df['is_bad'] = df['is_spike'] | df['is_zero_run']
df_clean = df[~df['is_bad']].copy()

clean_days = df_clean.groupby('account_id')['date'].nunique()
still_valid = clean_days[clean_days >= MIN_DAYS].index
df_clean = df_clean[df_clean['account_id'].isin(still_valid)]

df_clean['is_outlier'] = False
for grp in df_clean['appliance_group'].unique():
    mask = df_clean['appliance_group'] == grp
    threshold = df_clean.loc[mask, 'consumption'].quantile(OUTLIER_QUANTILE)
    outlier_mask = mask & (df_clean['consumption'] > threshold)
    df_clean.loc[outlier_mask, 'is_outlier'] = True
df_clean = df_clean[~df_clean['is_outlier']].copy()
df_clean = df_clean.drop(columns=['rolling_median', 'is_spike', 'is_zero_run', 'is_bad', 'is_outlier'],
                          errors='ignore')

print(f"  После очистки: {len(df_clean):,} записей, {df_clean['account_id'].nunique()} потребителей")

# ============================================================================
# Блок 3: Месячная агрегация
# ============================================================================
print("\nБлок 3: Месячная агрегация...")

monthly = df_clean.groupby(['account_id', 'year_month']).agg(
    consumption_sum=('consumption', 'sum'),
    days_count=('consumption', 'count'),
).reset_index()

consumer_meta = raw[['account_id', 'appliance_group', 'heated_area', 'total_area',
                     'residents', 'residents_all']].copy()
monthly = monthly.merge(consumer_meta, on='account_id', how='left')

monthly['has_PG'] = monthly['appliance_group'].str.contains(r'(?<!В)ПГ', regex=True, na=False)
monthly['has_VPG'] = monthly['appliance_group'].str.contains('ВПГ', na=False)
monthly['has_OP'] = monthly['appliance_group'].str.contains('ОП', na=False)

monthly['month_num'] = monthly['year_month'].dt.month
monthly['year_num'] = monthly['year_month'].dt.year
monthly['days_in_month'] = monthly['year_month'].apply(lambda p: p.days_in_month)
monthly['consumption_norm'] = monthly['consumption_sum'] / monthly['days_count'] * monthly['days_in_month']
monthly = monthly[monthly['days_count'] >= 20].copy()

print(f"  {len(monthly):,} записей, {monthly['account_id'].nunique()} потребителей")

# ============================================================================
# Блок 4: Выбор 100 holdout потребителей с ОП
# ============================================================================
print("\n" + "=" * 80)
print("Блок 4: Выбор holdout")
print("=" * 80)

op_consumers = monthly[monthly['has_OP']]['account_id'].unique()
# Берем только тех, у кого есть данные за >= 6 месяцев (для надежной оценки)
months_per_op = monthly[monthly['account_id'].isin(op_consumers)].groupby('account_id')['year_month'].nunique()
op_with_enough = months_per_op[months_per_op >= 6].index.values

print(f"  ОП потребителей всего: {len(op_consumers)}")
print(f"  С >= 6 месяцами данных: {len(op_with_enough)}")

np.random.seed(RANDOM_STATE)
holdout_ids = set(np.random.choice(op_with_enough, size=min(N_HOLDOUT, len(op_with_enough)), replace=False))
train_ids = set(monthly['account_id'].unique()) - holdout_ids

print(f"  Holdout: {len(holdout_ids)}, Train: {len(train_ids)}")

# Распределение holdout по группам
holdout_groups = monthly[monthly['account_id'].isin(holdout_ids)].groupby('appliance_group')['account_id'].nunique()
print(f"\n  Holdout по группам:")
for grp, cnt in holdout_groups.items():
    print(f"    {grp}: {cnt}")

train_monthly = monthly[monthly['account_id'].isin(train_ids)].copy()
holdout_monthly = monthly[monthly['account_id'].isin(holdout_ids)].copy()

# ============================================================================
# Блок 5: Построение профилей на train
# ============================================================================
print("\n" + "=" * 80)
print("Блок 5: Профили на train")
print("=" * 80)

# --- ПГ медианы ---
tr_pg = train_monthly[train_monthly['appliance_group'] == 'ПГ'].dropna(subset=['residents', 'consumption_norm'])
tr_pg = tr_pg[tr_pg['residents'] > 0]
tr_pg_med = tr_pg.groupby(['residents', 'month_num'])['consumption_norm'].median()
tr_pg_by_res = tr_pg.groupby('residents')['consumption_norm'].median()
tr_pg_global = tr_pg['consumption_norm'].sum() / tr_pg['residents'].sum()
print(f"  ПГ: {tr_pg['account_id'].nunique()} потребителей")

# --- ВПГ медианы ---
tr_pgvpg = train_monthly[train_monthly['appliance_group'] == 'ПГ,ВПГ'].dropna(subset=['residents', 'consumption_norm'])
tr_pgvpg = tr_pgvpg[tr_pgvpg['residents'] > 0].copy()
tr_pgvpg['pg_part'] = tr_pgvpg.apply(
    lambda r: tr_pg_med.get((r['residents'], r['month_num']),
              tr_pg_by_res.get(r['residents'], tr_pg_global * r['residents'])), axis=1)
tr_pgvpg['vpg_part'] = (tr_pgvpg['consumption_norm'] - tr_pgvpg['pg_part']).clip(lower=0)
tr_vpg_med = tr_pgvpg.groupby(['residents', 'month_num'])['vpg_part'].median()
tr_vpg_by_res = tr_pgvpg.groupby('residents')['vpg_part'].median()
tr_vpg_global = tr_pgvpg['vpg_part'].sum() / tr_pgvpg['residents'].sum()
tr_pgvpg_med = tr_pgvpg.groupby(['residents', 'month_num'])['consumption_norm'].median()
print(f"  ПГ,ВПГ: {tr_pgvpg['account_id'].nunique()} потребителей")


def tr_get_pg(res, month):
    v = tr_pg_med.get((res, month))
    if v is not None: return v
    v2 = tr_pg_by_res.get(res)
    if v2 is not None: return v2
    return tr_pg_global * res


def tr_get_vpg(res, month):
    v = tr_vpg_med.get((res, month))
    if v is not None: return v
    v2 = tr_vpg_by_res.get(res)
    if v2 is not None: return v2
    return tr_vpg_global * res


# --- ОП регрессии по month ---
train_op_models = {}
for grp in sorted(train_monthly['appliance_group'].unique()):
    if 'ОП' not in grp:
        continue
    gd = train_monthly[train_monthly['appliance_group'] == grp]
    gd = gd.dropna(subset=['consumption_norm', 'residents'])
    gd = gd[(gd['heated_area'] > 0) & (gd['residents'] > 0)]
    n_c = gd['account_id'].nunique()
    print(f"  {grp}: {n_c} потребителей для регрессии")

    for ym in sorted(gd['year_month'].unique()):
        yd = gd[gd['year_month'] == ym]
        if len(yd) < 10:
            continue
        X = yd[['heated_area', 'residents']].values
        y = yd['consumption_norm'].values
        model = LinearRegression(fit_intercept=False)
        model.fit(X, y)
        train_op_models[(grp, ym)] = (max(0, model.coef_[0]), max(0, model.coef_[1]))

print(f"  ОП моделей (группа, месяц): {len(train_op_models)}")

# ============================================================================
# Блок 6: Предсказание для holdout
# ============================================================================
print("\n" + "=" * 80)
print("Блок 6: Предсказание holdout")
print("=" * 80)


def predict_row(row):
    grp = row['appliance_group']
    res = row['residents']
    month = row['month_num']
    ym = row['year_month']

    if pd.isna(res) or res <= 0:
        res = 2

    has_op = 'ОП' in str(grp)
    has_pg = bool(re.search(r'(?<!В)ПГ', str(grp)))
    has_vpg = 'ВПГ' in str(grp)

    if has_op:
        key = (grp, ym)
        if key not in train_op_models:
            return np.nan
        coef_a, coef_r = train_op_models[key]
        area = row['heated_area']
        if pd.isna(area) or area <= 0:
            area = row.get('total_area', 50)
            if pd.isna(area) or area <= 0:
                area = 50
        return max(0, coef_a * area + coef_r * res)
    elif grp == 'ПГ,ВПГ':
        v = tr_pgvpg_med.get((res, month))
        if v is not None: return v
        return tr_get_pg(res, month) + tr_get_vpg(res, month)
    elif grp == 'ПГ':
        return tr_get_pg(res, month)
    else:
        return np.nan


holdout_monthly['predicted'] = holdout_monthly.apply(predict_row, axis=1)
hv = holdout_monthly.dropna(subset=['consumption_norm', 'predicted']).copy()
hv = hv[hv['predicted'] > 0]

print(f"  Holdout записей с предсказанием: {len(hv)}")
print(f"  Holdout потребителей: {hv['account_id'].nunique()}")

# ============================================================================
# Блок 7: R2 для каждого потребителя отдельно
# ============================================================================
print("\n" + "=" * 80)
print("Блок 7: R2 по каждому потребителю")
print("=" * 80)

consumer_results = []
for acc_id in sorted(hv['account_id'].unique()):
    cd = hv[hv['account_id'] == acc_id]
    actual = cd['consumption_norm'].values
    predicted = cd['predicted'].values
    grp = cd['appliance_group'].iloc[0]
    area = cd['heated_area'].iloc[0]
    res = cd['residents'].iloc[0]
    n_months = len(cd)

    if n_months < 3:
        r2 = np.nan
    else:
        r2 = r2_score(actual, predicted)

    mape = np.mean(np.abs((actual - predicted) / np.maximum(actual, 0.1))) * 100
    mae = mean_absolute_error(actual, predicted)

    consumer_results.append({
        'account_id': acc_id,
        'appliance_group': grp,
        'heated_area': area,
        'residents': res,
        'n_months': n_months,
        'mean_actual': round(actual.mean(), 2),
        'mean_predicted': round(predicted.mean(), 2),
        'sum_actual': round(actual.sum(), 2),
        'sum_predicted': round(predicted.sum(), 2),
        'r2': round(r2, 4) if not np.isnan(r2) else np.nan,
        'mape': round(mape, 1),
        'mae': round(mae, 2),
    })

cr = pd.DataFrame(consumer_results)

# Статистика R2
r2_valid = cr.dropna(subset=['r2'])
print(f"\nПотребителей с R2: {len(r2_valid)} (из {len(cr)})")
print(f"\nРаспределение R2 по потребителям:")
print(f"  Среднее:   {r2_valid['r2'].mean():.4f}")
print(f"  Медиана:   {r2_valid['r2'].median():.4f}")
print(f"  Min:       {r2_valid['r2'].min():.4f}")
print(f"  Max:       {r2_valid['r2'].max():.4f}")
print(f"  Q25:       {r2_valid['r2'].quantile(0.25):.4f}")
print(f"  Q75:       {r2_valid['r2'].quantile(0.75):.4f}")

# Сколько с хорошим R2
for threshold in [0.0, 0.3, 0.5, 0.7, 0.9]:
    count = (r2_valid['r2'] >= threshold).sum()
    pct = count / len(r2_valid) * 100
    print(f"  R2 >= {threshold}: {count} ({pct:.0f}%)")

# По группам
print(f"\nR2 по группам:")
for grp in sorted(r2_valid['appliance_group'].unique()):
    gd = r2_valid[r2_valid['appliance_group'] == grp]
    print(f"  {grp}: N={len(gd)}, mean R2={gd['r2'].mean():.4f}, median R2={gd['r2'].median():.4f}")

# Топ-10 лучших и худших
print(f"\nТоп-10 лучших R2:")
best = r2_valid.nlargest(10, 'r2')
print(f"  {'account_id':<15} {'Группа':<15} {'Площадь':>8} {'Прож':>5} {'Мес':>4} {'R2':>8} {'MAPE':>8} {'Ср.факт':>10} {'Ср.предс':>10}")
for _, r in best.iterrows():
    print(f"  {r['account_id']:<15} {r['appliance_group']:<15} {r['heated_area']:>8.1f} {r['residents']:>5.0f} {r['n_months']:>4} {r['r2']:>8.4f} {r['mape']:>7.1f}% {r['mean_actual']:>10.2f} {r['mean_predicted']:>10.2f}")

print(f"\nТоп-10 худших R2:")
worst = r2_valid.nsmallest(10, 'r2')
print(f"  {'account_id':<15} {'Группа':<15} {'Площадь':>8} {'Прож':>5} {'Мес':>4} {'R2':>8} {'MAPE':>8} {'Ср.факт':>10} {'Ср.предс':>10}")
for _, r in worst.iterrows():
    print(f"  {r['account_id']:<15} {r['appliance_group']:<15} {r['heated_area']:>8.1f} {r['residents']:>5.0f} {r['n_months']:>4} {r['r2']:>8.4f} {r['mape']:>7.1f}% {r['mean_actual']:>10.2f} {r['mean_predicted']:>10.2f}")

# ============================================================================
# Блок 8: R2 по суммарному объему 100 потребителей
# ============================================================================
print("\n" + "=" * 80)
print("Блок 8: R2 суммарного объема")
print("=" * 80)

# Суммарный объем по месяцам
agg = hv.groupby('year_month').agg(
    total_actual=('consumption_norm', 'sum'),
    total_predicted=('predicted', 'sum'),
    n_consumers=('account_id', 'nunique'),
).reset_index()

print(f"\n{'Месяц':<12} {'Факт':>12} {'Прогноз':>12} {'Разн.%':>10} {'N потр':>8}")
print("-" * 60)
for _, row in agg.iterrows():
    diff_pct = (row['total_predicted'] - row['total_actual']) / row['total_actual'] * 100
    print(f"{str(row['year_month']):<12} {row['total_actual']:>12.1f} {row['total_predicted']:>12.1f} {diff_pct:>9.1f}% {int(row['n_consumers']):>8}")

total_a = agg['total_actual'].values
total_p = agg['total_predicted'].values

if len(total_a) > 2:
    agg_r2 = r2_score(total_a, total_p)
    agg_mape = np.mean(np.abs((total_a - total_p) / total_a)) * 100
    agg_mae = mean_absolute_error(total_a, total_p)

    print(f"\n  Суммарный R2:   {agg_r2:.4f}")
    print(f"  Суммарный MAPE: {agg_mape:.1f}%")
    print(f"  Суммарный MAE:  {agg_mae:.1f} m3/мес")
    print(f"  Всего факт:     {total_a.sum():.1f} m3")
    print(f"  Всего прогноз:  {total_p.sum():.1f} m3")
    print(f"  Отклонение:     {(total_p.sum() - total_a.sum()) / total_a.sum() * 100:.1f}%")

# Также: R2 суммарного по группам
print(f"\nСуммарный R2 по группам:")
for grp in sorted(hv['appliance_group'].unique()):
    gd = hv[hv['appliance_group'] == grp]
    ga = gd.groupby('year_month').agg(
        total_actual=('consumption_norm', 'sum'),
        total_predicted=('predicted', 'sum'),
    ).reset_index()
    if len(ga) > 2:
        gr2 = r2_score(ga['total_actual'], ga['total_predicted'])
        gmape = np.mean(np.abs((ga['total_actual'].values - ga['total_predicted'].values) / ga['total_actual'].values)) * 100
        n_c = gd['account_id'].nunique()
        print(f"  {grp}: N={n_c}, R2={gr2:.4f}, MAPE={gmape:.1f}%")

# ============================================================================
# Блок 9: Сохранение
# ============================================================================
print("\n" + "=" * 80)
print("Блок 9: Сохранение результатов")
print("=" * 80)

out_path = OUTPUT_DIR / 'holdout_test_results.xlsx'
with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
    cr.to_excel(writer, sheet_name='Per_Consumer_R2', index=False)

    # Месячная сводка
    agg_out = agg.copy()
    agg_out['year_month'] = agg_out['year_month'].astype(str)
    agg_out.to_excel(writer, sheet_name='Monthly_Aggregate', index=False)

    # Детальные предсказания
    hv_out = hv[['account_id', 'appliance_group', 'year_month', 'heated_area', 'residents',
                 'consumption_norm', 'predicted', 'days_count']].copy()
    hv_out['year_month'] = hv_out['year_month'].astype(str)
    hv_out.to_excel(writer, sheet_name='Detail', index=False)

print(f"[OK] {out_path}")

print("\n" + "=" * 80)
print("ЗАВЕРШЕНО")
print("=" * 80)
