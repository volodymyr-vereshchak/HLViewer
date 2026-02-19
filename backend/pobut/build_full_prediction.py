"""
Объединённый скрипт: фактические данные модемов + предикт для остальных.

Логика:
1. Загрузить профили из data/profiles/
2. Загрузить файл модемов (CSV ;-separated, 11 мета-колонок + 488 дат) -> metered_ids
3. Загрузить all_pobut.xlsx -> ~222K потребителей
4. Фильтрация: убрать газ откл., дачи, группы "---"/"222046"
5. Разделить:
   - metered (account_id в metered_ids): фактические дневные as-is, NaN->0
   - unmetered (остальные): предикт по профилям (логика из predict_all_consumers.py)
6. Объединить, колонка source = "fact" / "predict"
7. Сохранить parquet, monthly CSV, GRS-сводку
"""

import argparse
import json
import re
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

PREDICT_PERIODS = []
for y in range(2024, 2027):
    for m in range(1, 13):
        if (y == 2024 and m >= 10) or (y == 2025) or (y == 2026 and m <= 1):
            PREDICT_PERIODS.append((y, m))

SKIP_GROUPS = {'---', '222046'}

OP_FALLBACK = {
    'ОП,ПГ': 'ОП,ПГ',
    'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ',
    'ОП,ВПГ': 'ОП,ПГ,ВПГ',
    'ОП': 'ОП,ПГ',
}

SUMMER_FALLBACK = {
    'ОП,ПГ': 'ОП,ПГ',
    'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ',
    'ОП,ВПГ': 'ОП,ВПГ',
    'ОП': 'ОП,ПГ',
}

META_COLS = ['account_id', 'gas_off', 'alternative', 'dacha',
             'appliance_group', 'profile_no', 'heated_area',
             'total_area', 'residents', 'residents_all', 'device_serial']


def parse_args():
    parser = argparse.ArgumentParser(description='Build full prediction: metered fact + unmetered predict')
    parser.add_argument('--metered', type=str,
                        default=str(INPUT_DIR / 'profile_pobut_daily_result.csv'),
                        help='Path to metered consumers CSV (;-separated)')
    parser.add_argument('--consumers', type=str,
                        default=str(INPUT_DIR / 'all_pobut.xlsx'),
                        help='Path to all consumers XLSX')
    return parser.parse_args()


# ============================================================================
# Блок 1: Загрузка профилей
# ============================================================================
def load_profiles():
    """Загрузка профилей из data/profiles/."""
    with open(PROFILE_DIR / 'profiles.json', 'r', encoding='utf-8') as f:
        profiles = json.load(f)

    pg_means = {r['month']: r['pg_mean'] for r in profiles['pg_means']}
    pgvpg_means = {r['month']: r['pgvpg_mean'] for r in profiles['pgvpg_means']}
    summer_op_means = {}
    for r in profiles['summer_op_means']:
        summer_op_means[(r['appliance_group'], r['month'])] = r['summer_mean']

    op_profiles = pd.read_csv(PROFILE_DIR / 'op_profiles.csv')
    op_coefs = {}
    for _, row in op_profiles.iterrows():
        key = (row['appliance_group'], int(row['year']), int(row['month']))
        op_coefs[key] = (row['coef_area_per_month'], row['coef_residents_per_month'])

    daily_op = pd.read_csv(PROFILE_DIR / 'daily_op_profile.csv')
    daily_op['date'] = pd.to_datetime(daily_op['date'])
    daily_weights = dict(zip(daily_op['date'], daily_op['norm_weight']))

    return pg_means, pgvpg_means, summer_op_means, op_coefs, daily_weights


# ============================================================================
# Блок 2: Загрузка и обработка фактических данных модемов
# ============================================================================
def load_metered(metered_path):
    """Загрузка CSV с фактическими данными модемов. Возвращает (metered_ids, metered_daily_df)."""
    raw = pd.read_csv(metered_path, sep=';', encoding='utf-8')
    raw.columns = META_COLS + list(raw.columns[len(META_COLS):])

    raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
    raw['total_area'] = pd.to_numeric(raw['total_area'].astype(str).str.strip(), errors='coerce')
    raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce')
    raw['residents_all'] = pd.to_numeric(raw['residents_all'], errors='coerce')

    date_cols = [c for c in raw.columns if re.match(r'\d{2}\.\d{2}\.\d{4}$', str(c))]

    metered_ids = set(raw['account_id'].unique())
    print(f"  Модемы: {len(metered_ids)} потребителей, {len(date_cols)} дат")

    # Парсим даты в формат YYYY-MM-DD
    parsed_dates = [pd.to_datetime(c, format='%d.%m.%Y').strftime('%Y-%m-%d') for c in date_cols]

    # Дневная матрица: account_id + даты
    daily_data = raw[['account_id'] + date_cols].copy()
    daily_data.columns = ['account_id'] + parsed_dates

    # Числовые значения, NaN -> 0
    for col in parsed_dates:
        daily_data[col] = pd.to_numeric(daily_data[col], errors='coerce').fillna(0)

    # Также вернём мету для merge
    meta = raw[['account_id', 'appliance_group', 'heated_area', 'total_area', 'residents']].copy()

    return metered_ids, daily_data, meta, parsed_dates


# ============================================================================
# Блок 3: Загрузка и фильтрация всех потребителей
# ============================================================================
def load_all_consumers(consumers_path):
    """Загрузка all_pobut.xlsx с фильтрацией."""
    raw = pd.read_excel(consumers_path)
    col_names = ['account_id', 'gas_off', 'alternative', 'dacha',
                 'appliance_group', 'profile_no', 'heated_area',
                 'total_area', 'residents', 'residents_all', 'grs']
    raw.columns = col_names[:len(raw.columns)]

    raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
    raw['total_area'] = pd.to_numeric(raw['total_area'].astype(str).str.strip(), errors='coerce')
    raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce')
    raw['residents_all'] = pd.to_numeric(raw['residents_all'], errors='coerce')
    raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()

    print(f"  Всего потребителей: {len(raw)}")

    # Фильтрация
    mask_exclude = raw['gas_off'].notna() | raw['dacha'].notna()
    mask_bad_group = raw['appliance_group'].isin(SKIP_GROUPS)
    mask_total = mask_exclude | mask_bad_group
    consumers = raw[~mask_total].copy()
    print(f"  После фильтрации (газ откл./дачи/плохие группы): {len(consumers)}")

    # Edge cases для ОП
    consumers['has_OP'] = consumers['appliance_group'].str.contains('ОП', na=False)
    for grp in consumers.loc[consumers['has_OP'], 'appliance_group'].unique():
        mask_grp = consumers['appliance_group'] == grp
        valid_area = consumers.loc[
            mask_grp & consumers['heated_area'].notna() & (consumers['heated_area'] > 0), 'heated_area']
        median_area = valid_area.median() if len(valid_area) > 0 else 55.0
        bad_area = mask_grp & (consumers['heated_area'].isna() | (consumers['heated_area'] <= 0))
        n_fix = bad_area.sum()
        if n_fix > 0:
            consumers.loc[bad_area, 'heated_area'] = median_area
            print(f"    {grp}: {n_fix} без площади -> медиана {median_area:.1f}")

    no_res = consumers['residents'].isna() | (consumers['residents'] <= 0)
    consumers.loc[no_res, 'residents'] = 2
    print(f"  Без residents: {no_res.sum()} -> residents=2")

    return consumers


# ============================================================================
# Блок 4: Предикт для unmetered (логика из predict_all_consumers.py)
# ============================================================================
def predict_unmetered(consumers_slim, pg_means, pgvpg_means, summer_op_means, op_coefs, daily_weights):
    """Месячный + дневной предикт для потребителей без модемов."""
    periods_df = pd.DataFrame(PREDICT_PERIODS, columns=['year', 'month'])
    periods_df['days_in_month'] = periods_df.apply(
        lambda r: monthrange(r['year'], r['month'])[1], axis=1)
    periods_df['is_heating'] = periods_df['month'].isin(HEATING_MONTHS)

    consumer_months = consumers_slim.merge(periods_df, how='cross')
    print(f"  Unmetered потребитель x месяц: {len(consumer_months):,}")

    # Vectorized monthly prediction
    predicted = np.zeros(len(consumer_months), dtype=np.float64)

    grp_col = consumer_months['appliance_group'].values
    month_col = consumer_months['month'].values
    year_col = consumer_months['year'].values
    area_col = consumer_months['heated_area'].values.astype(np.float64)
    res_col = consumer_months['residents'].values.astype(np.float64)
    is_heating_col = consumer_months['is_heating'].values
    has_op_col = consumer_months['has_OP'].values

    # ПГ
    pg_map = np.array([pg_means.get(m, 7.5) for m in range(1, 13)])
    mask_pg = grp_col == 'ПГ'
    predicted[mask_pg] = pg_map[month_col[mask_pg] - 1]

    # ВПГ
    vpg_diff = np.array([max(0, pgvpg_means.get(m, 20.0) - pg_means.get(m, 7.5)) for m in range(1, 13)])
    mask_vpg = grp_col == 'ВПГ'
    predicted[mask_vpg] = vpg_diff[month_col[mask_vpg] - 1]

    # ПГ,ВПГ
    pgvpg_map = np.array([pgvpg_means.get(m, 20.0) for m in range(1, 13)])
    mask_pgvpg = grp_col == 'ПГ,ВПГ'
    predicted[mask_pgvpg] = pgvpg_map[month_col[mask_pgvpg] - 1]

    # ОП лето
    mask_op_summer = has_op_col & ~is_heating_col
    if mask_op_summer.any():
        for grp_name in np.unique(grp_col[mask_op_summer]):
            summer_grp = SUMMER_FALLBACK.get(grp_name, 'ОП,ПГ,ВПГ')
            mask_this = mask_op_summer & (grp_col == grp_name)
            for m in range(5, 10):
                mask_m = mask_this & (month_col == m)
                if mask_m.any():
                    key = (summer_grp, m)
                    val = summer_op_means.get(key,
                          summer_op_means.get(('ОП,ПГ,ВПГ', m), pgvpg_means.get(m, 20.0)))
                    predicted[mask_m] = val

    # ОП зима
    mask_op_winter = has_op_col & is_heating_col
    if mask_op_winter.any():
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
                    ca, cr = None, None
                    for alt_y in [2025, 2024, 2026]:
                        alt_key = (cg, alt_y, m)
                        if alt_key in op_coefs:
                            ca, cr = op_coefs[alt_key]
                            break
                    if ca is None:
                        ca, cr = 1.5, 20.0
                predicted[mask_this] = np.maximum(0, ca * area_col[mask_this] + cr * res_col[mask_this])

    # Остальные -> fallback к ПГ
    mask_other = ~(mask_pg | mask_vpg | mask_pgvpg | has_op_col)
    if mask_other.any():
        predicted[mask_other] = pg_map[month_col[mask_other] - 1]

    consumer_months['predicted_month'] = predicted

    # Дневное распределение
    all_dates = []
    for y, m in PREDICT_PERIODS:
        n_days = monthrange(y, m)[1]
        for d in range(1, n_days + 1):
            all_dates.append(pd.Timestamp(y, m, d))
    all_dates = sorted(all_dates)

    month_daily_info = {}
    col_offset = 0
    for y, m in PREDICT_PERIODS:
        n_days = monthrange(y, m)[1]
        month_dates = [pd.Timestamp(y, m, d) for d in range(1, n_days + 1)]
        op_w = np.array([daily_weights.get(dt, 1.0 / n_days) for dt in month_dates])
        op_w_sum = op_w.sum()
        if op_w_sum > 0:
            op_w = op_w / op_w_sum
        month_daily_info[(y, m)] = {
            'col_start': col_offset,
            'n_days': n_days,
            'op_weights': op_w,
            'uniform_w': 1.0 / n_days,
        }
        col_offset += n_days

    n_dates = len(all_dates)
    n_c = len(consumers_slim)
    n_p = len(PREDICT_PERIODS)

    result_matrix = np.zeros((n_c, n_dates), dtype=np.float32)
    has_op_arr = consumers_slim['has_OP'].values
    pred_values = consumer_months['predicted_month'].values.reshape(n_c, n_p)

    for j, (y, m) in enumerate(PREDICT_PERIODS):
        info = month_daily_info[(y, m)]
        cs = info['col_start']
        nd = info['n_days']
        pred_col = pred_values[:, j]

        if m in HEATING_MONTHS:
            op_w = info['op_weights']
            uw = info['uniform_w']
            result_matrix[:, cs:cs+nd] = pred_col[:, np.newaxis] * uw
            op_mask = has_op_arr
            if op_mask.any():
                result_matrix[op_mask, cs:cs+nd] = pred_col[op_mask, np.newaxis] * op_w[np.newaxis, :]
        else:
            uw = info['uniform_w']
            result_matrix[:, cs:cs+nd] = pred_col[:, np.newaxis] * uw

    date_columns = [dt.strftime('%Y-%m-%d') for dt in all_dates]
    return result_matrix, date_columns


# ============================================================================
# Блок 5: Формирование фактической матрицы модемов
# ============================================================================
def build_metered_matrix(metered_daily, metered_meta, all_consumers, date_columns):
    """Строит матрицу фактических данных для модемных потребителей, выровненную по date_columns."""
    # Фильтруем: оставляем только тех модемных, кто прошёл фильтрацию all_consumers
    valid_ids = set(all_consumers['account_id'])
    metered_filtered = metered_daily[metered_daily['account_id'].isin(valid_ids)].copy()
    metered_meta_filtered = metered_meta[metered_meta['account_id'].isin(valid_ids)].copy()

    print(f"  Модемы после фильтрации: {len(metered_filtered)}")

    # Создаём матрицу: строки = потребители, колонки = date_columns
    # Нужные даты из date_columns, данные из metered_filtered
    available_dates = [c for c in date_columns if c in metered_filtered.columns]
    missing_dates = [c for c in date_columns if c not in metered_filtered.columns]

    print(f"  Дат в предикте: {len(date_columns)}, есть в факте: {len(available_dates)}, нет: {len(missing_dates)}")

    result = pd.DataFrame(index=metered_filtered['account_id'])
    for col in date_columns:
        if col in metered_filtered.columns:
            result[col] = metered_filtered.set_index('account_id')[col].values
        else:
            result[col] = 0.0

    result = result.fillna(0).values.astype(np.float32)
    account_ids = metered_filtered['account_id'].values

    return account_ids, result


# ============================================================================
# Main
# ============================================================================
def main():
    args = parse_args()

    print("=" * 80)
    print("ПОЛНЫЙ ПРОГНОЗ: факт (модемы) + предикт (остальные)")
    print("=" * 80)

    # --- 1. Загрузка профилей ---
    print("\n[1] Загрузка профилей...")
    pg_means, pgvpg_means, summer_op_means, op_coefs, daily_weights = load_profiles()
    print(f"  pg_means: {len(pg_means)}, pgvpg_means: {len(pgvpg_means)}, "
          f"summer_op_means: {len(summer_op_means)}, op_coefs: {len(op_coefs)}, "
          f"daily_weights: {len(daily_weights)}")

    # --- 2. Загрузка модемов ---
    print("\n[2] Загрузка модемов...")
    metered_ids, metered_daily, metered_meta, metered_dates = load_metered(args.metered)

    # --- 3. Загрузка всех потребителей ---
    print("\n[3] Загрузка всех потребителей...")
    all_consumers = load_all_consumers(args.consumers)

    # --- 4. Разделение на metered / unmetered ---
    print("\n[4] Разделение...")
    metered_consumers = all_consumers[all_consumers['account_id'].isin(metered_ids)].copy()
    unmetered_consumers = all_consumers[~all_consumers['account_id'].isin(metered_ids)].copy()
    print(f"  Metered (факт): {len(metered_consumers)}")
    print(f"  Unmetered (предикт): {len(unmetered_consumers)}")

    # --- 5. Предикт для unmetered ---
    print("\n[5] Предикт для unmetered...")
    unmetered_slim = unmetered_consumers[
        ['account_id', 'appliance_group', 'heated_area', 'residents', 'grs', 'has_OP']
    ].reset_index(drop=True)

    predict_matrix, date_columns = predict_unmetered(
        unmetered_slim, pg_means, pgvpg_means, summer_op_means, op_coefs, daily_weights)
    print(f"  Predict matrix: {predict_matrix.shape}")

    # --- 6. Фактические данные модемов ---
    print("\n[6] Формирование факта модемов...")
    metered_account_ids, metered_matrix = build_metered_matrix(
        metered_daily, metered_meta, all_consumers, date_columns)
    print(f"  Metered matrix: {metered_matrix.shape}")

    # --- 7. Объединение ---
    print("\n[7] Объединение...")

    # Мета для metered
    metered_meta_full = all_consumers[all_consumers['account_id'].isin(metered_account_ids)].copy()
    metered_meta_full = metered_meta_full.set_index('account_id').loc[metered_account_ids].reset_index()
    metered_meta_df = metered_meta_full[['account_id', 'appliance_group', 'heated_area', 'residents', 'grs']].copy()
    metered_meta_df['source'] = 'fact'

    # Мета для unmetered
    unmetered_meta_df = unmetered_slim[['account_id', 'appliance_group', 'heated_area', 'residents', 'grs']].copy()
    unmetered_meta_df['source'] = 'predict'

    # Объединяем мету
    combined_meta = pd.concat([metered_meta_df, unmetered_meta_df], ignore_index=True)

    # Объединяем матрицы
    combined_matrix = np.vstack([metered_matrix, predict_matrix])

    print(f"  Итого: {len(combined_meta)} потребителей x {combined_matrix.shape[1]} дней")
    print(f"    source=fact: {(combined_meta['source'] == 'fact').sum()}")
    print(f"    source=predict: {(combined_meta['source'] == 'predict').sum()}")

    # --- 8. Сохранение ---
    print("\n[8] Сохранение...")

    # 8a. Parquet: полная дневная матрица
    result_df = pd.DataFrame(combined_matrix, columns=date_columns)
    output_df = pd.concat([combined_meta.reset_index(drop=True), result_df], axis=1)

    parquet_path = OUTPUT_DIR / 'full_daily_prediction.parquet'
    print(f"  Сохранение {parquet_path}...")
    output_df.to_parquet(parquet_path, index=False, engine='pyarrow')
    print(f"  [OK] {parquet_path.name} ({parquet_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # 8b. CSV: месячные суммы
    print("  Месячные суммы...")
    monthly_records = [{} for _ in range(len(combined_meta))]
    col_offset = 0
    for y, m in PREDICT_PERIODS:
        n_days = monthrange(y, m)[1]
        month_totals = combined_matrix[:, col_offset:col_offset + n_days].sum(axis=1)
        col_name = f"{y}-{m:02d}"
        for i in range(len(combined_meta)):
            monthly_records[i][col_name] = round(float(month_totals[i]), 2)
        col_offset += n_days

    monthly_df = pd.DataFrame(monthly_records)
    monthly_output = pd.concat([combined_meta.reset_index(drop=True), monthly_df], axis=1)

    monthly_path = OUTPUT_DIR / 'full_monthly_prediction.csv'
    monthly_output.to_csv(monthly_path, index=False, encoding='utf-8-sig')
    print(f"  [OK] {monthly_path.name}")

    # 8c. GRS-сводка
    print("  GRS-сводка...")
    grs_values = combined_meta['grs'].values
    unique_grs = np.unique(grs_values[pd.notna(grs_values)])
    source_values = combined_meta['source'].values

    grs_rows = []
    col_offset = 0
    for y, m in PREDICT_PERIODS:
        n_days = monthrange(y, m)[1]
        month_totals = combined_matrix[:, col_offset:col_offset + n_days].sum(axis=1)
        for grs_name in unique_grs:
            grs_mask = grs_values == grs_name
            grs_total = month_totals[grs_mask].sum()
            grs_count = grs_mask.sum()
            fact_mask = grs_mask & (source_values == 'fact')
            predict_mask = grs_mask & (source_values == 'predict')
            grs_rows.append({
                'grs': grs_name,
                'year': y,
                'month': m,
                'total_m3': round(float(grs_total), 2),
                'n_consumers': int(grs_count),
                'n_fact': int(fact_mask.sum()),
                'n_predict': int(predict_mask.sum()),
                'fact_m3': round(float(month_totals[fact_mask].sum()), 2),
                'predict_m3': round(float(month_totals[predict_mask].sum()), 2),
            })
        col_offset += n_days

    grs_summary = pd.DataFrame(grs_rows)
    grs_path = OUTPUT_DIR / 'grs_summary.xlsx'
    grs_summary.to_excel(grs_path, index=False, engine='openpyxl')
    print(f"  [OK] {grs_path.name}")

    # --- Итого ---
    print("\n" + "=" * 80)
    print("ИТОГО")
    print("=" * 80)
    print(f"  Потребителей: {len(combined_meta)}")
    print(f"    Факт (модемы): {(combined_meta['source'] == 'fact').sum()}")
    print(f"    Предикт:       {(combined_meta['source'] == 'predict').sum()}")
    print(f"  Дат: {len(date_columns)}")
    print(f"  Файлы:")
    print(f"    {parquet_path.name}")
    print(f"    {monthly_path.name}")
    print(f"    {grs_path.name}")

    # Суммы по месяцам
    print(f"\n  Суммы по месяцам (тыс. m3):")
    col_offset = 0
    for y, m in PREDICT_PERIODS:
        n_days = monthrange(y, m)[1]
        total = combined_matrix[:, col_offset:col_offset + n_days].sum()
        fact_total = combined_matrix[:len(metered_account_ids), col_offset:col_offset + n_days].sum()
        predict_total = combined_matrix[len(metered_account_ids):, col_offset:col_offset + n_days].sum()
        print(f"    {y}-{m:02d}: {total/1000:.1f} (факт: {fact_total/1000:.1f}, предикт: {predict_total/1000:.1f})")
        col_offset += n_days

    print("\n" + "=" * 80)
    print("ЗАВЕРШЕНО")
    print("=" * 80)


if __name__ == '__main__':
    main()
