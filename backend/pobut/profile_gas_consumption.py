"""
Профилирование потребления газа бытовыми потребителями.

Подход:
1. Группы с ОП (зима): регрессия consumption = coef_area * area + coef_residents * residents
   coef_residents разбивается на ПГ/ВПГ части по средним из чистых групп.
2. Группы с ОП (лето): средние по (appliance_group, month) из своих групп.
3. Группы без ОП: средние по (appliance_group, month) без разбивки по residents.
"""

import json
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
# Блок 1: Конфигурация
# ============================================================================

SCRIPT_DIR = Path(__file__).parent
INPUT_DIR = SCRIPT_DIR / 'data' / 'input'
OUTPUT_DIR = SCRIPT_DIR / 'data' / 'profiles'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PROFILE_FILE = INPUT_DIR / 'profile_pobut_daily_result.csv'

MIN_DAYS = 90
OUTLIER_QUANTILE = 0.995
RANDOM_STATE = 42
TEST_FRACTION = 0.2

print("=" * 80)
print("ПРОФИЛИРОВАНИЕ ГАЗА - регрессия ОП + средние ПГ/ВПГ")
print("=" * 80)

# ============================================================================
# Блок 2: Загрузка данных
# ============================================================================
print("\n" + "=" * 80)
print("Блок 2: Загрузка данных")
print("=" * 80)

raw = pd.read_csv(PROFILE_FILE, sep=';', encoding='utf-8')
print(f"Загружено строк: {len(raw)}")

meta_cols = ['account_id', 'gas_off', 'alternative', 'dacha',
             'appliance_group', 'profile_no', 'heated_area',
             'total_area', 'residents', 'residents_all', 'device_serial']
raw.columns = meta_cols + list(raw.columns[len(meta_cols):])

raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
raw['total_area'] = pd.to_numeric(raw['total_area'].astype(str).str.strip(), errors='coerce')
raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce')
raw['residents_all'] = pd.to_numeric(raw['residents_all'], errors='coerce')

date_cols = [c for c in raw.columns if re.match(r'\d{2}\.\d{2}\.\d{4}$', str(c))]
print(f"Колонок с датами: {len(date_cols)}, диапазон: {date_cols[0]} - {date_cols[-1]}")

df = raw.melt(id_vars=meta_cols, value_vars=date_cols, var_name='date_str', value_name='consumption')
df['date'] = pd.to_datetime(df['date_str'], format='%d.%m.%Y')
df['consumption'] = pd.to_numeric(df['consumption'], errors='coerce')
df['month'] = df['date'].dt.month
df['year'] = df['date'].dt.year
df['year_month'] = df['date'].dt.to_period('M')

df['has_PG'] = df['appliance_group'].str.contains(r'(?<!В)ПГ', regex=True, na=False)
df['has_VPG'] = df['appliance_group'].str.contains('ВПГ', na=False)
df['has_OP'] = df['appliance_group'].str.contains('ОП', na=False)

print(f"Записей: {len(df):,}, потребителей: {df['account_id'].nunique()}")

group_counts = raw['appliance_group'].value_counts()
print(f"\nГруппы:")
for grp, cnt in group_counts.items():
    print(f"  {grp}: {cnt}")

# ============================================================================
# Блок 3: Очистка данных (продвинутая)
# ============================================================================
print("\n" + "=" * 80)
print("Блок 3: Очистка данных")
print("=" * 80)

before = len(df)
df = df.dropna(subset=['consumption'])
print(f"Удалено NaN: {before - len(df):,}")

# --- 3*: Исключение альтернативного отопления / дач / отключённых от газа ---
flagged_ids = set(raw.loc[
    raw['gas_off'].notna() | raw['alternative'].notna() | raw['dacha'].notna(),
    'account_id'
])
before_c = df['account_id'].nunique()
df = df[~df['account_id'].isin(flagged_ids)]
n_removed = before_c - df['account_id'].nunique()
print(f"3*) Альтернатива/дача/газ откл: {n_removed} потребителей удалено "
      f"(из {len(flagged_ids)} флагов), осталось: {df['account_id'].nunique()}")

# --- 3a: Минимум дней ---
days_per_consumer = df.groupby('account_id')['date'].nunique()
valid_consumers = days_per_consumer[days_per_consumer >= MIN_DAYS].index
before_c = df['account_id'].nunique()
df = df[df['account_id'].isin(valid_consumers)]
removed_days = before_c - df['account_id'].nunique()
print(f"3a) Потребители <{MIN_DAYS} дней: {removed_days} удалено, осталось: {df['account_id'].nunique()}")

# --- 3b: "Нулевые" потребители (модем не работает) ---
# Если >50% дней = 0 потребление, потребитель считается неактивным
zero_stats = df.groupby('account_id').agg(
    total_days=('consumption', 'count'),
    zero_days=('consumption', lambda x: (x == 0).sum()),
    mean_cons=('consumption', 'mean'),
)
zero_stats['zero_pct'] = zero_stats['zero_days'] / zero_stats['total_days']

ZERO_THRESHOLD = 0.50  # >50% нулей = неактивный
zero_consumers = set(zero_stats[zero_stats['zero_pct'] > ZERO_THRESHOLD].index)
before_c = df['account_id'].nunique()
df = df[~df['account_id'].isin(zero_consumers)]
print(f"3b) 'Нулевые' потребители (>{ZERO_THRESHOLD:.0%} нулей): {len(zero_consumers)} удалено, "
      f"осталось: {df['account_id'].nunique()}")

# --- 3c: Обнаружение скачков (аномальные дни) ---
# Для каждого потребителя: если дневное потребление > 5x скользящей медианы за 14 дней -> аномалия
# Или если после ненулевых дней вдруг 7+ нулей подряд -> модем сломался
df = df.sort_values(['account_id', 'date'])

# Скользящая медиана (14 дней) по потребителю
df['rolling_median'] = df.groupby('account_id')['consumption'].transform(
    lambda x: x.rolling(14, min_periods=7, center=True).median()
)

# Аномальные скачки вверх: >5x медианы (и медиана > 0.1 чтобы не делить на ноль)
SPIKE_MULT = 5
df['is_spike'] = (
    (df['consumption'] > df['rolling_median'] * SPIKE_MULT) &
    (df['rolling_median'] > 0.1)
)
n_spikes = df['is_spike'].sum()
print(f"3c) Скачки вверх (>{SPIKE_MULT}x медианы 14д): {n_spikes} дней помечено")

# --- 3d: Серии нулей (модем перестал работать) ---
# Если 7+ нулей подряд, а до этого были ненулевые -> помечаем эту серию
def mark_zero_runs(group):
    """Помечает серии из 7+ нулей подряд."""
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
    # Финальная серия
    if run_len >= 7 and run_start is not None:
        bad[run_start:run_start + run_len] = True

    return pd.Series(bad, index=group.index)

df['is_zero_run'] = df.groupby('account_id', group_keys=False).apply(mark_zero_runs)
n_zero_runs = df['is_zero_run'].sum()
print(f"3d) Серии 7+ нулей подряд: {n_zero_runs} дней помечено")

# --- 3e: Удаляем аномальные дни, потом потребителей с малым остатком ---
df['is_bad'] = df['is_spike'] | df['is_zero_run']
before_rows = len(df)
df_clean = df[~df['is_bad']].copy()
print(f"3e) Удалено аномальных дней: {before_rows - len(df_clean):,}")

# После удаления аномалий проверяем: достаточно ли дней осталось?
clean_days = df_clean.groupby('account_id')['date'].nunique()
still_valid = clean_days[clean_days >= MIN_DAYS].index
before_c = df_clean['account_id'].nunique()
df_clean = df_clean[df_clean['account_id'].isin(still_valid)]
print(f"    Потребители с <{MIN_DAYS} чистых дней: {before_c - df_clean['account_id'].nunique()} удалено")

# --- 3f: Квантильные выбросы по группам ---
df_clean['is_outlier'] = False
for grp in df_clean['appliance_group'].unique():
    mask = df_clean['appliance_group'] == grp
    threshold = df_clean.loc[mask, 'consumption'].quantile(OUTLIER_QUANTILE)
    outlier_mask = mask & (df_clean['consumption'] > threshold)
    df_clean.loc[outlier_mask, 'is_outlier'] = True

n_outliers = df_clean['is_outlier'].sum()
df_clean = df_clean[~df_clean['is_outlier']].copy()
print(f"3f) Квантильные выбросы (>{OUTLIER_QUANTILE}): {n_outliers} удалено")

# --- Итог очистки ---
print(f"\n=== ИТОГ: {len(df_clean):,} записей, {df_clean['account_id'].nunique()} потребителей ===")
print(f"  Было: {raw['account_id'].nunique()}, удалено: {raw['account_id'].nunique() - df_clean['account_id'].nunique()}")

# Очистка вспомогательных колонок
df_clean = df_clean.drop(columns=['rolling_median', 'is_spike', 'is_zero_run', 'is_bad', 'is_outlier'],
                          errors='ignore')

# ============================================================================
# Блок 4: Агрегация в месячные суммы
# ============================================================================
print("\n" + "=" * 80)
print("Блок 4: Месячная агрегация")
print("=" * 80)

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
print(f"Записей: {len(monthly):,}, потребителей: {monthly['account_id'].nunique()}, месяцев: {monthly['year_month'].nunique()}")

# Определяем отопительный/неотопительный сезон
# Май-сентябрь (5-9) = лето, нет отопления
HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
SUMMER_MONTHS = {5, 6, 7, 8, 9}
monthly['is_heating'] = monthly['month_num'].isin(HEATING_MONTHS)

print(f"Отопительный сезон: {sorted(HEATING_MONTHS)}")
print(f"Летний сезон (без ОП): {sorted(SUMMER_MONTHS)}")

# ============================================================================
# Блок 5: Средние по (группа, месяц) — без прописанных
# ============================================================================
print("\n" + "=" * 80)
print("Блок 5: Средние по (группа, месяц)")
print("=" * 80)

# --- Средние по группам без ОП ---
# ПГ: среднее по месяцу (без разбивки по residents — дает +2% максимум)
pg_data = monthly[monthly['appliance_group'] == 'ПГ'].dropna(subset=['consumption_norm'])
pg_means = pg_data.groupby('month_num')['consumption_norm'].mean()
pg_global = pg_data['consumption_norm'].mean()
print(f"ПГ: {pg_data['account_id'].nunique()} потребителей, среднее: {pg_global:.2f} m3/мес")
print(f"  По месяцам: {pg_means.round(2).to_dict()}")

# ПГ,ВПГ: среднее по месяцу
pgvpg_data = monthly[monthly['appliance_group'] == 'ПГ,ВПГ'].dropna(subset=['consumption_norm'])
pgvpg_means = pgvpg_data.groupby('month_num')['consumption_norm'].mean()
pgvpg_global = pgvpg_data['consumption_norm'].mean()
print(f"\nПГ,ВПГ: {pgvpg_data['account_id'].nunique()} потребителей, среднее: {pgvpg_global:.2f} m3/мес")
print(f"  По месяцам: {pgvpg_means.round(2).to_dict()}")

# --- Летние средние для ОП-групп (свои, не из чистых) ---
# Летом ОП не топят -> их летнее потребление = ПГ+ВПГ, но СВОЕ (не из чистых групп)
summer_op_means = {}  # (appliance_group, month) -> median
for grp in monthly[monthly['has_OP']]['appliance_group'].unique():
    grp_summer = monthly[
        (monthly['appliance_group'] == grp) & ~monthly['is_heating']
    ].dropna(subset=['consumption_norm'])
    if len(grp_summer) == 0:
        continue
    for m in grp_summer['month_num'].unique():
        md = grp_summer[grp_summer['month_num'] == m]
        summer_op_means[(grp, m)] = md['consumption_norm'].median()  # median стійкіша до викидів

print(f"\nЛетние медианы ОП-групп (свои):")
for key in sorted(summer_op_means.keys()):
    print(f"  {key[0]}, мес {key[1]}: {summer_op_means[key]:.2f}")

# --- Середні зимові коефіцієнти по ОП-групах (для гібриду травня) ---
# Травень: 40% winter_pred_scaled + 60% summer_median (Модель C)
# Scale: summer_median(grp, 5) / winter_mean(grp)
avg_winter_coef_by_group = {}  # grp -> (coef_area, coef_res)
scale_may_by_group = {}         # grp -> scale factor для травня
for grp in monthly[monthly['has_OP']]['appliance_group'].unique():
    gd = monthly[(monthly['appliance_group'] == grp) & monthly['is_heating']]
    gd = gd.dropna(subset=['consumption_norm', 'residents'])
    gd = gd[(gd['heated_area'] > 0) & (gd['residents'] > 0)]
    if len(gd) < 20:
        continue
    # Одна регресія по всіх зимових даних
    X_all = gd[['heated_area', 'residents']].values
    y_all = gd['consumption_norm'].values
    m_all = LinearRegression(fit_intercept=False)
    m_all.fit(X_all, y_all)
    ca = max(0.0, m_all.coef_[0])
    cr = max(0.0, m_all.coef_[1])
    avg_winter_coef_by_group[grp] = (ca, cr)
    # Scale: summer_median(травень) / winter_mean
    w_mean = gd['consumption_norm'].mean()
    s_may_med = summer_op_means.get((grp, 5))
    if w_mean > 0 and s_may_med is not None:
        scale_may_by_group[grp] = s_may_med / w_mean

print(f"\nСередні зимові коефіцієнти (для гібриду травня):")
for grp, (ca, cr) in sorted(avg_winter_coef_by_group.items()):
    sc = scale_may_by_group.get(grp, 0)
    print(f"  {grp}: coef_area={ca:.4f}  coef_res={cr:.2f}  scale_may={sc:.4f}")

# --- ПГ/ВПГ компоненты для разбивки coef_residents зимой ---
# Нужны для информативной разбивки, используем средние из чистых групп
pg_mean_by_month = pg_means.to_dict()
# ВПГ = ПГ,ВПГ среднее - ПГ среднее
vpg_mean_by_month = {}
for m in pgvpg_means.index:
    vpg_mean_by_month[m] = max(0, pgvpg_means.get(m, 0) - pg_means.get(m, 0))

print(f"\nПГ компонент (среднее/мес): {dict((m, round(v,2)) for m,v in pg_mean_by_month.items())}")
print(f"ВПГ компонент (среднее/мес): {dict((m, round(v,2)) for m,v in vpg_mean_by_month.items())}")


# ============================================================================
# Блок 6: ОП-группы: регрессия зимой, средние летом
# ============================================================================
print("\n" + "=" * 80)
print("Блок 6: ОП-группы (регрессия зимой, средние летом)")
print("=" * 80)

op_profiles = []

for grp in sorted(monthly['appliance_group'].unique()):
    if 'ОП' not in grp:
        continue

    has_pg = bool(re.search(r'(?<!В)ПГ', grp))
    has_vpg = 'ВПГ' in grp
    grp_data = monthly[monthly['appliance_group'] == grp].copy()
    grp_data = grp_data.dropna(subset=['consumption_norm', 'residents'])
    grp_data = grp_data[(grp_data['heated_area'] > 0) & (grp_data['residents'] > 0)]
    n_consumers = grp_data['account_id'].nunique()

    print(f"\n--- {grp}: {n_consumers} потребителей ---")

    for ym in sorted(grp_data['year_month'].unique()):
        ym_data = grp_data[grp_data['year_month'] == ym]
        if len(ym_data) < 10:
            continue

        month_num = ym.month
        year_num = ym.year
        days = ym_data['days_in_month'].iloc[0]
        is_heating = month_num in HEATING_MONTHS

        med_res = ym_data['residents'].median()

        if is_heating:
            # Отопительный сезон: регрессия consumption = a*area + b*residents
            X = ym_data[['heated_area', 'residents']].values
            y = ym_data['consumption_norm'].values

            model = LinearRegression(fit_intercept=False)
            model.fit(X, y)
            coef_area = max(0, model.coef_[0])
            coef_residents = max(0, model.coef_[1])

            y_pred = np.maximum(0, model.predict(X))
            r2 = r2_score(y, y_pred) if len(y) > 2 else 0

            # Разбивка coef_residents на компоненты (информативно)
            pg_est = pg_mean_by_month.get(month_num, pg_global) / med_res if has_pg else 0
            vpg_est = vpg_mean_by_month.get(month_num, 0) / med_res if has_vpg else 0
            op_per_resident = max(0, coef_residents - pg_est - vpg_est)

            op_profiles.append({
                'appliance_group': grp, 'year': year_num, 'month': month_num,
                'days_in_month': days, 'n_consumers': len(ym_data),
                'season': 'heating',
                'coef_area_per_month': round(coef_area, 4),
                'coef_residents_per_month': round(coef_residents, 2),
                'r2': round(r2, 4),
                'pg_per_person_month': round(pg_est, 2) if has_pg else 0,
                'vpg_per_person_month': round(vpg_est, 2) if has_vpg else 0,
                'op_per_person_month': round(op_per_resident, 2),
                'median_area': round(ym_data['heated_area'].median(), 1),
                'median_residents': round(med_res, 1),
                'median_consumption': round(ym_data['consumption_norm'].median(), 1),
                'mean_consumption': round(ym_data['consumption_norm'].mean(), 1),
            })
        else:
            # Лето: нет отопления -> медиана из СВОЕЙ группы (не из чистых)
            summer_median = summer_op_means.get((grp, month_num))
            if summer_median is None:
                summer_median = ym_data['consumption_norm'].median()

            # Травень — гібрид: 40% зимовий predikt (scaled) + 60% літня медіана
            if month_num == 5 and grp in avg_winter_coef_by_group:
                ca_w, cr_w = avg_winter_coef_by_group[grp]
                sc = scale_may_by_group.get(grp, 0)
                med_area = ym_data['heated_area'].median()
                med_res_v = ym_data['residents'].median()
                winter_part = max(0.0, (ca_w * med_area + cr_w * med_res_v) * sc)
                summer_mean = 0.4 * winter_part + 0.6 * summer_median
            else:
                summer_mean = summer_median

            y_pred_arr = np.full(len(ym_data), summer_mean)
            r2 = r2_score(ym_data['consumption_norm'], y_pred_arr) if len(ym_data) > 2 else 0

            op_profiles.append({
                'appliance_group': grp, 'year': year_num, 'month': month_num,
                'days_in_month': days, 'n_consumers': len(ym_data),
                'season': 'summer',
                'coef_area_per_month': 0,
                'coef_residents_per_month': 0,
                'r2': round(r2, 4),
                'summer_mean': round(summer_mean, 2),
                'pg_per_person_month': 0,
                'vpg_per_person_month': 0,
                'op_per_person_month': 0,
                'median_area': round(ym_data['heated_area'].median(), 1),
                'median_residents': round(med_res, 1),
                'median_consumption': round(ym_data['consumption_norm'].median(), 1),
                'mean_consumption': round(ym_data['consumption_norm'].mean(), 1),
            })

op_profiles_df = pd.DataFrame(op_profiles)

# Вывод
for grp in sorted(op_profiles_df['appliance_group'].unique()):
    gp = op_profiles_df[op_profiles_df['appliance_group'] == grp]
    has_pg = bool(re.search(r'(?<!В)ПГ', grp))
    has_vpg = 'ВПГ' in grp

    print(f"\n  {grp} ({gp['n_consumers'].iloc[0]} потреб.):")
    hdr = f"    {'Месяц':<10} {'Сезон':<8} {'m3/m2':>8} {'m3/чел':>8} {'R2':>6}"
    if has_pg: hdr += f" {'ПГ/чел':>8}"
    if has_vpg: hdr += f" {'ВПГ/чел':>8}"
    hdr += f" {'ОП/чел':>8} {'N':>5}"
    print(hdr)

    for _, p in gp.iterrows():
        ym_str = f"{int(p['year'])}-{int(p['month']):02d}"
        season = 'ОТОПЛ' if p['season'] == 'heating' else 'лето'
        line = f"    {ym_str:<10} {season:<8} {p['coef_area_per_month']:>8.4f} {p['coef_residents_per_month']:>8.2f} {p['r2']:>6.3f}"
        if has_pg: line += f" {p['pg_per_person_month']:>8.2f}"
        if has_vpg: line += f" {p['vpg_per_person_month']:>8.2f}"
        line += f" {p['op_per_person_month']:>8.2f} {int(p['n_consumers']):>5}"
        print(line)

# ============================================================================
# Блок 7: Валидация (80/20 split)
# ============================================================================
print("\n" + "=" * 80)
print("Блок 7: Валидация (80/20 split)")
print("=" * 80)

all_consumers = monthly['account_id'].unique()
np.random.seed(RANDOM_STATE)
test_mask = np.random.rand(len(all_consumers)) < TEST_FRACTION
test_ids = set(all_consumers[test_mask])
train_ids = set(all_consumers[~test_mask])
print(f"Train: {len(train_ids)}, Test: {len(test_ids)}")

train_monthly = monthly[monthly['account_id'].isin(train_ids)].copy()
test_monthly = monthly[monthly['account_id'].isin(test_ids)].copy()

# --- Train: средние по (группа, месяц) для не-ОП ---
tr_pg = train_monthly[train_monthly['appliance_group'] == 'ПГ'].dropna(subset=['consumption_norm'])
tr_pg_means = tr_pg.groupby('month_num')['consumption_norm'].mean()
tr_pg_global = tr_pg['consumption_norm'].mean()

tr_pgvpg = train_monthly[train_monthly['appliance_group'] == 'ПГ,ВПГ'].dropna(subset=['consumption_norm'])
tr_pgvpg_means = tr_pgvpg.groupby('month_num')['consumption_norm'].mean()
tr_pgvpg_global = tr_pgvpg['consumption_norm'].mean()

# --- Train: летние средние ОП-групп (свои) ---
tr_summer_op_means = {}
for grp in train_monthly[train_monthly['has_OP']]['appliance_group'].unique():
    grp_s = train_monthly[
        (train_monthly['appliance_group'] == grp) & ~train_monthly['is_heating']
    ].dropna(subset=['consumption_norm'])
    for m in grp_s['month_num'].unique():
        tr_summer_op_means[(grp, m)] = grp_s[grp_s['month_num'] == m]['consumption_norm'].median()

# --- Train: середні зимові коефіцієнти (для гібриду травня у валідації) ---
tr_avg_winter_coef = {}  # grp -> (coef_area, coef_res)
tr_scale_may = {}         # grp -> scale factor
for grp in train_monthly[train_monthly['has_OP']]['appliance_group'].unique():
    gd_w = train_monthly[(train_monthly['appliance_group'] == grp) & train_monthly['is_heating']]
    gd_w = gd_w.dropna(subset=['consumption_norm', 'residents'])
    gd_w = gd_w[(gd_w['heated_area'] > 0) & (gd_w['residents'] > 0)]
    if len(gd_w) < 20:
        continue
    X_w = gd_w[['heated_area', 'residents']].values
    y_w = gd_w['consumption_norm'].values
    m_w = LinearRegression(fit_intercept=False)
    m_w.fit(X_w, y_w)
    tr_avg_winter_coef[grp] = (max(0.0, m_w.coef_[0]), max(0.0, m_w.coef_[1]))
    w_mean = gd_w['consumption_norm'].mean()
    s_may_med = tr_summer_op_means.get((grp, 5))
    if w_mean > 0 and s_may_med is not None:
        tr_scale_may[grp] = s_may_med / w_mean

# --- Train: ОП регрессии (только отопительные месяцы) ---
train_op_models = {}
for grp in sorted(train_monthly['appliance_group'].unique()):
    if 'ОП' not in grp:
        continue
    gd = train_monthly[(train_monthly['appliance_group'] == grp) & train_monthly['is_heating']]
    gd = gd.dropna(subset=['consumption_norm', 'residents'])
    gd = gd[(gd['heated_area'] > 0) & (gd['residents'] > 0)]

    for ym in sorted(gd['year_month'].unique()):
        yd = gd[gd['year_month'] == ym]
        if len(yd) < 10:
            continue
        X = yd[['heated_area', 'residents']].values
        y = yd['consumption_norm'].values
        model = LinearRegression(fit_intercept=False)
        model.fit(X, y)
        train_op_models[(grp, ym)] = (max(0, model.coef_[0]), max(0, model.coef_[1]))

# --- Предсказание ---
def predict_test(row):
    grp = row['appliance_group']
    res = row['residents']
    month = row['month_num']
    ym = row['year_month']

    if pd.isna(res) or res <= 0:
        res = 2

    has_op = 'ОП' in str(grp)

    if has_op:
        is_heating = month in HEATING_MONTHS
        if is_heating:
            # Отопительный: регрессия
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
        else:
            # Лето: медіана з своєї ОП-групи; травень — гібрид
            v = tr_summer_op_means.get((grp, month))
            if v is None:
                return np.nan
            if month == 5 and grp in tr_avg_winter_coef:
                ca_w, cr_w = tr_avg_winter_coef[grp]
                sc = tr_scale_may.get(grp, 0)
                area = row['heated_area']
                if pd.isna(area) or area <= 0:
                    area = row.get('total_area', None)
                    if area is None or pd.isna(area) or area <= 0:
                        area = 50
                winter_part = max(0.0, (ca_w * area + cr_w * res) * sc)
                return 0.4 * winter_part + 0.6 * v
            return v
    elif grp == 'ПГ,ВПГ':
        return tr_pgvpg_means.get(month, tr_pgvpg_global)
    elif grp == 'ПГ':
        return tr_pg_means.get(month, tr_pg_global)
    else:
        return np.nan


test_monthly['predicted'] = test_monthly.apply(predict_test, axis=1)
test_valid = test_monthly.dropna(subset=['consumption_norm', 'predicted']).copy()
test_valid = test_valid[test_valid['predicted'] > 0]

# Метрики
validation_results = []
print(f"\n{'Группа':<20} {'N':>6} {'MAPE':>8} {'MAE':>10} {'R2':>8} {'Avg Act':>10} {'Avg Pred':>10}")
print("-" * 80)

for grp in sorted(test_valid['appliance_group'].unique()):
    gd = test_valid[test_valid['appliance_group'] == grp]
    actual = gd['consumption_norm'].values
    predicted = gd['predicted'].values
    nz = actual > 0.1
    if nz.sum() == 0: continue

    mape = np.mean(np.abs((actual[nz] - predicted[nz]) / actual[nz])) * 100
    mae = mean_absolute_error(actual, predicted)
    r2 = r2_score(actual, predicted) if len(actual) > 1 else 0

    print(f"{grp:<20} {len(gd):>6} {mape:>7.1f}% {mae:>10.2f} {r2:>8.4f} "
          f"{actual.mean():>10.2f} {predicted.mean():>10.2f}")

    validation_results.append({
        'appliance_group': grp, 'n_records': len(gd),
        'n_consumers': gd['account_id'].nunique(),
        'mape': round(mape, 1), 'mae': round(mae, 2), 'r2': round(r2, 4),
        'mean_actual': round(actual.mean(), 2), 'mean_predicted': round(predicted.mean(), 2),
    })

all_a = test_valid['consumption_norm'].values
all_p = test_valid['predicted'].values
nz = all_a > 0.1
overall_mape = np.mean(np.abs((all_a[nz] - all_p[nz]) / all_a[nz])) * 100
overall_mae = mean_absolute_error(all_a, all_p)
overall_r2 = r2_score(all_a, all_p)
print("-" * 80)
print(f"{'ИТОГО':<20} {len(test_valid):>6} {overall_mape:>7.1f}% {overall_mae:>10.2f} {overall_r2:>8.4f} "
      f"{all_a.mean():>10.2f} {all_p.mean():>10.2f}")

# Суммарный R2 по месяцам
agg = test_valid.groupby('year_month').agg(
    total_actual=('consumption_norm', 'sum'),
    total_predicted=('predicted', 'sum'),
).reset_index()
ta = agg['total_actual'].values
tp = agg['total_predicted'].values
agg_r2 = r2_score(ta, tp)
agg_mape = np.mean(np.abs((ta - tp) / ta)) * 100
print(f"\nСуммарный R2: {agg_r2:.4f}, MAPE: {agg_mape:.1f}%, откл: {(tp.sum()-ta.sum())/ta.sum()*100:.1f}%")

# ============================================================================
# Блок 8: Вывод результатов
# ============================================================================
print("\n" + "=" * 80)
print("Блок 8: Вывод")
print("=" * 80)

# 1. profiles.json
pg_means_df = pg_means.reset_index()
pg_means_df.columns = ['month', 'pg_mean']
pgvpg_means_df = pgvpg_means.reset_index()
pgvpg_means_df.columns = ['month', 'pgvpg_mean']
summer_means_df = pd.DataFrame([
    {'appliance_group': k[0], 'month': k[1], 'summer_mean': v}
    for k, v in summer_op_means.items()
])

profiles_json = {
    'pg_means': pg_means_df.to_dict('records'),
    'pgvpg_means': pgvpg_means_df.to_dict('records'),
    'summer_op_means': summer_means_df.to_dict('records') if len(summer_means_df) > 0 else [],
    'op_profiles': [],
}
for _, p in op_profiles_df.iterrows():
    entry = {k: (int(v) if isinstance(v, (np.integer,)) else
                  float(v) if isinstance(v, (np.floating,)) else v)
             for k, v in p.to_dict().items() if pd.notna(v)}
    profiles_json['op_profiles'].append(entry)

json_path = OUTPUT_DIR / 'profiles.json'
with open(json_path, 'w', encoding='utf-8') as f:
    json.dump(profiles_json, f, ensure_ascii=False, indent=2)
print(f"[OK] {json_path}")

# 2. profiles_summary.xlsx
summary_path = OUTPUT_DIR / 'profiles_summary.xlsx'
with pd.ExcelWriter(summary_path, engine='openpyxl') as writer:
    pg_means_df.to_excel(writer, sheet_name='PG_means', index=False)
    pgvpg_means_df.to_excel(writer, sheet_name='PGVPG_means', index=False)
    if len(summer_means_df) > 0:
        summer_means_df.to_excel(writer, sheet_name='Summer_OP_means', index=False)
    op_profiles_df.to_excel(writer, sheet_name='OP_profiles', index=False)
    if validation_results:
        pd.DataFrame(validation_results).to_excel(writer, sheet_name='Validation', index=False)
print(f"[OK] {summary_path}")

# 3. validation_results.xlsx
if len(test_valid) > 0:
    val_path = OUTPUT_DIR / 'validation_results.xlsx'
    ve = test_valid[['account_id', 'appliance_group', 'year_month', 'consumption_norm',
                     'predicted', 'residents', 'heated_area', 'days_count']].copy()
    ve['year_month'] = ve['year_month'].astype(str)
    ve.head(50000).to_excel(val_path, index=False)
    print(f"[OK] {val_path}")

# 4. CSV для применения
pg_means_df.to_csv(OUTPUT_DIR / 'pg_means.csv', index=False)
print(f"[OK] pg_means.csv")
pgvpg_means_df.to_csv(OUTPUT_DIR / 'pgvpg_means.csv', index=False)
print(f"[OK] pgvpg_means.csv")
if len(summer_means_df) > 0:
    summer_means_df.to_csv(OUTPUT_DIR / 'summer_op_means.csv', index=False)
    print(f"[OK] summer_op_means.csv")
op_profiles_df.to_csv(OUTPUT_DIR / 'op_profiles.csv', index=False)
print(f"[OK] op_profiles.csv")

# 5. may_hybrid_coefs.csv — для predict_all_consumers.py
may_hybrid_rows = []
for grp, (ca, cr) in avg_winter_coef_by_group.items():
    sc = scale_may_by_group.get(grp, 0.0)
    s_med = summer_op_means.get((grp, 5), 0.0)
    may_hybrid_rows.append({
        'appliance_group': grp,
        'coef_area': round(ca, 6),
        'coef_res': round(cr, 4),
        'scale_may': round(sc, 6),
        'summer_median_may': round(s_med, 4),
        'may_winter_weight': 0.4,
        'may_summer_weight': 0.6,
    })
may_hybrid_df = pd.DataFrame(may_hybrid_rows)
may_hybrid_df.to_csv(OUTPUT_DIR / 'may_hybrid_coefs.csv', index=False)
print(f"[OK] may_hybrid_coefs.csv")
print(may_hybrid_df.to_string(index=False))

print("\n" + "=" * 80)
print("ЗАВЕРШЕНО")
print("=" * 80)
print(f"\nФормулы для 200K:")
print(f"  ПГ:          расход = ПГ_среднее(month)")
print(f"  ПГ,ВПГ:      расход = ПГВПГ_среднее(month)")
print(f"  ОП (зима):   расход = coef_area*площадь + coef_residents*прож.")
print(f"  ОП (лето):   расход = среднее_ОП_группы(group, month)")
print(f"\nФайлы: pg_means.csv, pgvpg_means.csv, summer_op_means.csv, op_profiles.csv")
