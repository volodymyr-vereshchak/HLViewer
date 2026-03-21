"""
Предикт ежедневного потребления газа для ~210K бытовых потребителей.

Использует готовые профили из profile_gas_consumption.py:
- profiles.json: pg_means, pgvpg_means, summer_op_means
- op_profiles.csv: coef_area, coef_residents по (group, year, month)
- daily_op_profile.csv: norm_weight по date (488 дней)

Входные данные: all_pobut.xlsx (222K потребителей, 11 колонок)
"""

import json
import warnings
from calendar import monthrange
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

SCRIPT_DIR = Path(__file__).parent
INPUT_DIR = SCRIPT_DIR / 'data' / 'input'
PROFILE_DIR = SCRIPT_DIR / 'data' / 'profiles'
OUTPUT_DIR = SCRIPT_DIR / 'data' / 'output'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
SUMMER_MONTHS = {5, 6, 7, 8, 9}

# Период предикта: октябрь 2024 — январь 2026 (16 месяцев)
PREDICT_PERIODS = []
for y in range(2024, 2027):
    for m in range(1, 13):
        if (y == 2024 and m >= 10) or (y == 2025) or (y == 2026 and m <= 1):
            PREDICT_PERIODS.append((y, m))

SKIP_GROUPS = {'---', '222046'}

# Маппинг: группа -> группа для ОП-коэффициентов (fallback)
OP_FALLBACK = {
    'ОП,ПГ': 'ОП,ПГ',
    'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ',
    'ОП,ВПГ': 'ОП,ПГ,ВПГ',
    'ОП': 'ОП,ПГ',
}

# Маппинг: группа -> группа для summer_op_means (fallback)
SUMMER_FALLBACK = {
    'ОП,ПГ': 'ОП,ПГ',
    'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ',
    'ОП,ВПГ': 'ОП,ВПГ',
    'ОП': 'ОП,ПГ',
}

print("=" * 80)
print("ПРЕДИКТ ПОТРЕБЛЕНИЯ ГАЗА — 210K потребителей")
print("=" * 80)

# ============================================================================
# Блок 1: Загрузка профилей
# ============================================================================
print("\n" + "=" * 80)
print("Блок 1: Загрузка профилей")
print("=" * 80)

# 1a. profiles.json — pg_means, pgvpg_means, summer_op_means
with open(PROFILE_DIR / 'profiles.json', 'r', encoding='utf-8') as f:
    profiles = json.load(f)

pg_means = {r['month']: r['pg_mean'] for r in profiles['pg_means']}
pgvpg_means = {r['month']: r['pgvpg_mean'] for r in profiles['pgvpg_means']}
summer_op_means = {}
for r in profiles['summer_op_means']:
    summer_op_means[(r['appliance_group'], r['month'])] = r['summer_mean']

print(f"  pg_means: {len(pg_means)} месяцев")
print(f"  pgvpg_means: {len(pgvpg_means)} месяцев")
print(f"  summer_op_means: {len(summer_op_means)} записей")

# 1b. op_profiles.csv — коэффициенты регрессии по (group, year, month)
op_profiles = pd.read_csv(PROFILE_DIR / 'op_profiles.csv')
op_coefs = {}
for _, row in op_profiles.iterrows():
    key = (row['appliance_group'], int(row['year']), int(row['month']))
    op_coefs[key] = (row['coef_area_per_month'], row['coef_residents_per_month'])
print(f"  op_coefs: {len(op_coefs)} записей (group, year, month)")

# 1c. daily_op_profile.csv — дневные веса ОП
daily_op = pd.read_csv(PROFILE_DIR / 'daily_op_profile.csv')
daily_op['date'] = pd.to_datetime(daily_op['date'])
daily_weights = dict(zip(daily_op['date'], daily_op['norm_weight']))
print(f"  daily_op_profile: {len(daily_weights)} дней ({daily_op['date'].min().date()} — {daily_op['date'].max().date()})")

# 1d. may_hybrid_coefs.csv — коефіцієнти гібриду травня (Модель C)
may_hybrid_coefs = {}  # grp -> (coef_area, coef_res, scale_may, summer_median, w_winter, w_summer)
may_hybrid_path = PROFILE_DIR / 'may_hybrid_coefs.csv'
if may_hybrid_path.exists():
    mhc = pd.read_csv(may_hybrid_path)
    for _, r in mhc.iterrows():
        may_hybrid_coefs[r['appliance_group']] = (
            r['coef_area'], r['coef_res'], r['scale_may'],
            r['summer_median_may'], r['may_winter_weight'], r['may_summer_weight'],
        )
    print(f"  may_hybrid_coefs: {len(may_hybrid_coefs)} групп ({list(may_hybrid_coefs.keys())})")
else:
    print("  may_hybrid_coefs: файл не знайдено, травень -> тільки median")

# 1e. pg_billing_multiplier.csv — персональний множник за груднем 2024 (ПГ, ВПГ, ПГ,ВПГ)
pg_multiplier = {}  # account_id -> multiplier
pg_mult_path = PROFILE_DIR / 'pg_billing_multiplier.csv'
if pg_mult_path.exists():
    pm = pd.read_csv(pg_mult_path)
    pg_multiplier = dict(zip(pm['account_id'], pm['multiplier']))
    print(f"  pg_multiplier: {len(pg_multiplier)} персональних множників (ПГ/ВПГ/ПГ,ВПГ)")
else:
    print("  pg_multiplier: файл не знайдено, ПГ/ВПГ -> груповий середній")

# Подготовка: op_coefs DataFrame для merge
op_coefs_list = []
for (grp, yr, mo), (ca, cr) in op_coefs.items():
    op_coefs_list.append({'coef_group': grp, 'year': yr, 'month': mo,
                          'coef_area': ca, 'coef_residents': cr})
op_coefs_df = pd.DataFrame(op_coefs_list)

# ============================================================================
# Блок 2: Загрузка all_pobut.xlsx
# ============================================================================
print("\n" + "=" * 80)
print("Блок 2: Загрузка all_pobut.xlsx")
print("=" * 80)

raw = pd.read_excel(INPUT_DIR / 'all_pobut.xlsx')
print(f"  Загружено: {len(raw)} строк, {raw.shape[1]} колонок")

col_names = ['account_id', 'gas_off', 'alternative', 'dacha',
             'appliance_group', 'profile_no', 'heated_area',
             'total_area', 'residents', 'residents_all', 'grs']
raw.columns = col_names[:len(raw.columns)]

raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
raw['total_area'] = pd.to_numeric(raw['total_area'].astype(str).str.strip(), errors='coerce')
raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce')
raw['residents_all'] = pd.to_numeric(raw['residents_all'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()

print(f"  Потребителей до фильтрации: {len(raw)}")

# --- Фильтрация ---
mask_exclude = raw['gas_off'].notna() | raw['dacha'].notna()
n_gas_off = raw['gas_off'].notna().sum()
n_dacha = raw['dacha'].notna().sum()
n_alt = raw['alternative'].notna().sum()
print(f"  Газ откл.: {n_gas_off}, Дачи: {n_dacha}, Альтернатива: {n_alt} (оставляем)")

mask_bad_group = raw['appliance_group'].isin(SKIP_GROUPS)
n_bad_group = mask_bad_group.sum()
print(f"  Группы {SKIP_GROUPS}: {n_bad_group}")

mask_total = mask_exclude | mask_bad_group
consumers = raw[~mask_total].copy()
print(f"  Потребителей после фильтрации: {len(consumers)}")

# --- Edge cases ---
consumers['has_OP'] = consumers['appliance_group'].str.contains('ОП', na=False)

# ОП без площади -> медиана по группе
for grp in consumers.loc[consumers['has_OP'], 'appliance_group'].unique():
    mask_grp = consumers['appliance_group'] == grp
    valid_area = consumers.loc[mask_grp & consumers['heated_area'].notna() & (consumers['heated_area'] > 0), 'heated_area']
    median_area = valid_area.median() if len(valid_area) > 0 else 55.0
    bad_area = mask_grp & (consumers['heated_area'].isna() | (consumers['heated_area'] <= 0))
    n_fix = bad_area.sum()
    if n_fix > 0:
        consumers.loc[bad_area, 'heated_area'] = median_area
        print(f"  {grp}: {n_fix} без площади -> медиана {median_area:.1f}")

# Без residents -> 2
no_res = consumers['residents'].isna() | (consumers['residents'] <= 0)
n_no_res = no_res.sum()
consumers.loc[no_res, 'residents'] = 2
print(f"  Без residents: {n_no_res} -> residents=2")

print(f"\n  Группы:")
for grp, cnt in consumers['appliance_group'].value_counts().items():
    print(f"    {grp}: {cnt}")

# ============================================================================
# Блок 3: Месячный предикт (vectorized)
# ============================================================================
print("\n" + "=" * 80)
print("Блок 3: Месячный предикт (vectorized)")
print("=" * 80)

# Создаём DataFrame: каждый потребитель × каждый (year, month)
periods_df = pd.DataFrame(PREDICT_PERIODS, columns=['year', 'month'])

# Предвычисляем days_in_month для периодов
periods_df['days_in_month'] = periods_df.apply(
    lambda r: monthrange(r['year'], r['month'])[1], axis=1
)
periods_df['is_heating'] = periods_df['month'].isin(HEATING_MONTHS)

# Cross join
consumers_slim = consumers[['account_id', 'appliance_group', 'heated_area',
                             'residents', 'grs', 'has_OP']].copy()
consumer_months = consumers_slim.merge(periods_df, how='cross')

print(f"  Потребитель x месяц: {len(consumer_months):,} записей")

# --- Vectorized monthly prediction ---
print("  Вычисление месячных предиктов...")
predicted = np.zeros(len(consumer_months), dtype=np.float64)

grp_col = consumer_months['appliance_group'].values
month_col = consumer_months['month'].values
year_col = consumer_months['year'].values
area_col = consumer_months['heated_area'].values.astype(np.float64)
res_col = consumer_months['residents'].values.astype(np.float64)
is_heating_col = consumer_months['is_heating'].values
has_op_col = consumer_months['has_OP'].values

# 1. ПГ: pg_mean(month)
pg_map = np.array([pg_means.get(m, 7.5) for m in range(1, 13)])  # 0-indexed: pg_map[m-1]
mask_pg = grp_col == 'ПГ'
predicted[mask_pg] = pg_map[month_col[mask_pg] - 1]

# 2. ВПГ: pgvpg_mean - pg_mean
vpg_diff = np.array([max(0, pgvpg_means.get(m, 20.0) - pg_means.get(m, 7.5)) for m in range(1, 13)])
mask_vpg = grp_col == 'ВПГ'
predicted[mask_vpg] = vpg_diff[month_col[mask_vpg] - 1]

# 3. ПГ,ВПГ: pgvpg_mean(month)
pgvpg_map = np.array([pgvpg_means.get(m, 20.0) for m in range(1, 13)])
mask_pgvpg = grp_col == 'ПГ,ВПГ'
predicted[mask_pgvpg] = pgvpg_map[month_col[mask_pgvpg] - 1]

# 3a. Персональний множник для ПГ/ВПГ/ПГ,ВПГ на основі грудня 2024
if pg_multiplier:
    acc_col = consumer_months['account_id'].values
    mask_pg_groups = mask_pg | mask_vpg | mask_pgvpg
    if mask_pg_groups.any():
        acc_ids = acc_col[mask_pg_groups]
        mult_arr = np.array([pg_multiplier.get(aid, 1.0) for aid in acc_ids], dtype=np.float64)
        predicted[mask_pg_groups] *= mult_arr
        n_personalized = (mult_arr != 1.0).sum()
        n_total = mask_pg_groups.sum()
        print(f"  Персоналізовано ПГ/ВПГ/ПГ,ВПГ: {n_personalized:,} / {n_total:,} рядків")

# 4. ОП-группы (летом): median(group, month); травень — гібрид per-consumer
mask_op_summer = has_op_col & ~is_heating_col
if mask_op_summer.any():
    for grp_name in np.unique(grp_col[mask_op_summer]):
        summer_grp = SUMMER_FALLBACK.get(grp_name, 'ОП,ПГ,ВПГ')
        mask_this = mask_op_summer & (grp_col == grp_name)
        for m in range(5, 10):
            mask_m = mask_this & (month_col == m)
            if not mask_m.any():
                continue
            key = (summer_grp, m)
            val = summer_op_means.get(key, summer_op_means.get(('ОП,ПГ,ВПГ', m), pgvpg_means.get(m, 20.0)))

            if m == 5 and may_hybrid_coefs:
                # Травень: per-consumer гібрид 0.4*winter_scaled + 0.6*summer_median
                coef_grp = OP_FALLBACK.get(grp_name, grp_name)
                hc = may_hybrid_coefs.get(coef_grp) or may_hybrid_coefs.get(summer_grp)
                if hc is not None:
                    ca, cr, sc, s_med, w_w, w_s = hc
                    areas = area_col[mask_m]
                    ress  = res_col[mask_m]
                    winter_part = np.maximum(0.0, (ca * areas + cr * ress) * sc)
                    predicted[mask_m] = w_w * winter_part + w_s * s_med
                    continue  # не перезаписуємо нижче

            predicted[mask_m] = val

# 5. ОП-группы (зимой): coef_area * area + coef_residents * residents
mask_op_winter = has_op_col & is_heating_col
if mask_op_winter.any():
    # Добавляем coef_group колонку
    coef_group_col = np.array([OP_FALLBACK.get(g, 'ОП,ПГ,ВПГ') for g in grp_col])

    for cg in np.unique(coef_group_col[mask_op_winter]):
        for y, m in PREDICT_PERIODS:
            if m not in HEATING_MONTHS:
                continue
            mask_this = mask_op_winter & (coef_group_col == cg) & (year_col == y) & (month_col == m)
            if not mask_this.any():
                continue
            key = (cg, y, m)
            if key in op_coefs:
                ca, cr = op_coefs[key]
            else:
                # Fallback: тот же месяц из другого года
                ca, cr = None, None
                for alt_y in [2025, 2024, 2026]:
                    alt_key = (cg, alt_y, m)
                    if alt_key in op_coefs:
                        ca, cr = op_coefs[alt_key]
                        break
                if ca is None:
                    ca, cr = 1.5, 20.0
            predicted[mask_this] = np.maximum(0, ca * area_col[mask_this] + cr * res_col[mask_this])

# 6. Всё остальное (не ОП, не ПГ, не ВПГ, не ПГ,ВПГ) — fallback к ПГ
mask_other = ~(mask_pg | mask_vpg | mask_pgvpg | has_op_col)
if mask_other.any():
    predicted[mask_other] = pg_map[month_col[mask_other] - 1]

consumer_months['predicted_month'] = predicted

# Статистика
print(f"\n  Средние предикты по группам (m3/мес):")
grp_stats = consumer_months.groupby('appliance_group')['predicted_month'].agg(['mean', 'count'])
for grp_name, row in grp_stats.iterrows():
    n_consumers = int(row['count']) // len(PREDICT_PERIODS)
    print(f"    {grp_name}: средн. {row['mean']:.1f}, потребителей {n_consumers}")

print(f"\n  Суммы по месяцам (тыс. m3):")
month_sums = consumer_months.groupby(['year', 'month'])['predicted_month'].sum()
for (y, m), total in month_sums.items():
    print(f"    {y}-{m:02d}: {total / 1000:.1f}")

# ============================================================================
# Блок 4: Дневное распределение
# ============================================================================
print("\n" + "=" * 80)
print("Блок 4: Дневное распределение")
print("=" * 80)

# Генерируем все даты периода
all_dates = []
for y, m in PREDICT_PERIODS:
    n_days = monthrange(y, m)[1]
    for d in range(1, n_days + 1):
        all_dates.append(pd.Timestamp(y, m, d))
all_dates = sorted(all_dates)
print(f"  Дат в периоде: {len(all_dates)} ({all_dates[0].date()} — {all_dates[-1].date()})")

# Строим дневные веса по месяцам
# Для каждого месяца: op_weights (norm_weight, нормализован к 1) и uniform_weight (1/days)
month_daily_info = {}  # (y,m) -> {'col_start': int, 'n_days': int, 'op_weights': array, 'uniform_w': float}
col_offset = 0
for y, m in PREDICT_PERIODS:
    n_days = monthrange(y, m)[1]
    month_dates = [pd.Timestamp(y, m, d) for d in range(1, n_days + 1)]

    # ОП веса
    op_w = np.array([daily_weights.get(dt, 1.0 / n_days) for dt in month_dates])
    op_w_sum = op_w.sum()
    if op_w_sum > 0:
        op_w = op_w / op_w_sum  # нормализуем к 1

    month_daily_info[(y, m)] = {
        'col_start': col_offset,
        'n_days': n_days,
        'op_weights': op_w,
        'uniform_w': 1.0 / n_days,
    }
    col_offset += n_days

n_dates = len(all_dates)
n_consumers_total = len(consumers_slim)
print(f"  Матрица: {n_consumers_total} x {n_dates} = {n_consumers_total * n_dates:,} ячеек")

# Строим матрицу потребитель × дата (float32 для экономии памяти)
result_matrix = np.zeros((n_consumers_total, n_dates), dtype=np.float32)

# account_id порядок = порядок в consumers_slim
account_ids = consumers_slim['account_id'].values
has_op_arr = consumers_slim['has_OP'].values

# Pivot consumer_months: для каждого (y,m) получаем predicted_month в порядке consumers_slim
# Так как cross join сохраняет порядок: первые N строк = период 0, вторые N = период 1, ...
n_c = n_consumers_total
n_p = len(PREDICT_PERIODS)

# Проверяем порядок
assert len(consumer_months) == n_c * n_p, "Cross join size mismatch"

pred_values = consumer_months['predicted_month'].values.reshape(n_c, n_p)
# pred_values[i, j] = predicted_month для потребителя i, периода j

print("  Заполнение дневной матрицы...")
for j, (y, m) in enumerate(PREDICT_PERIODS):
    info = month_daily_info[(y, m)]
    cs = info['col_start']
    nd = info['n_days']
    is_heating = m in HEATING_MONTHS

    pred_col = pred_values[:, j]  # (n_consumers,)

    if is_heating:
        # ОП потребители -> op_weights, остальные -> uniform
        op_w = info['op_weights']  # (n_days,)
        uw = info['uniform_w']

        # Матричное умножение: pred * weight
        # ОП: result[i, cs:cs+nd] = pred[i] * op_w
        # Не-ОП: result[i, cs:cs+nd] = pred[i] * uw

        # Uniform для всех
        result_matrix[:, cs:cs+nd] = pred_col[:, np.newaxis] * uw

        # Перезаписываем ОП
        op_mask = has_op_arr
        if op_mask.any():
            result_matrix[op_mask, cs:cs+nd] = pred_col[op_mask, np.newaxis] * op_w[np.newaxis, :]
    else:
        # Все uniform
        uw = info['uniform_w']
        result_matrix[:, cs:cs+nd] = pred_col[:, np.newaxis] * uw

print("  Матрица заполнена.")

# Верификация: сумма дневных = месячному
print("\n  Верификация (сумма дневных vs месячный):")
for j, (y, m) in enumerate(PREDICT_PERIODS[:3]):
    info = month_daily_info[(y, m)]
    cs = info['col_start']
    nd = info['n_days']
    daily_sum = result_matrix[:, cs:cs+nd].sum(axis=1)
    monthly_pred = pred_values[:, j]
    mask_nz = monthly_pred > 0
    if mask_nz.any():
        max_diff = np.max(np.abs(daily_sum[mask_nz] - monthly_pred[mask_nz]))
        rel_err = np.max(np.abs(daily_sum[mask_nz] - monthly_pred[mask_nz]) / monthly_pred[mask_nz])
        print(f"    {y}-{m:02d}: max абс. разница = {max_diff:.6f}, max отн. = {rel_err:.2e}")

# ============================================================================
# Блок 5: Вывод
# ============================================================================
print("\n" + "=" * 80)
print("Блок 5: Вывод (parquet + Excel-сводка)")
print("=" * 80)

date_columns = [dt.strftime('%Y-%m-%d') for dt in all_dates]
meta_df = consumers_slim[['account_id', 'appliance_group', 'heated_area', 'residents', 'grs']].reset_index(drop=True)

print(f"  Output shape: ({n_consumers_total}, {len(meta_df.columns) + n_dates})")

# Агрегация по ГРС и месяцу
print("\n  Агрегация по ГРС...")
grs_values = meta_df['grs'].values
unique_grs = np.unique(grs_values)

grs_summary_rows = []
for j, (y, m) in enumerate(PREDICT_PERIODS):
    info = month_daily_info[(y, m)]
    cs = info['col_start']
    nd = info['n_days']
    month_totals = result_matrix[:, cs:cs+nd].sum(axis=1)

    for grs_name in unique_grs:
        grs_mask = grs_values == grs_name
        grs_total = month_totals[grs_mask].sum()
        grs_count = grs_mask.sum()
        grs_summary_rows.append({
            'grs': grs_name,
            'year': y,
            'month': m,
            'total_m3': round(float(grs_total), 2),
            'n_consumers': int(grs_count),
            'mean_per_consumer': round(float(grs_total / grs_count) if grs_count > 0 else 0, 2),
        })

grs_summary = pd.DataFrame(grs_summary_rows)

# --- 1. Parquet: основной файл с дневными предиктами ---
print("\n  Формирование выходного DataFrame...")
result_df = pd.DataFrame(result_matrix, columns=date_columns)
output_df = pd.concat([meta_df, result_df], axis=1)

parquet_path = OUTPUT_DIR / 'all_consumers_daily_prediction.parquet'
print(f"  Сохранение в {parquet_path}...")
output_df.to_parquet(parquet_path, index=False, engine='pyarrow')
print(f"  [OK] {parquet_path} ({parquet_path.stat().st_size / 1024 / 1024:.1f} MB)")

# --- 2. Excel: только GRS-сводка (компактный файл) ---
summary_path = OUTPUT_DIR / 'prediction_grs_summary.xlsx'
grs_summary.to_excel(summary_path, index=False, engine='openpyxl')
print(f"  [OK] {summary_path}")

# --- 3. CSV: месячные предикты по потребителям (компактнее дневных) ---
monthly_path = OUTPUT_DIR / 'all_consumers_monthly_prediction.csv'
print(f"  Сохранение месячных предиктов в {monthly_path}...")
monthly_records = []
for j, (y, m) in enumerate(PREDICT_PERIODS):
    info = month_daily_info[(y, m)]
    cs = info['col_start']
    nd = info['n_days']
    month_totals = result_matrix[:, cs:cs+nd].sum(axis=1)
    col_name = f"{y}-{m:02d}"
    if not monthly_records:
        monthly_records = [{} for _ in range(n_consumers_total)]
    for i in range(n_consumers_total):
        monthly_records[i][col_name] = round(float(month_totals[i]), 2)

monthly_df = pd.DataFrame(monthly_records)
monthly_output = pd.concat([meta_df, monthly_df], axis=1)
monthly_output.to_csv(monthly_path, index=False, encoding='utf-8-sig')
print(f"  [OK] {monthly_path}")

print(f"\n  Итого файлы:")
print(f"    {parquet_path.name}: {n_consumers_total} строк x {output_df.shape[1]} колонок (дневные)")
print(f"    {monthly_path.name}: {n_consumers_total} строк x {monthly_output.shape[1]} колонок (месячные)")
print(f"    {summary_path.name}: {len(grs_summary)} строк (ГРС-сводка)")

# Итоговая статистика
print("\n" + "=" * 80)
print("ИТОГО")
print("=" * 80)
print(f"  Потребителей: {n_consumers_total}")
print(f"  Период: {all_dates[0].date()} — {all_dates[-1].date()} ({n_dates} дней)")
print(f"  Месяцев: {len(PREDICT_PERIODS)}")

print(f"\n  По группам:")
for grp in sorted(consumers_slim['appliance_group'].unique()):
    mask = consumers_slim['appliance_group'].values == grp
    cnt = mask.sum()
    total_per_consumer = result_matrix[mask, :].sum(axis=1).mean()
    avg_monthly = total_per_consumer / len(PREDICT_PERIODS)
    print(f"    {grp}: {cnt} потреб., средн. {avg_monthly:.1f} m3/мес")

print(f"\n  Суммы по месяцам (тыс. m3):")
for j, (y, m) in enumerate(PREDICT_PERIODS):
    info = month_daily_info[(y, m)]
    cs = info['col_start']
    nd = info['n_days']
    total = result_matrix[:, cs:cs+nd].sum()
    print(f"    {y}-{m:02d}: {total / 1000:.1f}")

# ГРС Топ-10
print(f"\n  Топ-10 ГРС по суммарному объёму:")
grs_total = grs_summary.groupby('grs')['total_m3'].sum().sort_values(ascending=False).head(10)
for grs_name, total in grs_total.items():
    print(f"    {grs_name}: {total / 1000:.1f} тыс. m3")

print("\n" + "=" * 80)
print("ЗАВЕРШЕНО")
print("=" * 80)
