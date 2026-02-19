"""
Дневной профиль потребления: удельные веса ОП на м2 по дням.

Подход:
1. Из дневных данных ОП-потребителей вычитаем ПГ/ВПГ медианы (дневные)
2. Делим на площадь -> ОП/м2/день
3. Считаем удельный вес каждого дня от суммы за месяц
4. Усредняем форму по всем потребителям -> единый дневной профиль
"""

import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import r2_score

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 30)
pd.set_option('display.width', 250)

SCRIPT_DIR = Path(__file__).parent
INPUT_DIR = SCRIPT_DIR / 'data' / 'input'
OUTPUT_DIR = SCRIPT_DIR / 'data' / 'profiles'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PROFILE_FILE = INPUT_DIR / 'profile_pobut_daily_result.csv'
MIN_DAYS = 90

print("=" * 80)
print("ДНЕВНОЙ ПРОФИЛЬ: удельные веса ОП/м2")
print("=" * 80)

# ============================================================================
# Блок 1: Загрузка
# ============================================================================
print("\nБлок 1: Загрузка...")

raw = pd.read_csv(PROFILE_FILE, sep=';', encoding='utf-8')
meta_cols = ['account_id', 'gas_off', 'alternative', 'dacha',
             'appliance_group', 'profile_no', 'heated_area',
             'total_area', 'residents', 'residents_all', 'device_serial']
raw.columns = meta_cols + list(raw.columns[len(meta_cols):])

raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
raw['total_area'] = pd.to_numeric(raw['total_area'].astype(str).str.strip(), errors='coerce')
raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce')

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

print(f"  {len(df):,} записей, {df['account_id'].nunique()} потребителей")

# ============================================================================
# Блок 2: Очистка (как в основном скрипте)
# ============================================================================
print("\nБлок 2: Очистка...")

df = df.dropna(subset=['consumption'])

# Исключение альтернативы/дач/газ откл
flagged_ids = set(raw.loc[
    raw['gas_off'].notna() | raw['alternative'].notna() | raw['dacha'].notna(),
    'account_id'
])
df = df[~df['account_id'].isin(flagged_ids)]

# Минимум дней
days_per = df.groupby('account_id')['date'].nunique()
df = df[df['account_id'].isin(days_per[days_per >= MIN_DAYS].index)]

# Нулевые потребители
zero_stats = df.groupby('account_id').agg(
    total=('consumption', 'count'),
    zeros=('consumption', lambda x: (x == 0).sum()),
)
zero_stats['pct'] = zero_stats['zeros'] / zero_stats['total']
zero_ids = set(zero_stats[zero_stats['pct'] > 0.50].index)
df = df[~df['account_id'].isin(zero_ids)]

# Скачки
df = df.sort_values(['account_id', 'date'])
df['roll_med'] = df.groupby('account_id')['consumption'].transform(
    lambda x: x.rolling(14, min_periods=7, center=True).median()
)
df['is_spike'] = (df['consumption'] > df['roll_med'] * 5) & (df['roll_med'] > 0.1)


def mark_zero_runs(group):
    cons = group['consumption'].values
    bad = np.zeros(len(cons), dtype=bool)
    run_start = None
    run_len = 0
    for i in range(len(cons)):
        if cons[i] == 0:
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
df = df[~(df['is_spike'] | df['is_zero_run'])].copy()

# Квантильные выбросы
for grp in df['appliance_group'].unique():
    mask = df['appliance_group'] == grp
    thr = df.loc[mask, 'consumption'].quantile(0.995)
    df.loc[mask & (df['consumption'] > thr), 'consumption'] = np.nan
df = df.dropna(subset=['consumption'])

# Проверка минимума дней после очистки
clean_days = df.groupby('account_id')['date'].nunique()
df = df[df['account_id'].isin(clean_days[clean_days >= MIN_DAYS].index)]

df = df.drop(columns=['roll_med', 'is_spike', 'is_zero_run'], errors='ignore')

print(f"  После очистки: {len(df):,} записей, {df['account_id'].nunique()} потребителей")

# ============================================================================
# Блок 3: ПГ и ВПГ дневные медианы
# ============================================================================
print("\nБлок 3: Дневные медианы ПГ/ВПГ...")

# ПГ дневные медианы по (residents, month)
pg_only = df[df['appliance_group'] == 'ПГ'].copy()
pg_only = pg_only[pg_only['residents'] > 0]
pg_daily_med = pg_only.groupby(['residents', 'month'])['consumption'].median()
pg_by_res = pg_only.groupby('residents')['consumption'].median()
pg_global_per_person = pg_only['consumption'].sum() / pg_only['residents'].sum()

print(f"  ПГ: {pg_only['account_id'].nunique()} потребителей")
print(f"  ПГ дневная медиана (1 чел): {pg_daily_med.get((1, 1), 0):.3f} m3/день (янв)")

# ВПГ дневные медианы: из группы ПГ,ВПГ минус ПГ
pgvpg = df[df['appliance_group'] == 'ПГ,ВПГ'].copy()
pgvpg = pgvpg[pgvpg['residents'] > 0]


def get_pg_daily(res, month):
    v = pg_daily_med.get((res, month))
    if v is not None:
        return v
    v2 = pg_by_res.get(res)
    if v2 is not None:
        return v2
    return pg_global_per_person * res


pgvpg['pg_part'] = pgvpg.apply(lambda r: get_pg_daily(r['residents'], r['month']), axis=1)
pgvpg['vpg_part'] = (pgvpg['consumption'] - pgvpg['pg_part']).clip(lower=0)

vpg_daily_med = pgvpg.groupby(['residents', 'month'])['vpg_part'].median()
vpg_by_res = pgvpg.groupby('residents')['vpg_part'].median()
vpg_global_per_person = pgvpg['vpg_part'].sum() / pgvpg['residents'].sum()

print(f"  ВПГ: {pgvpg['account_id'].nunique()} потребителей")
print(f"  ВПГ дневная медиана (2 чел): {vpg_daily_med.get((2, 1), 0):.3f} m3/день (янв)")


def get_vpg_daily(res, month):
    v = vpg_daily_med.get((res, month))
    if v is not None:
        return v
    v2 = vpg_by_res.get(res)
    if v2 is not None:
        return v2
    return vpg_global_per_person * res


# ============================================================================
# Блок 4: Выделение ОП-компонента по дням
# ============================================================================
print("\nБлок 4: ОП-компонент по дням...")

op_df = df[df['has_OP']].copy()
op_df = op_df[(op_df['heated_area'] > 0) & (op_df['residents'] > 0)]

# Вычитаем ПГ/ВПГ дневные медианы
op_df['pg_daily'] = op_df.apply(
    lambda r: get_pg_daily(r['residents'], r['month']) if r['has_PG'] else 0, axis=1)
op_df['vpg_daily'] = op_df.apply(
    lambda r: get_vpg_daily(r['residents'], r['month']) if r['has_VPG'] else 0, axis=1)

op_df['op_component'] = (op_df['consumption'] - op_df['pg_daily'] - op_df['vpg_daily']).clip(lower=0)
op_df['op_per_m2'] = op_df['op_component'] / op_df['heated_area']

print(f"  ОП записей: {len(op_df):,}, потребителей: {op_df['account_id'].nunique()}")
print(f"  Ср. ОП/м2/день: {op_df['op_per_m2'].mean():.4f}")

# ============================================================================
# Блок 5: Удельные веса по дням внутри месяца
# ============================================================================
print("\nБлок 5: Удельные веса...")

# Сумма ОП/м2 за каждый месяц для каждого потребителя
month_sum = op_df.groupby(['account_id', 'year_month'])['op_per_m2'].transform('sum')
op_df['weight'] = op_df['op_per_m2'] / month_sum.replace(0, np.nan)
op_df = op_df.dropna(subset=['weight'])

# Средний удельный вес по дате (усреднение по всем потребителям)
daily_profile = op_df.groupby('date').agg(
    mean_weight=('weight', 'mean'),
    median_weight=('weight', 'median'),
    std_weight=('weight', 'std'),
    mean_op_m2=('op_per_m2', 'mean'),
    n_consumers=('account_id', 'nunique'),
).reset_index()

daily_profile['month'] = daily_profile['date'].dt.month
daily_profile['year'] = daily_profile['date'].dt.year
daily_profile['year_month'] = daily_profile['date'].dt.to_period('M')
daily_profile['day'] = daily_profile['date'].dt.day

# Нормализуем: веса внутри каждого месяца должны давать 1.0
for ym in daily_profile['year_month'].unique():
    mask = daily_profile['year_month'] == ym
    s = daily_profile.loc[mask, 'mean_weight'].sum()
    if s > 0:
        daily_profile.loc[mask, 'norm_weight'] = daily_profile.loc[mask, 'mean_weight'] / s

print(f"  Дней в профиле: {len(daily_profile)}")

# Вывод по месяцам
print(f"\n{'Месяц':<10} {'Дней':>5} {'N потр':>8} {'Ср.ОП/м2':>10} "
      f"{'Min вес':>10} {'Max вес':>10} {'Std вес':>10} {'Max/Min':>10}")
print("-" * 80)
for ym in sorted(daily_profile['year_month'].unique()):
    md = daily_profile[daily_profile['year_month'] == ym]
    mn_w = md['norm_weight'].min()
    mx_w = md['norm_weight'].max()
    ratio = mx_w / mn_w if mn_w > 0 else 0
    print(f"{str(ym):<10} {len(md):>5} {int(md['n_consumers'].mean()):>8} "
          f"{md['mean_op_m2'].mean():>10.4f} "
          f"{mn_w:>10.4f} {mx_w:>10.4f} {md['std_weight'].mean():>10.4f} {ratio:>10.1f}")

# ============================================================================
# Блок 6: Проверка однородности формы между потребителями
# ============================================================================
print(f"\n{'='*80}")
print("Блок 6: Насколько форма одинакова у разных потребителей?")
print(f"{'='*80}")

# Для каждого потребителя и месяца посчитаем корреляцию его формы со средней
correlations = []
for ym in sorted(op_df['year_month'].unique()):
    ym_data = op_df[op_df['year_month'] == ym]
    # Средний профиль за этот месяц
    avg_profile = ym_data.groupby('date')['weight'].mean().sort_index()

    for acc_id in ym_data['account_id'].unique():
        consumer = ym_data[ym_data['account_id'] == acc_id].set_index('date')['weight'].sort_index()
        # Выровнять по датам
        common = avg_profile.index.intersection(consumer.index)
        if len(common) < 20:
            continue
        corr = np.corrcoef(avg_profile[common].values, consumer[common].values)[0, 1]
        correlations.append({
            'account_id': acc_id,
            'year_month': ym,
            'correlation': corr,
            'n_days': len(common),
        })

corr_df = pd.DataFrame(correlations)
corr_valid = corr_df.dropna(subset=['correlation'])

print(f"\nКорреляция формы потребителя со средней (по {len(corr_valid)} наблюдениям):")
print(f"  Среднее:  {corr_valid['correlation'].mean():.4f}")
print(f"  Медиана:  {corr_valid['correlation'].median():.4f}")
print(f"  Q25:      {corr_valid['correlation'].quantile(0.25):.4f}")
print(f"  Q75:      {corr_valid['correlation'].quantile(0.75):.4f}")

# По месяцам
print(f"\nПо месяцам:")
for ym in sorted(corr_valid['year_month'].unique()):
    cd = corr_valid[corr_valid['year_month'] == ym]
    print(f"  {str(ym):<10} N={len(cd):>5}, corr mean={cd['correlation'].mean():.4f}, "
          f"median={cd['correlation'].median():.4f}")

# Сколько потребителей с высокой корреляцией
print(f"\nДоля потребителей по корреляции:")
for thr in [0.5, 0.7, 0.8, 0.9]:
    n = (corr_valid['correlation'] >= thr).sum()
    pct = n / len(corr_valid) * 100
    print(f"  corr >= {thr}: {n} ({pct:.0f}%)")

# ============================================================================
# Блок 7: Валидация — holdout 100 потребителей
# ============================================================================
print(f"\n{'='*80}")
print("Блок 7: Holdout-тест (100 ОП потребителей)")
print(f"{'='*80}")

op_consumers = op_df['account_id'].unique()
months_per = op_df.groupby('account_id')['year_month'].nunique()
op_enough = months_per[months_per >= 6].index.values

np.random.seed(42)
holdout_ids = set(np.random.choice(op_enough, size=min(100, len(op_enough)), replace=False))
train_ids = set(op_df['account_id'].unique()) - holdout_ids

print(f"  Train: {len(train_ids)}, Holdout: {len(holdout_ids)}")

# Строим профиль на train
train_op = op_df[op_df['account_id'].isin(train_ids)].copy()

# Средние удельные веса по дате (из train)
train_daily_profile = train_op.groupby('date')['weight'].mean().reset_index()
train_daily_profile.columns = ['date', 'profile_weight']
# Нормализуем по месяцам
train_daily_profile['year_month'] = train_daily_profile['date'].dt.to_period('M')
for ym in train_daily_profile['year_month'].unique():
    mask = train_daily_profile['year_month'] == ym
    s = train_daily_profile.loc[mask, 'profile_weight'].sum()
    if s > 0:
        train_daily_profile.loc[mask, 'profile_weight'] /= s

# --- Также строим ПГ/ВПГ медианы на train (без holdout) ---
train_df_all = df[~df['account_id'].isin(holdout_ids)]

tr_pg = train_df_all[train_df_all['appliance_group'] == 'ПГ']
tr_pg = tr_pg[tr_pg['residents'] > 0]
tr_pg_med = tr_pg.groupby(['residents', 'month'])['consumption'].median()
tr_pg_by_res = tr_pg.groupby('residents')['consumption'].median()
tr_pg_global = tr_pg['consumption'].sum() / tr_pg['residents'].sum()

tr_pgvpg = train_df_all[train_df_all['appliance_group'] == 'ПГ,ВПГ']
tr_pgvpg = tr_pgvpg[tr_pgvpg['residents'] > 0].copy()
tr_pgvpg['pg_part'] = tr_pgvpg.apply(
    lambda r: tr_pg_med.get((r['residents'], r['month']),
              tr_pg_by_res.get(r['residents'], tr_pg_global * r['residents'])), axis=1)
tr_pgvpg['vpg_part'] = (tr_pgvpg['consumption'] - tr_pgvpg['pg_part']).clip(lower=0)
tr_vpg_med = tr_pgvpg.groupby(['residents', 'month'])['vpg_part'].median()
tr_vpg_by_res = tr_pgvpg.groupby('residents')['vpg_part'].median()
tr_vpg_global = tr_pgvpg['vpg_part'].sum() / tr_pgvpg['residents'].sum()


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


# --- Также нужен коэффициент ОП/м2/месяц из train регрессии ---
# Для каждого месяца: средний ОП/м2 из train
train_op_monthly = train_op.groupby(['account_id', 'year_month']).agg(
    op_sum=('op_component', 'sum'),
    days=('op_component', 'count'),
).reset_index()
train_op_monthly = train_op_monthly.merge(
    raw[['account_id', 'heated_area']].drop_duplicates(), on='account_id', how='left')
train_op_monthly['op_per_m2_month'] = train_op_monthly['op_sum'] / train_op_monthly['heated_area']
train_op_monthly['days_in_month'] = train_op_monthly['year_month'].apply(lambda p: p.days_in_month)
train_op_monthly['op_per_m2_norm'] = (train_op_monthly['op_per_m2_month']
                                       / train_op_monthly['days'] * train_op_monthly['days_in_month'])

# Медианный коэффициент ОП/м2/месяц
op_coef_monthly = train_op_monthly.groupby('year_month')['op_per_m2_norm'].median()

# --- Предсказание для holdout по дням ---
holdout_op = op_df[op_df['account_id'].isin(holdout_ids)].copy()
holdout_op = holdout_op.merge(train_daily_profile[['date', 'profile_weight']], on='date', how='left')

# Для каждого holdout потребителя и месяца:
# predicted_daily = pg_daily + vpg_daily + op_coef_m2_month * area * profile_weight_day
holdout_op['pred_pg'] = holdout_op.apply(
    lambda r: tr_get_pg(r['residents'], r['month']) if r['has_PG'] else 0, axis=1)
holdout_op['pred_vpg'] = holdout_op.apply(
    lambda r: tr_get_vpg(r['residents'], r['month']) if r['has_VPG'] else 0, axis=1)

# ОП: op_coef * area * daily_weight
holdout_op['op_coef'] = holdout_op['year_month'].map(op_coef_monthly)
holdout_op['pred_op'] = (holdout_op['op_coef'] * holdout_op['heated_area']
                          * holdout_op['profile_weight']).fillna(0).clip(lower=0)

holdout_op['predicted'] = holdout_op['pred_pg'] + holdout_op['pred_vpg'] + holdout_op['pred_op']
holdout_op = holdout_op.dropna(subset=['predicted', 'consumption'])
holdout_op = holdout_op[holdout_op['predicted'] > 0]

print(f"  Holdout записей: {len(holdout_op):,}")

# --- R2 по дням (все вместе) ---
all_a = holdout_op['consumption'].values
all_p = holdout_op['predicted'].values
daily_r2 = r2_score(all_a, all_p)
daily_mape = np.mean(np.abs((all_a - all_p) / np.maximum(all_a, 0.01))) * 100
print(f"\n  Дневной R2 (все): {daily_r2:.4f}")
print(f"  Дневной MAPE:     {daily_mape:.1f}%")

# --- R2 по месячным суммам ---
holdout_monthly = holdout_op.groupby(['account_id', 'year_month']).agg(
    actual_sum=('consumption', 'sum'),
    predicted_sum=('predicted', 'sum'),
    days=('consumption', 'count'),
).reset_index()
holdout_monthly['days_in_month'] = holdout_monthly['year_month'].apply(lambda p: p.days_in_month)
holdout_monthly['actual_norm'] = holdout_monthly['actual_sum'] / holdout_monthly['days'] * holdout_monthly['days_in_month']
holdout_monthly['pred_norm'] = holdout_monthly['predicted_sum'] / holdout_monthly['days'] * holdout_monthly['days_in_month']

monthly_r2 = r2_score(holdout_monthly['actual_norm'], holdout_monthly['pred_norm'])
monthly_mape = np.mean(np.abs(
    (holdout_monthly['actual_norm'] - holdout_monthly['pred_norm'])
    / holdout_monthly['actual_norm'].replace(0, np.nan).dropna()
)) * 100
print(f"\n  Месячный R2 (per consumer): {monthly_r2:.4f}")
print(f"  Месячный MAPE:              {monthly_mape:.1f}%")

# --- R2 суммарного объема ---
agg = holdout_op.groupby('year_month').agg(
    total_actual=('consumption', 'sum'),
    total_predicted=('predicted', 'sum'),
    n_consumers=('account_id', 'nunique'),
).reset_index()

print(f"\n{'Месяц':<10} {'Факт':>10} {'Прогноз':>10} {'Разн%':>8} {'N':>5}")
print("-" * 50)
for _, r in agg.iterrows():
    diff = (r['total_predicted'] - r['total_actual']) / r['total_actual'] * 100
    print(f"{str(r['year_month']):<10} {r['total_actual']:>10.0f} {r['total_predicted']:>10.0f} "
          f"{diff:>7.1f}% {int(r['n_consumers']):>5}")

ta = agg['total_actual'].values
tp = agg['total_predicted'].values
agg_r2 = r2_score(ta, tp)
agg_mape = np.mean(np.abs((ta - tp) / ta)) * 100
print(f"\n  Суммарный R2:       {agg_r2:.4f}")
print(f"  Суммарный MAPE:     {agg_mape:.1f}%")
print(f"  Общее отклонение:   {(tp.sum()-ta.sum())/ta.sum()*100:.1f}%")

# --- R2 суммарного по дням ---
daily_agg = holdout_op.groupby('date').agg(
    total_actual=('consumption', 'sum'),
    total_predicted=('predicted', 'sum'),
).reset_index()

daily_agg_r2 = r2_score(daily_agg['total_actual'], daily_agg['total_predicted'])
print(f"\n  Суммарный R2 по дням: {daily_agg_r2:.4f}")

# ============================================================================
# Блок 8: Вывод профиля
# ============================================================================
print(f"\n{'='*80}")
print("Блок 8: Сохранение")
print(f"{'='*80}")

# Сохраним дневной профиль
out_profile = daily_profile[['date', 'norm_weight', 'mean_op_m2', 'n_consumers']].copy()
out_profile['date'] = out_profile['date'].dt.strftime('%Y-%m-%d')
out_profile.to_csv(OUTPUT_DIR / 'daily_op_profile.csv', index=False)
print(f"[OK] daily_op_profile.csv ({len(out_profile)} дней)")

# ОП коэффициент по месяцам
op_coef_out = op_coef_monthly.reset_index()
op_coef_out.columns = ['year_month', 'op_per_m2_month']
op_coef_out['year_month'] = op_coef_out['year_month'].astype(str)
op_coef_out.to_csv(OUTPUT_DIR / 'op_coef_monthly.csv', index=False)
print(f"[OK] op_coef_monthly.csv")

print(f"\n{'='*80}")
print("ЗАВЕРШЕНО")
print(f"{'='*80}")
print(f"\nФормула для дневного прогноза:")
print(f"  predicted_day = PG_daily(res, month) + VPG_daily(res, month)")
print(f"                + OP_coef_m2_month * area * daily_weight(date)")
