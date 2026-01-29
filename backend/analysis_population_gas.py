"""
Аналіз споживання газу населенням — кореляція з температурою

Скрипт розраховує об'єм газу, спожитого населенням, як різницю між загальним
об'ємом лінії (з БД) та об'ємом підприємств (з Excel).
Далі будується кореляція Пірсона та лінійна регресія між температурою
та об'ємом населення для кожної ГРС.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Для роботи без GUI
import matplotlib.pyplot as plt
from sqlalchemy import create_engine, text
from scipy.stats import pearsonr
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score
import json
import warnings
from pathlib import Path
import os
from dotenv import load_dotenv

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 200)

# === Конфігурація ===
# Шляхи відносно розташування скрипта
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = SCRIPT_DIR / 'data'
OUTPUT_DIR = DATA_DIR / 'analysis_output'

# Завантажити змінні з .env файлу
load_dotenv(PROJECT_ROOT / '.env')

# Побудувати DB_URL з змінних оточення
DB_USER = os.getenv('POSTGRES_USER', 'postgres')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'assembler')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('POSTGRES_DB', 'hostlib_db')
DB_URL = f'postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
LINE_ID_FILE = DATA_DIR / 'line_id_2025.xlsx'
VOLUME_FILE = DATA_DIR / 'volume_2025.xlsx'

VIRTUAL_LINES_FILE = DATA_DIR / 'virtual_lines.json'
WEATHER_FILE = DATA_DIR / 'weather_2025_daily_contractual.csv'
TRAIN_START = pd.to_datetime('2025-01-01')
TRAIN_END = pd.to_datetime('2025-12-31')
TEST_START = pd.to_datetime('2026-01-01')
TEST_END = pd.to_datetime('2026-01-31')

# Створити директорію для виводу
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(DB_URL)
print('DB engine створено:', engine.url)

# ============================================================================
# Блок 2 — Маппінг ГРС → line_id (з підтримкою віртуальних ліній)
# ============================================================================

# Цільові ГРС (1003 — Вольнянськ, 1004 — Новомиколаївка; це віртуальні лінії)
TARGET_LINE_IDS = [1003, 1004, 16, 20, 21, 22, 23, 24, 25]

# --- Завантаження конфігурації віртуальних ліній ---
VIRTUAL_LINES = {}  # {virtual_id: [physical_ids]}
vl_data = {}
if VIRTUAL_LINES_FILE.exists():
    with open(VIRTUAL_LINES_FILE, 'r', encoding='utf-8') as f:
        vl_data = json.load(f)
    for vid_str, info in vl_data.get('virtual_lines', {}).items():
        vid = int(vid_str)
        VIRTUAL_LINES[vid] = info['physical_line_ids']
    print(f'Завантажено {len(VIRTUAL_LINES)} віртуальних ліній:')
    for vid, phys in VIRTUAL_LINES.items():
        print(f'  {vid} → фізичні лінії {phys}')
else:
    print(f'⚠ Файл віртуальних ліній не знайдено: {VIRTUAL_LINES_FILE}')

# Розширений набір line_id для фільтрації Excel (цільові + фізичні компоненти віртуальних)
expanded_line_ids = set(TARGET_LINE_IDS)
for vid in TARGET_LINE_IDS:
    if vid >= 1000 and vid in VIRTUAL_LINES:
        expanded_line_ids.update(VIRTUAL_LINES[vid])

# Завантаження маппінгу з Excel (без заголовка, 2 колонки: назва ГРС, line_id)
grs_map_df = pd.read_excel(LINE_ID_FILE, sheet_name='line_id', header=None, names=['grs_name', 'line_id'])
print(f'\nВсього записів у файлі маппінгу: {len(grs_map_df)}')

# Фільтрація: цільові line_id + фізичні компоненти віртуальних ліній
grs_map_df = grs_map_df[grs_map_df['line_id'].isin(expanded_line_ids)].copy()
grs_map_df = grs_map_df.reset_index(drop=True)

# Словник: назва ГРС → line_id (включає фізичні компоненти для enterprise mapping)
grs_to_lineid = dict(zip(grs_map_df['grs_name'], grs_map_df['line_id']))
# Зворотній словник: line_id → назва ГРС
lineid_to_grs = dict(zip(grs_map_df['line_id'], grs_map_df['grs_name']))

# Додати імена віртуальних ліній з JSON (якщо відсутні в Excel)
for vid in TARGET_LINE_IDS:
    if vid >= 1000 and vid not in lineid_to_grs:
        vid_str = str(vid)
        if vid_str in vl_data.get('virtual_lines', {}):
            vl_name = vl_data['virtual_lines'][vid_str].get('name', f'Virtual_{vid}')
            grs_name = f'ГРС {vl_name}'
            lineid_to_grs[vid] = grs_name
            grs_to_lineid[grs_name] = vid
            print(f'  Додано віртуальну лінію з JSON: {grs_name} → line_id={vid}')

print(f'\nЦільових ГРС: {len(TARGET_LINE_IDS)}')
for lid in TARGET_LINE_IDS:
    virt_label = ' [virtual]' if lid >= 1000 else ''
    print(f'  {lineid_to_grs.get(lid, "?")} → line_id={lid}{virt_label}')

# Перевірка дублікатів назв
target_map = grs_map_df[grs_map_df['line_id'].isin(TARGET_LINE_IDS)]
dup_names = target_map['grs_name'].duplicated(keep=False)
if dup_names.any():
    print('\n⚠ Знайдено дублікати назв ГРС:')
    print(target_map[dup_names])
else:
    print('\nДублікатів назв ГРС не знайдено.')

# ============================================================================
# Блок 3 — Об'єми підприємств (Excel)
# ============================================================================

# Завантаження volume_2025.xlsx, лист TDSheet
vol_raw = pd.read_excel(VOLUME_FILE, sheet_name='TDSheet')
print(f'Розмір: {vol_raw.shape}')
print(f'Колонки (перші 5): {list(vol_raw.columns[:5])}')
print(f'Колонки (останні 5): {list(vol_raw.columns[-5:])}')

# Перейменування перших 3 колонок
cols = list(vol_raw.columns)
cols[0] = 'enterprise_name'
cols[1] = 'enterprise_line'
cols[2] = 'grs'
vol_raw.columns = cols

# Парсинг колонок дат: формат 'DD.MM.YYYY г.' або 'DD.MM.YYYY р.'
date_cols = []
date_map = {}  # col_name → datetime
for c in vol_raw.columns[3:]:
    c_str = str(c).strip()
    # Обробка формату 'DD.MM.YYYY г.' (українська) або 'DD.MM.YYYY р.' (російська)
    if c_str.endswith('\u0433.') or c_str.endswith(' р.'):
        date_str = c_str.replace('\u0433.', '').replace(' р.', '').strip()
        try:
            dt = pd.to_datetime(date_str, format='%d.%m.%Y')
            date_cols.append(c)
            date_map[c] = dt
        except ValueError:
            pass

print(f'Знайдено дат-колонок: {len(date_cols)}')
if len(date_cols) > 0:
    print(f'Перша дата: {date_map[date_cols[0]].date()}')
    print(f'Остання дата: {date_map[date_cols[-1]].date()}')
else:
    print('⚠ Не знайдено жодної колонки з датами!')
    exit(1)

# Фільтрація тільки за ГРС з маппінгу
grs_names_set = set(grs_to_lineid.keys())
vol_filtered = vol_raw[vol_raw['grs'].isin(grs_names_set)].copy()
print(f'Рядків після фільтрації за цільовими ГРС: {len(vol_filtered)}')
print(f'ГРС у відфільтрованих даних: {sorted(vol_filtered["grs"].unique())}')

# Додаємо line_id
vol_filtered['line_id'] = vol_filtered['grs'].map(grs_to_lineid)

# Перетворення wide → long через pd.melt()
ent_long = vol_filtered.melt(
    id_vars=['enterprise_name', 'enterprise_line', 'grs', 'line_id'],
    value_vars=date_cols,
    var_name='date_col',
    value_name='enterprise_volume'
)

# Конвертація назви колонки → дата
ent_long['date'] = pd.to_datetime(ent_long['date_col'].map(date_map)).dt.normalize()
ent_long['enterprise_volume'] = pd.to_numeric(ent_long['enterprise_volume'], errors='coerce').fillna(0)

# Агрегація (sum) по line_id + date
enterprise_daily = (
    ent_long
    .groupby(['line_id', 'date'], as_index=False)['enterprise_volume']
    .sum()
)

# Ремапінг фізичних line_id → віртуальних для enterprise_daily
# (якщо enterprise Excel має дані під фізичними лініями, що входять у віртуальну)
phys_to_virt = {}  # physical_line_id → virtual_line_id
for vid, phys_ids in VIRTUAL_LINES.items():
    if vid in TARGET_LINE_IDS:
        for pid in phys_ids:
            phys_to_virt[pid] = vid

if phys_to_virt:
    remapped = enterprise_daily['line_id'].map(phys_to_virt)
    has_remap = remapped.notna()
    if has_remap.any():
        enterprise_daily.loc[has_remap, 'line_id'] = remapped[has_remap].astype(int)
        # Ре-агрегація після ремапінгу (можуть бути кілька фізичних ліній → одна віртуальна)
        enterprise_daily = (
            enterprise_daily
            .groupby(['line_id', 'date'], as_index=False)['enterprise_volume']
            .sum()
        )
        print(f'Ремапінг enterprise: {has_remap.sum()} записів фізичних ліній → віртуальні')

print(f'enterprise_daily: {enterprise_daily.shape}')
print(f'line_id у даних: {sorted(enterprise_daily["line_id"].unique())}')

# ============================================================================
# Блок 4 — Об'єми ліній з БД (з підтримкою віртуальних ліній)
# ============================================================================

# Розділення на фізичні та віртуальні line_id
physical_target_ids = [lid for lid in TARGET_LINE_IDS if lid < 1000]
virtual_target_ids = [lid for lid in TARGET_LINE_IDS if lid >= 1000]

# Зібрати ВСІ фізичні line_id для запиту (включаючи компоненти віртуальних)
all_physical_ids = set(physical_target_ids)
for vid in virtual_target_ids:
    if vid in VIRTUAL_LINES:
        all_physical_ids.update(VIRTUAL_LINES[vid])
    else:
        print(f'⚠ Віртуальна лінія {vid} не знайдена в конфігурації!')
all_physical_ids = sorted(all_physical_ids)

print(f'\nФізичні цільові line_id: {physical_target_ids}')
print(f'Віртуальні цільові line_id: {virtual_target_ids}')
print(f'Усі фізичні line_id для запиту: {all_physical_ids}')

# SQL запит до daily_archive для ВСІХ фізичних line_id
line_ids_str = ', '.join(str(x) for x in all_physical_ids)

sql_lines = text(f"""
    SELECT line_id, period AS date, volume AS line_volume
    FROM daily_archive
    WHERE line_id IN ({line_ids_str})
      AND period BETWEEN '{TRAIN_START.strftime('%Y-%m-%d')}' AND '{TEST_END.strftime('%Y-%m-%d')}'
    ORDER BY line_id, period
""")

with engine.connect() as conn:
    line_volumes_raw = pd.read_sql(sql_lines, conn)

line_volumes_raw['date'] = pd.to_datetime(line_volumes_raw['date']).dt.normalize()
print(f'\nline_volumes (raw з БД): {line_volumes_raw.shape}')
print(f'line_id у БД: {sorted(line_volumes_raw["line_id"].unique())}')

# Агрегація віртуальних ліній (SUM volumes по даті)
virtual_dfs = []
for vid in virtual_target_ids:
    if vid not in VIRTUAL_LINES:
        continue
    phys_ids = VIRTUAL_LINES[vid]
    virt_data = line_volumes_raw[line_volumes_raw['line_id'].isin(phys_ids)].copy()
    if virt_data.empty:
        print(f'⚠ Немає даних для віртуальної лінії {vid} (фізичні: {phys_ids})')
        continue
    # Перевірка наявності всіх фізичних ліній
    found_phys = sorted(virt_data['line_id'].unique())
    missing_phys = set(phys_ids) - set(found_phys)
    if missing_phys:
        print(f'⚠ Віртуальна лінія {vid}: відсутні фізичні лінії {missing_phys} у БД')
    # Агрегація: SUM volume по даті
    virt_agg = virt_data.groupby('date', as_index=False)['line_volume'].sum()
    virt_agg['line_id'] = vid
    virtual_dfs.append(virt_agg)
    print(f'  Віртуальна лінія {vid} ({lineid_to_grs.get(vid, "?")}): '
          f'{len(virt_agg)} днів, фізичні лінії {phys_ids} (знайдено: {found_phys})')

# Фізичні лінії (тільки ті, що є в TARGET, не компоненти віртуальних)
physical_data = line_volumes_raw[line_volumes_raw['line_id'].isin(physical_target_ids)].copy()

# Об'єднуємо фізичні + віртуальні
line_volumes = pd.concat([physical_data] + virtual_dfs, ignore_index=True)
line_volumes = line_volumes.sort_values(['line_id', 'date']).reset_index(drop=True)

print(f'\nline_volumes (з віртуальними): {line_volumes.shape}')
print(f'line_id: {sorted(line_volumes["line_id"].unique())}')

# Перевірка яких line_id немає в результаті
missing_in_result = set(TARGET_LINE_IDS) - set(line_volumes['line_id'].unique())
if missing_in_result:
    print(f'⚠ Наступні line_id відсутні: {missing_in_result}')
    for lid in missing_in_result:
        print(f'  line_id={lid} ({lineid_to_grs.get(lid, "?")})')
else:
    print('Всі цільові line_id (включаючи віртуальні) присутні.')

# Статистика по кожному line_id
for lid in sorted(line_volumes['line_id'].unique()):
    subset = line_volumes[line_volumes['line_id'] == lid]
    virt_label = ' [virtual]' if lid >= 1000 else ''
    print(f'  line_id={lid} ({lineid_to_grs.get(lid, "?")}){virt_label}: {len(subset)} днів, '
          f'volume range=[{subset["line_volume"].min():.1f}, {subset["line_volume"].max():.1f}]')

# ============================================================================
# Блок 5 — Температура з Meteostat (weather_2025.csv)
# ============================================================================

print(f'\nЗавантаження погодних даних: {WEATHER_FILE}')
temp_df = pd.read_csv(WEATHER_FILE, parse_dates=['date'])
# Нормалізація дат: прибираємо час, залишаємо тільки дату для коректного merge
temp_df['date'] = pd.to_datetime(temp_df['date']).dt.normalize()
print(f'Записів температури: {len(temp_df)}')
print(f'Діапазон дат: {temp_df["date"].min().date()} .. {temp_df["date"].max().date()}')
print(f'Температура (°C): min={temp_df["temperature"].min():.1f}, max={temp_df["temperature"].max():.1f}, mean={temp_df["temperature"].mean():.1f}')

# Конвертація °C -> K
temp_df['temp_kelvin'] = temp_df['temperature'] + 273.15

# Зберігаємо тільки температурні стовпці для фіч (виключаємо humidity, wind_speed, pressure, cloud_cover)
weather_extra_cols = [
    'temperature_min', 'temperature_max',
    'temp_range', 'temp_hourly_std', 'hdh_18c', 'hdh_15c',
    'hours_below_0c'
]
weather_extra_cols = [c for c in weather_extra_cols if c in temp_df.columns]

temp_daily = temp_df[['date', 'temperature', 'temp_kelvin'] + weather_extra_cols].copy()

print(f'Унікальних днів температури: {len(temp_daily)}')
print(f'temp_kelvin: min={temp_daily["temp_kelvin"].min():.2f}, max={temp_daily["temp_kelvin"].max():.2f}')
if weather_extra_cols:
    print(f'Додаткові погодні стовпці: {weather_extra_cols}')

# ============================================================================
# Блок 6 — Розрахунок об'єму населення
# ============================================================================

# Merge: line_volumes + enterprise_daily по (line_id, date)
merged = line_volumes.merge(enterprise_daily, on=['line_id', 'date'], how='left')
merged['enterprise_volume'] = merged['enterprise_volume'].fillna(0)

# Об'єм населення = лінія − підприємства
merged['population_volume'] = merged['line_volume'] - merged['enterprise_volume']

print(f'merged: {merged.shape}')
print(f'Рядків з population_volume < 0: {(merged["population_volume"] < 0).sum()}')
if (merged['population_volume'] < 0).any():
    neg = merged[merged['population_volume'] < 0]
    print('⚠ Від\'ємні значення population_volume:')
    for lid in neg['line_id'].unique():
        cnt = len(neg[neg['line_id'] == lid])
        print(f'  line_id={lid} ({lineid_to_grs.get(lid, "?")}): {cnt} днів')

# Merge з температурою та погодними даними по date
merge_cols = ['date', 'temp_kelvin', 'temperature'] + weather_extra_cols
analysis = merged.merge(temp_daily[merge_cols], on='date', how='inner')

# ============================================================================
# Feature Engineering - додаткові фічі для моделі
# ============================================================================

print('\n=== Feature Engineering ===')

# Сортування по line_id та date для lag-фіч
analysis = analysis.sort_values(['line_id', 'date']).reset_index(drop=True)

# 1. Часові фічі
analysis['month'] = analysis['date'].dt.month
analysis['day_of_week'] = analysis['date'].dt.dayofweek  # 0=понеділок, 6=неділя
analysis['day_of_month'] = analysis['date'].dt.day
analysis['week_of_year'] = analysis['date'].dt.isocalendar().week
analysis['quarter'] = analysis['date'].dt.quarter
analysis['is_weekend'] = (analysis['day_of_week'] >= 5).astype(int)  # 1 якщо субота/неділя

# Сезон: зима (12,1,2), весна (3,4,5), літо (6,7,8), осінь (9,10,11)
def get_season(month):
    if month in [12, 1, 2]:
        return 1  # зима
    elif month in [3, 4, 5]:
        return 2  # весна
    elif month in [6, 7, 8]:
        return 3  # літо
    else:
        return 4  # осінь

analysis['season'] = analysis['month'].apply(get_season)

# 2. Температурні фічі
# Скользяче середнє температури за 7 днів (для кожної ГРС окремо)
analysis['temp_ma7'] = analysis.groupby('line_id')['temperature'].transform(
    lambda x: x.rolling(window=7, min_periods=1).mean()
)

# Різниця температури з попереднім днем
analysis['temp_diff'] = analysis.groupby('line_id')['temperature'].transform(
    lambda x: x.diff()
)

# Мінімальна температура за останні 3 дні
analysis['temp_min3'] = analysis.groupby('line_id')['temperature'].transform(
    lambda x: x.rolling(window=3, min_periods=1).min()
)

# Температура в квадраті (для нелінійної залежності)
analysis['temp_kelvin_squared'] = analysis['temp_kelvin'] ** 2

# ============================================================================
# Кусочно-линейные температурные фичи (учет физики процесса)
# ============================================================================
# Зона сильного отопления: чем холоднее, тем больше газа (активна при t < 15°C)
analysis['temp_heating'] = np.maximum(0, 15 - analysis['temperature'])

# Переходная зона: 15-25°C (линейная зависимость)
analysis['temp_transition'] = np.clip(analysis['temperature'], 15, 25) - 15

# Летняя зона: выше 25°C газ почти не зависит от температуры (только ГВС)
analysis['temp_summer'] = np.maximum(0, analysis['temperature'] - 25)

# Дополнительные кусочные фичи для разных температурных режимов
analysis['temp_very_cold'] = np.maximum(0, -analysis['temperature'])  # активна при t < 0°C
analysis['temp_cold'] = np.maximum(0, 10 - analysis['temperature'])   # активна при t < 10°C
analysis['temp_moderate'] = np.maximum(0, analysis['temperature'] - 10) * (analysis['temperature'] <= 20)  # 10-20°C

# Фічі на основі мінімальної та максимальної температури
if 'temperature_min' in analysis.columns:
    analysis['temperature_min'] = analysis['temperature_min'].interpolate().fillna(analysis['temperature'])
    analysis['tmin_heating'] = np.maximum(0, 15 - analysis['temperature_min'])
    analysis['tmin_very_cold'] = np.maximum(0, -analysis['temperature_min'])
if 'temperature_max' in analysis.columns:
    analysis['temperature_max'] = analysis['temperature_max'].interpolate().fillna(analysis['temperature'])
# Добовий розмах температури (амплітуда)
if 'temperature_min' in analysis.columns and 'temperature_max' in analysis.columns:
    analysis['temp_range'] = analysis['temperature_max'] - analysis['temperature_min']

print(f'\nДодано кусочно-лінійні температурні фічі:')
print(f'  temp_heating: max(0, 15-T) - зона вiдопления')
print(f'  temp_transition: clip(T, 15, 25) - 15 - перехідна зона')
print(f'  temp_summer: max(0, T-25) - літня зона')
print(f'  temp_very_cold, temp_cold, temp_moderate - додаткові зони')
print(f'  temperature_min, temperature_max, temp_range - мін/макс/амплітуда')
print(f'  tmin_heating, tmin_very_cold - кусочні фічі від мін. температури')

# 3. Lag-фічі на основі історії споживання
# Споживання 7 днів тому
analysis['pop_volume_lag7'] = analysis.groupby('line_id')['population_volume'].transform(
    lambda x: x.shift(7)
)

# Середнє споживання за останні 7 днів
analysis['pop_volume_ma7'] = analysis.groupby('line_id')['population_volume'].transform(
    lambda x: x.rolling(window=7, min_periods=1).mean().shift(1)  # shift(1) щоб не було витоку даних
)

# 4. Взаємодії фіч
analysis['temp_x_weekend'] = analysis['temperature'] * analysis['is_weekend']
analysis['temp_x_month'] = analysis['temperature'] * analysis['month']

# Заповнення NaN для lag-фіч (перші дні)
analysis['temp_diff'] = analysis['temp_diff'].fillna(0)
analysis['pop_volume_lag7'] = analysis['pop_volume_lag7'].fillna(analysis['population_volume'])
analysis['pop_volume_ma7'] = analysis['pop_volume_ma7'].fillna(analysis['population_volume'])

# 5. Температурні фічі з Meteostat (тільки пов'язані з температурою)
print(f'Додано температурні фічі: {[c for c in weather_extra_cols if c in analysis.columns]}')

# ============================================================================
# Погодинні фічі температури (з контрактної добової агрегації)
# ============================================================================

# Temperature variance from hourly data
if 'temp_hourly_std' in analysis.columns:
    analysis['temp_hourly_std'] = analysis['temp_hourly_std'].fillna(
        analysis['temp_range'] / 4  # Approximation when missing
    )
    print(f'  temp_hourly_std: стандартне відхилення погодинної температури')

# Heating degree hours
if 'hdh_18c' in analysis.columns:
    analysis['hdh_18c'] = analysis['hdh_18c'].fillna(0)
    print(f'  hdh_18c: градусо-години опалення (база 18°C)')

if 'hdh_15c' in analysis.columns:
    analysis['hdh_15c'] = analysis['hdh_15c'].fillna(0)
    print(f'  hdh_15c: градусо-години опалення (база 15°C)')

# Hours below freezing
if 'hours_below_0c' in analysis.columns:
    analysis['hours_below_0c'] = analysis['hours_below_0c'].fillna(0)
    print(f'  hours_below_0c: кількість годин з T < 0°C')

# ============================================================================
# PHASE 1: Quick Wins - Temperature lag features and rolling statistics
# ============================================================================
print('\n=== Phase 1: Temperature Lag Features ===')

# 1. Temperature lag features (1, 3, 7 days ago)
for lag in [1, 3, 7]:
    analysis[f'temp_lag{lag}'] = (
        analysis.groupby('line_id')['temperature']
        .transform(lambda x: x.shift(lag))
    )
    analysis[f'temp_lag{lag}'] = analysis[f'temp_lag{lag}'].fillna(analysis['temperature'])

print(f'  temp_lag1, temp_lag3, temp_lag7: температура 1/3/7 днів тому')

# 2. Heating demand lag features
for lag in [1, 3]:
    analysis[f'temp_heating_lag{lag}'] = (
        analysis.groupby('line_id')['temp_heating']
        .transform(lambda x: x.shift(lag))
    )
    analysis[f'temp_heating_lag{lag}'] = analysis[f'temp_heating_lag{lag}'].fillna(analysis['temp_heating'])

print(f'  temp_heating_lag1, temp_heating_lag3: потреба в опаленні 1/3 дні тому')

# 3. HDH lag features (if available)
if 'hdh_18c' in analysis.columns:
    for lag in [1, 3, 7]:
        analysis[f'hdh_18c_lag{lag}'] = (
            analysis.groupby('line_id')['hdh_18c']
            .transform(lambda x: x.shift(lag))
        )
        analysis[f'hdh_18c_lag{lag}'] = analysis[f'hdh_18c_lag{lag}'].fillna(analysis['hdh_18c'])
    print(f'  hdh_18c_lag1, hdh_18c_lag3, hdh_18c_lag7: градусо-години 1/3/7 днів тому')

# 4. Temperature acceleration (2nd derivative - rate of change)
analysis['temp_accel'] = (
    analysis.groupby('line_id')['temp_diff']
    .transform(lambda x: x.diff())
)
analysis['temp_accel'] = analysis['temp_accel'].fillna(0)
print(f'  temp_accel: прискорення зміни температури (2-га похідна)')

# 5. Maximum temperature change over rolling windows
for window in [3, 7]:
    analysis[f'temp_max_change_{window}d'] = (
        analysis.groupby('line_id')['temp_diff']
        .transform(lambda x: x.rolling(window=window, min_periods=1).apply(
            lambda vals: vals.max() - vals.min() if len(vals) > 0 else 0
        ))
    )
print(f'  temp_max_change_3d, temp_max_change_7d: максимальна зміна температури за вікно')

print('\n=== Phase 1: Rolling Statistics ===')

# 6. Rolling averages for different windows
for window in [3, 14, 30]:
    analysis[f'temp_ma{window}'] = (
        analysis.groupby('line_id')['temperature']
        .transform(lambda x: x.rolling(window=window, min_periods=1).mean())
    )
print(f'  temp_ma3, temp_ma14, temp_ma30: ковзне середнє за 3/14/30 днів')

# 7. Rolling standard deviations (temperature variability)
for window in [3, 7, 14]:
    analysis[f'temp_std{window}'] = (
        analysis.groupby('line_id')['temperature']
        .transform(lambda x: x.rolling(window=window, min_periods=1).std())
    )
    analysis[f'temp_std{window}'] = analysis[f'temp_std{window}'].fillna(0)
print(f'  temp_std3, temp_std7, temp_std14: стандартне відхилення температури за вікно')

# 8. Rolling min/max and range
for window in [7, 14]:
    analysis[f'temp_min{window}'] = (
        analysis.groupby('line_id')['temperature']
        .transform(lambda x: x.rolling(window=window, min_periods=1).min())
    )
    analysis[f'temp_max{window}'] = (
        analysis.groupby('line_id')['temperature']
        .transform(lambda x: x.rolling(window=window, min_periods=1).max())
    )
    analysis[f'temp_range{window}'] = (
        analysis[f'temp_max{window}'] - analysis[f'temp_min{window}']
    )
print(f'  temp_min7, temp_max7, temp_range7: мін/макс/амплітуда за 7 днів')
print(f'  temp_min14, temp_max14, temp_range14: мін/макс/амплітуда за 14 днів')

# 9. Deviation from rolling mean (temperature anomaly)
analysis['temp_deviation_ma7'] = analysis['temperature'] - analysis['temp_ma7']
analysis['temp_deviation_ma30'] = analysis['temperature'] - analysis['temp_ma30']
print(f'  temp_deviation_ma7, temp_deviation_ma30: відхилення від ковзного середнього')

# 10. Z-score (how many std deviations from mean)
analysis['temp_zscore_7d'] = (
    (analysis['temperature'] - analysis['temp_ma7']) /
    (analysis['temp_std7'] + 1e-6)  # +epsilon to avoid division by zero
)
analysis['temp_zscore_14d'] = (
    (analysis['temperature'] - analysis['temp_ma14']) /
    (analysis['temp_std14'] + 1e-6)
)
print(f'  temp_zscore_7d, temp_zscore_14d: z-score (скільки σ від середнього)')

print('\n=== Phase 1: Max Temperature Features ===')

# 11. Features derived from max temperature
if 'temperature_max' in analysis.columns:
    # Piecewise features from max temperature
    analysis['tmax_heating'] = np.maximum(0, 15 - analysis['temperature_max'])
    analysis['tmax_summer'] = np.maximum(0, analysis['temperature_max'] - 25)

    # Temperature asymmetry (day warming)
    analysis['temp_asymmetry'] = analysis['temperature_max'] - analysis['temperature_min']

    # Daytime warming relative to average
    analysis['daytime_warming'] = analysis['temperature_max'] - analysis['temperature']

    # Min × Max interaction
    analysis['tmin_x_tmax'] = analysis['temperature_min'] * analysis['temperature_max']

    print(f'  tmax_heating, tmax_summer: кусочні фічі від макс. температури')
    print(f'  temp_asymmetry: добова асиметрія (tmax - tmin)')
    print(f'  daytime_warming: денне прогрівання')
    print(f'  tmin_x_tmax: взаємодія мін × макс')

print(f'\n✅ Phase 1 completed: Added ~35 new temperature features')

# ============================================================================
# PHASE 2: High Impact - Temperature inertia and cumulative features
# ============================================================================
print('\n=== Phase 2: Temperature Inertia & Cumulative Features ===')

# 1. Cumulative Heating Degree Days (accumulated cold over last N days)
for window in [7, 14, 30]:
    analysis[f'cumulative_hdd_{window}d'] = (
        analysis.groupby('line_id')['temp_heating']
        .transform(lambda x: x.rolling(window=window, min_periods=1).sum())
    )
print(f'  cumulative_hdd_7d, 14d, 30d: накопичені градусо-дні холоду')

# 2. Cumulative HDH (accumulated heating degree hours)
if 'hdh_18c' in analysis.columns:
    for window in [7, 14]:
        analysis[f'cumulative_hdh18_{window}d'] = (
            analysis.groupby('line_id')['hdh_18c']
            .transform(lambda x: x.rolling(window=window, min_periods=1).sum())
        )
    print(f'  cumulative_hdh18_7d, 14d: накопичені градусо-години')

# 3. Cold spell length (how many consecutive days below threshold)
def cold_spell_length(temps, threshold=0):
    """Calculate how many consecutive days temperature was below threshold"""
    below = (temps < threshold).astype(int)
    # Cumulative count that resets when temp rises above threshold
    groups = (below != below.shift()).cumsum()
    spell = below * below.groupby(groups).cumsum()
    return spell

analysis['cold_spell_0c'] = (
    analysis.groupby('line_id')['temperature']
    .transform(lambda x: cold_spell_length(x, threshold=0))
)
analysis['cold_spell_minus5c'] = (
    analysis.groupby('line_id')['temperature']
    .transform(lambda x: cold_spell_length(x, threshold=-5))
)
print(f'  cold_spell_0c, cold_spell_minus5c: дні підряд з морозом')

# 4. Warm spell length (consecutive days above threshold)
def warm_spell_length(temps, threshold=15):
    """Calculate how many consecutive days temperature was above threshold"""
    above = (temps > threshold).astype(int)
    groups = (above != above.shift()).cumsum()
    spell = above * above.groupby(groups).cumsum()
    return spell

analysis['warm_spell_15c'] = (
    analysis.groupby('line_id')['temperature']
    .transform(lambda x: warm_spell_length(x, threshold=15))
)
print(f'  warm_spell_15c: дні підряд з теплом (>15°C)')

# 5. Exponentially weighted cumulative HDD (recent days weigh more)
analysis['ewm_hdd_7d'] = (
    analysis.groupby('line_id')['temp_heating']
    .transform(lambda x: x.ewm(span=7, adjust=False).mean() * 7)
)
print(f'  ewm_hdd_7d: експоненційно зважені градусо-дні')

# 6. Night temperature features (if available from hourly data)
if 'temp_night_min' in analysis.columns:
    analysis['night_heating'] = np.maximum(0, 15 - analysis['temp_night_min'])
    print(f'  night_heating: потреба в опаленні на основі нічного мінімуму')

print(f'\n✅ Phase 2 completed: Added ~15 new inertia features')

# ============================================================================
# PHASE 3: Fine-tuning - Temperature zone interactions
# ============================================================================
print('\n=== Phase 3: Temperature Interaction Features ===')

# 1. Cold × Amplitude (unstable cold weather)
analysis['temp_heating_x_range'] = analysis['temp_heating'] * analysis['temp_range']
analysis['temp_very_cold_x_range'] = analysis['temp_very_cold'] * analysis['temp_range']
print(f'  temp_heating_x_range, temp_very_cold_x_range: холод × амплітуда')

# 2. HDH × Variability
if 'hdh_18c' in analysis.columns and 'temp_hourly_std' in analysis.columns:
    analysis['hdh_x_std'] = analysis['hdh_18c'] * analysis['temp_hourly_std']
    print(f'  hdh_x_std: градусо-години × варіабельність')

# 3. Cold × Frost hours
if 'hours_below_0c' in analysis.columns:
    analysis['temp_heating_x_frost_hours'] = (
        analysis['temp_heating'] * analysis['hours_below_0c']
    )
    print(f'  temp_heating_x_frost_hours: опалення × години морозу')

# 4. Min × Average temperature interaction
if 'temperature_min' in analysis.columns:
    analysis['tmin_x_tavg'] = analysis['temperature_min'] * analysis['temperature']
    analysis['tmin_deviation'] = analysis['temperature'] - analysis['temperature_min']
    print(f'  tmin_x_tavg, tmin_deviation: взаємодія мін × середня')

# 5. Quadratic interactions of important features
analysis['temp_heating_squared'] = analysis['temp_heating'] ** 2
if 'hdh_18c' in analysis.columns:
    analysis['hdh_18c_squared'] = analysis['hdh_18c'] ** 2
print(f'  temp_heating_squared, hdh_18c_squared: квадратичні взаємодії')

# 6. Difference between HDH with different bases (shows temperature zone)
if 'hdh_18c' in analysis.columns and 'hdh_15c' in analysis.columns:
    analysis['hdh_diff_18_15'] = analysis['hdh_18c'] - analysis['hdh_15c']
    print(f'  hdh_diff_18_15: різниця між HDH 18°C та 15°C')

print(f'\n✅ Phase 3 completed: Added ~10 new interaction features')

print(f'\n🎯 Total new features added: ~60 temperature features')
print(f'   Phase 1 (Quick Wins): ~35 features')
print(f'   Phase 2 (High Impact): ~15 features')
print(f'   Phase 3 (Fine-tuning): ~10 features')

# ============================================================================
# ГРС-специфічні фічі для єдиної моделі
# ============================================================================
# Середній об'єм для кожної ГРС (нормалізація)
grs_avg = analysis.groupby('line_id')['population_volume'].transform('mean')
analysis['grs_avg_volume'] = grs_avg

# line_id як категоріальну фічу (для лінійної регресії просто числову)
analysis['line_id_feature'] = analysis['line_id']

print(f'analysis з новими фічами: {analysis.shape}')
print(f'Додано {analysis.shape[1] - 8} нових фіч')  # -8 оригінальних колонок
print(f'Місяці: {sorted(analysis["month"].unique())}')
print(f'line_id: {sorted(analysis["line_id"].unique())}')

# Зведена статистика по кожній ГРС
summary_stats = (
    analysis
    .groupby('line_id')
    .agg(
        grs_name=('line_id', lambda x: lineid_to_grs.get(x.iloc[0], '?')),
        days=('population_volume', 'count'),
        pop_vol_mean=('population_volume', 'mean'),
        pop_vol_std=('population_volume', 'std'),
        pop_vol_min=('population_volume', 'min'),
        pop_vol_max=('population_volume', 'max'),
    )
    .round(2)
)
print('\nЗведена статистика по ГРС:')
print(summary_stats)

# ============================================================================
# Блок 6.5 — Видалення викидів (outliers)
# ============================================================================

print('\n=== Видалення викидів ===')
n_before = len(analysis)

# 1. Видалити рядки з population_volume <= 0 (фізично неможливе споживання)
n_negative = (analysis['population_volume'] <= 0).sum()
analysis = analysis[analysis['population_volume'] > 0].copy()
print(f'Видалено population_volume <= 0: {n_negative} рядків')

# 2. Per-GRS IQR фільтр (Tukey method: Q1 - 1.5*IQR .. Q3 + 1.5*IQR)
outlier_mask = pd.Series(False, index=analysis.index)

for lid in sorted(analysis['line_id'].unique()):
    grs_mask = analysis['line_id'] == lid
    vol = analysis.loc[grs_mask, 'population_volume']

    q1 = vol.quantile(0.25)
    q3 = vol.quantile(0.75)
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr

    grs_outliers = grs_mask & ((analysis['population_volume'] < lower) | (analysis['population_volume'] > upper))
    n_out = grs_outliers.sum()
    if n_out > 0:
        print(f'  {lineid_to_grs.get(lid, lid)}: {n_out} викидів (bounds: {lower:.0f}..{upper:.0f})')
    outlier_mask |= grs_outliers

analysis = analysis[~outlier_mask].copy()
n_after = len(analysis)
if n_before > 0:
    print(f'Всього видалено: {n_before - n_after} рядків ({(n_before - n_after) / n_before * 100:.1f}%)')
else:
    print(f'Всього видалено: {n_before - n_after} рядків')
print(f'Залишилось: {n_after} рядків')

# ============================================================================
# Блок 7 — Кореляція Пірсона
# ============================================================================

# Кореляція Пірсона для кожної ГРС
corr_results = []
for lid in sorted(analysis['line_id'].unique()):
    subset = analysis[analysis['line_id'] == lid].dropna(subset=['temp_kelvin', 'population_volume'])
    if len(subset) < 3:
        continue
    r, p = pearsonr(subset['temp_kelvin'], subset['population_volume'])
    corr_results.append({
        'line_id': lid,
        'GRS': lineid_to_grs.get(lid, '?'),
        'Pearson_r': round(r, 4),
        'p_value': round(p, 6),
        'N': len(subset),
        'Significant': 'Yes' if p < 0.05 else 'No'
    })

corr_df = pd.DataFrame(corr_results)
print('\nКореляція Пірсона: температура (K) vs об\'єм населення')
print(corr_df)

# Scatter plots: температура vs об'єм населення для кожної ГРС
unique_lids = sorted(analysis['line_id'].unique())
n_grs = len(unique_lids)

if n_grs == 0:
    print('ПОПЕРЕДЖЕННЯ: Немає даних для побудови графіків (analysis порожній)')
else:
    ncols = 2
    nrows = (n_grs + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows))
    if nrows == 1 and ncols == 1:
        axes = [axes]
    else:
        axes = axes.flatten()

    for i, lid in enumerate(unique_lids):
        ax = axes[i]
        subset = analysis[analysis['line_id'] == lid]
        ax.scatter(subset['temp_kelvin'], subset['population_volume'], alpha=0.4, s=10)
        ax.set_title(f'{lineid_to_grs.get(lid, lid)} (line_id={lid})')
        ax.set_xlabel('Температура (K)')
        ax.set_ylabel('Об\'єм населення')
        # Додати r
        r_row = corr_df[corr_df['line_id'] == lid]
        if not r_row.empty:
            r_val = r_row.iloc[0]['Pearson_r']
            ax.text(0.05, 0.95, f'r = {r_val:.4f}', transform=ax.transAxes, va='top', fontsize=10)

    # Приховати порожні subplot
    for j in range(i + 1, len(axes)):
        axes[j].set_visible(False)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'scatter_temp_vs_population.png', dpi=150, bbox_inches='tight')
    print(f'\nЗбережено графік: {OUTPUT_DIR / "scatter_temp_vs_population.png"}')
    plt.close()

# Heatmap кореляцій
if not corr_df.empty:
    corr_pivot = corr_df.set_index('GRS')['Pearson_r']
    fig, ax = plt.subplots(figsize=(8, 3))
    colors = ['red' if v < 0 else 'blue' for v in corr_pivot.values]
    bars = ax.barh(corr_pivot.index, corr_pivot.values, color=colors, alpha=0.7)
    ax.set_xlabel('Pearson r')
    ax.set_title('Кореляція: температура vs об\'єм населення')
    ax.axvline(x=0, color='black', linewidth=0.5)
    for bar, val in zip(bars, corr_pivot.values):
        ax.text(val, bar.get_y() + bar.get_height() / 2, f' {val:.3f}', va='center', fontsize=9)
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'correlation_heatmap.png', dpi=150, bbox_inches='tight')
    print(f'Збережено графік: {OUTPUT_DIR / "correlation_heatmap.png"}')
    plt.close()
else:
    print('ПОПЕРЕДЖЕННЯ: Немає даних для побудови heatmap кореляцій')

# ============================================================================
# Блок 8 — Лінійна регресія з розширеними фічами
# ============================================================================

# Список фич для единой модели с кусочно-линейными температурными фичами
FEATURE_COLS = [
    'temperature',        # Основная температура (°C) - Meteostat
    'temp_ma7',          # Скользящее среднее 7 дней
    'temp_heating',      # Зона отопления (активна при t < 15°C)
    'temp_transition',   # Переходная зона (15-25°C)
    'temp_summer',       # Летняя зона (t > 25°C)
    'temp_very_cold',    # Очень холодно (t < 0°C)
    'temp_cold',         # Холодно (t < 10°C)
    'day_of_week',       # День недели (0-6)
    'pop_volume_lag7',   # Потребление 7 дней назад
    'pop_volume_ma7',    # Среднее потребление 7 дней
    'line_id_feature',   # ID ГРС
    'grs_avg_volume',    # Средний объем ГРС
]

# Додаткові температурні фічі з Meteostat (тільки температурні)
WEATHER_FEATURE_COLS = [
    'temperature_min', 'temperature_max', 'temp_range',
    'tmin_heating', 'tmin_very_cold',
]
for wf in WEATHER_FEATURE_COLS:
    if wf in analysis.columns:
        FEATURE_COLS.append(wf)

# Додаткові погодинні температурні фічі (з контрактної добової агрегації)
HOURLY_FEATURES = [
    'temp_hourly_std',
    'hdh_18c',
    'hdh_15c',
    'hours_below_0c',
]
for hf in HOURLY_FEATURES:
    if hf in analysis.columns:
        FEATURE_COLS.append(hf)
        print(f'  Додано погодинну фічу: {hf}')

print(f'\n=== Початковий набір фіч: {len(FEATURE_COLS)} фіч ===')
print(f'Фічі: {", ".join(FEATURE_COLS)}')

# ============================================================================
# Greedy Feature Selection на основі MAE
# ============================================================================

from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error

def greedy_feature_selection(X_train, y_train, X_val, y_val, available_features,
                             feature_names, base_features=['temperature'],
                             threshold_pct=0.5, max_features=15, alpha=10.0):
    """
    Жадібний відбір фіч на основі MAE на валідаційній вибірці.

    Parameters:
    -----------
    threshold_pct : float
        Мінімальне покращення MAE у відсотках для додавання фічі (за замовчуванням 0.5%)
    max_features : int
        Максимальна кількість фіч (за замовчуванням 15)
    alpha : float
        Параметр регуляризації Ridge (за замовчуванням 10.0)
    """
    print(f'\n=== Greedy Feature Selection (threshold={threshold_pct}%, max={max_features}, alpha={alpha}) ===')

    # Індекси базових фіч
    selected_indices = [i for i, name in enumerate(feature_names) if name in base_features]
    selected_names = base_features.copy()

    # Індекси доступних фіч (виключаючи вже вибрані)
    remaining_indices = [i for i in range(len(feature_names))
                        if i not in selected_indices]

    # Навчання базової моделі
    model = Ridge(alpha=alpha)
    model.fit(X_train[:, selected_indices], y_train)
    y_pred = np.maximum(0, model.predict(X_val[:, selected_indices]))
    best_mae = mean_absolute_error(y_val, y_pred)

    print(f'\nБазова модель ({len(selected_names)} фіч): MAE = {best_mae:.2f}')
    print(f'  Фічі: {", ".join(selected_names)}')

    iteration = 1
    while remaining_indices and len(selected_indices) < max_features:
        best_improvement = 0
        best_feature_idx = None
        best_feature_mae = best_mae

        # Пробуємо кожну залишкову фічу
        for idx in remaining_indices:
            # Додаємо фічу до вибраних
            trial_indices = selected_indices + [idx]

            # Навчаємо модель
            model = Ridge(alpha=alpha)
            model.fit(X_train[:, trial_indices], y_train)
            y_pred = np.maximum(0, model.predict(X_val[:, trial_indices]))
            mae = mean_absolute_error(y_val, y_pred)

            # Обчислюємо покращення у відсотках
            improvement_pct = (best_mae - mae) / best_mae * 100

            if improvement_pct > best_improvement:
                best_improvement = improvement_pct
                best_feature_idx = idx
                best_feature_mae = mae

        # Якщо знайдено фічу з достатнім покращенням
        if best_feature_idx is not None and best_improvement >= threshold_pct:
            selected_indices.append(best_feature_idx)
            selected_names.append(feature_names[best_feature_idx])
            remaining_indices.remove(best_feature_idx)

            print(f'\nІтерація {iteration}: Додано "{feature_names[best_feature_idx]}"')
            print(f'  MAE: {best_mae:.2f} → {best_feature_mae:.2f} (покращення: {best_improvement:.2f}%)')

            best_mae = best_feature_mae
            iteration += 1
        else:
            if best_feature_idx is not None:
                print(f'\nІтерація {iteration}: Найкраще покращення {best_improvement:.2f}% < {threshold_pct}% (поріг)')
            print(f'Зупинка: немає фіч з достатнім покращенням MAE')
            break

    if len(selected_indices) >= max_features:
        print(f'\nЗупинка: досягнуто максимальну кількість фіч ({max_features})')

    print(f'\n=== Фінальний набір: {len(selected_names)} фіч, MAE = {best_mae:.2f} ===')
    print(f'Вибрані фічі: {", ".join(selected_names)}')

    return selected_indices, selected_names, best_mae

# Підготовка даних з розділенням train/validation/test
analysis_clean = analysis.dropna(subset=['population_volume'] + FEATURE_COLS)

# Розділення: train (Jan-Oct 2025), validation (Nov-Dec 2025), test (Jan 2026)
VAL_START = pd.to_datetime('2025-11-01')
VAL_END = pd.to_datetime('2025-12-31')

train = analysis_clean[(analysis_clean['date'] >= TRAIN_START) & (analysis_clean['date'] < VAL_START)]
validation = analysis_clean[(analysis_clean['date'] >= VAL_START) & (analysis_clean['date'] <= VAL_END)]
test = analysis_clean[(analysis_clean['date'] >= TEST_START) & (analysis_clean['date'] <= TEST_END)]

print(f'\nРозділення даних:')
print(f'  Train:      {TRAIN_START.strftime("%Y-%m-%d")} — {(VAL_START - pd.Timedelta(days=1)).strftime("%Y-%m-%d")} ({len(train)} семплів)')
print(f'  Validation: {VAL_START.strftime("%Y-%m-%d")} — {VAL_END.strftime("%Y-%m-%d")} ({len(validation)} семплів)')
print(f'  Test:       {TEST_START.strftime("%Y-%m-%d")} — {TEST_END.strftime("%Y-%m-%d")} ({len(test)} семплів)')

if len(train) == 0:
    print('\n' + '='*80)
    print('КРИТИЧНА ПОМИЛКА: Тренувальна вибірка порожня!')
    print('='*80)
    print(f'\nДіагностика:')
    print(f'  analysis_clean: {len(analysis_clean)} рядків')
    print(f'  TRAIN_START: {TRAIN_START}')
    print(f'  TRAIN_END: {TRAIN_END}')
    if len(analysis_clean) > 0:
        print(f'  Діапазон дат в analysis_clean: {analysis_clean["date"].min()} — {analysis_clean["date"].max()}')
        print(f'  Типи даних date column: {analysis_clean["date"].dtype}')
        print(f'\nПерші 5 рядків analysis_clean:')
        print(analysis_clean[['date', 'line_id', 'population_volume', 'temperature']].head())
    raise ValueError('Неможливо навчити модель: тренувальна вибірка порожня. Перевірте діапазони дат та формат даних.')

# Застосування Greedy Feature Selection
X_train_all = train[FEATURE_COLS].values
y_train_all = train['population_volume'].values
X_val_all = validation[FEATURE_COLS].values
y_val_all = validation['population_volume'].values
X_test_all = test[FEATURE_COLS].values
y_test_all = test['population_volume'].values

selected_indices, selected_features, val_mae = greedy_feature_selection(
    X_train_all, y_train_all,
    X_val_all, y_val_all,
    FEATURE_COLS, FEATURE_COLS,
    base_features=['temperature'],
    threshold_pct=0.5,
    max_features=15,
    alpha=10.0
)

# Оновлюємо FEATURE_COLS відібраними фічами
FEATURE_COLS = selected_features
print(f'\n=== Навчання фінальної моделі з {len(FEATURE_COLS)} відібраними фічами ===')

# Об'єднуємо train + validation для фінального навчання
train_final = pd.concat([train, validation], ignore_index=True)

X_train = train_final[FEATURE_COLS].values
y_train = train_final['population_volume'].values
X_test = test[FEATURE_COLS].values
y_test = test['population_volume'].values

# Навчання ЄДИНОЇ моделі з Ridge регрессією (посилена регуляризація)
model = Ridge(alpha=10.0)
model.fit(X_train, y_train)

# Прогноз з клипінгом (прибираємо від'ємні значення)
y_pred_train = np.maximum(0, model.predict(X_train))
y_pred_test = np.maximum(0, model.predict(X_test))
print(f'\nЗастосовано клипінг прогнозів: min(y_pred) = 0 (фізичне обмеження)')
print(f'Параметр регуляризації Ridge: alpha = 10.0')

# Метрики общие
mae_test_overall = mean_absolute_error(y_test, y_pred_test)
r2_train_overall = r2_score(y_train, y_pred_train)
r2_test_overall = r2_score(y_test, y_pred_test)

print(f'\n=== ЗАГАЛЬНІ МЕТРИКИ (всі ГРС разом) ===')
print(f'R²_train: {r2_train_overall:.4f}')
print(f'R²_test:  {r2_test_overall:.4f}')
print(f'MAE_test: {mae_test_overall:.2f} м³')
print(f'Різниця R²_train - R²_test: {r2_train_overall - r2_test_overall:.4f}')

# Важность фич (абсолютное значение коэффициентов)
feature_importance_global = dict(zip(FEATURE_COLS, np.abs(model.coef_)))
print(f'\n=== Важливість фіч (глобальна модель) ===')
for feat, importance in sorted(feature_importance_global.items(), key=lambda x: x[1], reverse=True):
    print(f'  {feat}: {importance:.2f}')

# Метрики для каждой ГРС отдельно
print(f'\n=== МЕТРИКИ ДЛЯ КОЖНОЇ ГРС (з єдиної моделі) ===')
reg_results = []
models = {}  # Сохраняем данные для графиков

for lid in sorted(analysis['line_id'].unique()):
    train_grs = train[train['line_id'] == lid]
    test_grs = test[test['line_id'] == lid]

    if len(test_grs) < 1:
        print(f'⚠ line_id={lid}: немає тестових даних, пропуск')
        continue

    X_train_grs = train_grs[FEATURE_COLS].values
    y_train_grs = train_grs['population_volume'].values
    X_test_grs = test_grs[FEATURE_COLS].values
    y_test_grs = test_grs['population_volume'].values

    y_pred_train_grs = np.maximum(0, model.predict(X_train_grs))
    y_pred_test_grs = np.maximum(0, model.predict(X_test_grs))

    mae_grs = mean_absolute_error(y_test_grs, y_pred_test_grs)
    r2_train_grs = r2_score(y_train_grs, y_pred_train_grs)
    r2_test_grs = r2_score(y_test_grs, y_pred_test_grs)

    reg_results.append({
        'line_id': lid,
        'GRS': lineid_to_grs.get(lid, '?'),
        'MAE_test': round(mae_grs, 2),
        'R2_train': round(r2_train_grs, 4),
        'R2_test': round(r2_test_grs, 4),
        'N_train': len(train_grs),
        'N_test': len(test_grs),
    })

    # Сохраняем данные для графиков
    models[lid] = (model, train_grs, test_grs, X_train_grs, X_test_grs, y_train_grs, y_test_grs)

    print(f'{lineid_to_grs.get(lid, lid)} (line_id={lid}): R²_train={r2_train_grs:.4f}, R²_test={r2_test_grs:.4f}, MAE={mae_grs:.2f} м³')

reg_df = pd.DataFrame(reg_results)
print('\n=== Таблиця результатів для кожної ГРС (базова модель) ===')
print(reg_df)

# ============================================================================
# Блок 8.5 — Two-Stage Model: Корректирующие модели для каждой ГРС
# ============================================================================

print('\n' + '='*80)
print('STAGE 2: Навчання корректуючих моделей для кожної ГРС')
print('(корекція застосовується тільки якщо покращує R² на тесті)')
print('='*80)

# Упрощенный набір фіч для корекції (тільки ті, що є в FEATURE_COLS після greedy selection)
CORRECTION_COLS_CANDIDATES = ['temperature', 'temp_ma7', 'temp_heating', 'temp_transition', 'day_of_week']
CORRECTION_COLS = [col for col in CORRECTION_COLS_CANDIDATES if col in FEATURE_COLS]

if len(CORRECTION_COLS) == 0:
    # Якщо жодної фічі-кандидата немає, використовуємо всі відібрані фічі
    CORRECTION_COLS = FEATURE_COLS.copy()
    print(f'\nПОПЕРЕДЖЕННЯ: Жодної фічі з CORRECTION_COLS_CANDIDATES не знайдено в FEATURE_COLS')
    print(f'Використовуємо всі {len(CORRECTION_COLS)} відібраних фіч для корекції')
else:
    print(f'\nФічі для корекції: {CORRECTION_COLS} ({len(CORRECTION_COLS)} фіч)')

correction_models = {}
reg_results_corrected = []
models_corrected = {}

for lid in sorted(analysis['line_id'].unique()):
    train_grs = train[train['line_id'] == lid]
    test_grs = test[test['line_id'] == lid]

    if len(test_grs) < 1:
        continue

    X_train_grs = train_grs[FEATURE_COLS].values
    y_train_grs = train_grs['population_volume'].values
    X_test_grs = test_grs[FEATURE_COLS].values
    y_test_grs = test_grs['population_volume'].values

    # Базовий прогноз від основної моделі (з кліпінгом)
    y_pred_train_base = np.maximum(0, model.predict(X_train_grs))
    y_pred_test_base = np.maximum(0, model.predict(X_test_grs))

    r2_test_base = reg_df[reg_df['line_id'] == lid].iloc[0]['R2_test']
    mae_test_base = reg_df[reg_df['line_id'] == lid].iloc[0]['MAE_test']

    # Навчаємо корректуючу модель на residuals
    residuals_train = y_train_grs - y_pred_train_base

    # Перевіряємо наявність всіх колонок CORRECTION_COLS
    available_correction_cols = [col for col in CORRECTION_COLS if col in train_grs.columns]
    if len(available_correction_cols) == 0:
        # Якщо жодної колонки немає, використовуємо FEATURE_COLS
        available_correction_cols = FEATURE_COLS

    X_train_correction = train_grs[available_correction_cols].values
    X_test_correction = test_grs[available_correction_cols].values

    correction_model = Ridge(alpha=0.1)
    correction_model.fit(X_train_correction, residuals_train)

    correction_train = correction_model.predict(X_train_correction)
    correction_test = correction_model.predict(X_test_correction)

    y_pred_train_corrected = np.maximum(0, y_pred_train_base + correction_train)
    y_pred_test_corrected = np.maximum(0, y_pred_test_base + correction_test)

    mae_grs_corrected = mean_absolute_error(y_test_grs, y_pred_test_corrected)
    r2_train_grs_corrected = r2_score(y_train_grs, y_pred_train_corrected)
    r2_test_grs_corrected = r2_score(y_test_grs, y_pred_test_corrected)

    r2_improvement = r2_test_grs_corrected - r2_test_base

    # Якщо корекція покращує — використовуємо, інакше залишаємо базову модель
    use_correction = r2_improvement > 0

    if use_correction:
        y_pred_test_final = y_pred_test_corrected
        r2_final = r2_test_grs_corrected
        mae_final = mae_grs_corrected
        r2_train_final = r2_train_grs_corrected
        # Зберігаємо модель разом зі списком колонок, які вона використовує
        correction_models[lid] = (correction_model, available_correction_cols)
        status = 'CORRECTED'
    else:
        y_pred_test_final = y_pred_test_base
        r2_final = r2_test_base
        mae_final = mae_test_base
        r2_train_final = reg_df[reg_df['line_id'] == lid].iloc[0]['R2_train']
        status = 'BASE (корекція погіршує)'

    reg_results_corrected.append({
        'line_id': lid,
        'GRS': lineid_to_grs.get(lid, '?'),
        'MAE_test_base': mae_test_base,
        'MAE_test': round(mae_final, 2),
        'R2_test_base': r2_test_base,
        'R2_test': round(r2_final, 4),
        'R2_improvement': round(r2_improvement, 4) if use_correction else 0.0,
        'R2_train': round(r2_train_final, 4),
        'used_correction': use_correction,
        'N_train': len(train_grs),
        'N_test': len(test_grs),
    })

    models_corrected[lid] = (model, correction_model if use_correction else None,
                             train_grs, test_grs,
                             X_train_grs, X_test_grs, y_train_grs, y_test_grs,
                             y_pred_test_final)

    print(f'{lineid_to_grs.get(lid, lid)} (line_id={lid}): [{status}]')
    print(f'  Base:      R²_test={r2_test_base:.4f}, MAE={mae_test_base:.2f}')
    print(f'  Corrected: R²_test={r2_test_grs_corrected:.4f}, MAE={mae_grs_corrected:.2f}')
    print(f'  ΔR²={r2_improvement:+.4f} → {"застосовано корекцію" if use_correction else "залишено базову модель"}')

reg_df_corrected = pd.DataFrame(reg_results_corrected)
print('\n=== Таблиця результатів (найкраща модель для кожної ГРС) ===')
print(reg_df_corrected)

# Статистика використання корекції
n_corrected = reg_df_corrected['used_correction'].sum()
n_base = len(reg_df_corrected) - n_corrected
print(f'\nВикористано корекцію: {n_corrected}/{len(reg_df_corrected)} ГРС')
print(f'Залишено базову модель: {n_base}/{len(reg_df_corrected)} ГРС')

# Загальні метрики — найкраща модель для кожної ГРС
all_y_test = []
all_y_pred_best = []
for lid in models_corrected.keys():
    _, _, _, test_grs, _, _, _, y_test_grs, y_pred_final = models_corrected[lid]
    all_y_test.extend(y_test_grs)
    all_y_pred_best.extend(y_pred_final)

r2_test_overall_best = r2_score(all_y_test, all_y_pred_best)
mae_test_overall_best = mean_absolute_error(all_y_test, all_y_pred_best)

print('\n=== ЗАГАЛЬНЕ ПОРІВНЯННЯ ===')
print(f'{"Модель":<40} {"R²_test":<10} {"MAE_test":<10}')
print('-' * 60)
print(f'{"Базова (Ridge unified)":<40} {r2_test_overall:.4f}    {mae_test_overall:.2f}')
print(f'{"Найкраща (selective correction)":<40} {r2_test_overall_best:.4f}    {mae_test_overall_best:.2f}')
print(f'{"Покращення":<40} {r2_test_overall_best - r2_test_overall:+.4f}   {mae_test_overall_best - mae_test_overall:+.2f}')
print()

# Скільки ГРС досягли R² > 0.75
good_grs = (reg_df_corrected['R2_test'] >= 0.75).sum()
print(f'ГРС з R²_test >= 0.75: {good_grs}/{len(reg_df_corrected)}')
bad_grs = reg_df_corrected[reg_df_corrected['R2_test'] < 0.75]
if len(bad_grs) > 0:
    print(f'ГРС з R²_test < 0.75:')
    for _, row in bad_grs.iterrows():
        print(f'  - {row["GRS"]}: R²={row["R2_test"]:.4f}')

# Графік regression_lines: scatter + крива регресії (температура vs об'єм)
print('\n=== Створення графіка regression_lines ===')

n_grs = len(unique_lids)
ncols = 2
nrows = (n_grs + ncols - 1) // ncols

fig, axes = plt.subplots(nrows, ncols, figsize=(14, 5 * nrows))
axes = axes.flatten()

for i, lid in enumerate(unique_lids):
    ax = axes[i]
    grs_name = lineid_to_grs.get(lid, lid)

    # Всі дані для цієї ГРС (train + test)
    subset = analysis_clean[analysis_clean['line_id'] == lid].copy()
    train_subset = subset[(subset['date'] >= TRAIN_START) & (subset['date'] <= TRAIN_END)]

    # Scatter: фактичні значення
    ax.scatter(subset['temperature'], subset['population_volume'],
               alpha=0.3, s=10, color='steelblue', label='Фактичні дані', zorder=1)

    # Створюємо синтетичний ряд температур для кривої регресії
    temp_range = np.linspace(subset['temperature'].min(), subset['temperature'].max(), 200)

    # Медіанні значення інших фіч (від тренувальних даних цієї ГРС)
    synthetic = pd.DataFrame()
    synthetic['temperature'] = temp_range
    for col in FEATURE_COLS:
        if col == 'temperature':
            continue
        elif col == 'temp_ma7':
            synthetic[col] = temp_range  # ковзне середнє ~ поточна температура
        elif col == 'temp_heating':
            synthetic[col] = np.maximum(0, 15 - temp_range)
        elif col == 'temp_transition':
            synthetic[col] = np.clip(temp_range, 15, 25) - 15
        elif col == 'temp_summer':
            synthetic[col] = np.maximum(0, temp_range - 25)
        elif col == 'temp_very_cold':
            synthetic[col] = np.maximum(0, -temp_range)
        elif col == 'temp_cold':
            synthetic[col] = np.maximum(0, 10 - temp_range)
        elif col == 'temp_moderate':
            synthetic[col] = np.maximum(0, temp_range - 10) * (temp_range <= 20)
        elif col == 'tmin_heating':
            synthetic[col] = np.maximum(0, 15 - (temp_range - 3))  # ~min temp
        elif col == 'tmin_very_cold':
            synthetic[col] = np.maximum(0, -(temp_range - 3))
        elif col == 'temp_range':
            synthetic[col] = train_subset[col].median() if col in train_subset.columns else 6.0
        elif col == 'temp_hourly_std':
            # Approximate as fraction of daily range
            synthetic[col] = train_subset[col].median() if col in train_subset.columns else 2.0
        elif col == 'hdh_18c':
            # Heating degree hours: sum over 24 hours of max(0, 18 - T_hourly)
            synthetic[col] = np.maximum(0, (18 - temp_range) * 24)
        elif col == 'hdh_15c':
            # Heating degree hours with 15°C base
            synthetic[col] = np.maximum(0, (15 - temp_range) * 24)
        elif col == 'hours_below_0c':
            # Hours below freezing: 0 if temp >= 0, otherwise proportional estimate
            synthetic[col] = np.where(temp_range < 0, 24, 0)
        else:
            # Для всіх інших фіч — медіана від тренувальних даних
            if col in train_subset.columns:
                synthetic[col] = train_subset[col].median()
            else:
                synthetic[col] = 0

    X_synthetic = synthetic[FEATURE_COLS].values
    y_synthetic = np.maximum(0, model.predict(X_synthetic))

    # Якщо для цієї ГРС застосована корекція — додаємо її
    if lid in correction_models:
        # Отримуємо модель та її колонки
        correction_model_obj, correction_cols_used = correction_models[lid]

        # Перевіряємо, чи всі необхідні колонки є в synthetic
        missing_cols = [col for col in correction_cols_used if col not in synthetic.columns]
        if missing_cols:
            # Додаємо відсутні колонки (використовуємо медіани з train)
            for col in missing_cols:
                if col in train_subset.columns:
                    synthetic[col] = train_subset[col].median()
                else:
                    synthetic[col] = 0

        corr_cols_synthetic = synthetic[correction_cols_used].values
        correction = correction_model_obj.predict(corr_cols_synthetic)
        y_synthetic_corrected = np.maximum(0, y_synthetic + correction)
        ax.plot(temp_range, y_synthetic, 'r--', linewidth=1.5, alpha=0.5, label='Base model', zorder=2)
        ax.plot(temp_range, y_synthetic_corrected, 'g-', linewidth=2, label='Corrected', zorder=3)
    else:
        ax.plot(temp_range, y_synthetic, 'r-', linewidth=2, label='Base model', zorder=2)

    # R² та модель
    row_grs = reg_df_corrected[reg_df_corrected['line_id'] == lid].iloc[0]
    r2_val = row_grs['R2_test']
    used_corr = row_grs['used_correction']
    model_label = 'corrected' if used_corr else 'base'

    ax.set_title(f'{grs_name} [{model_label}]\nR²_test={r2_val:.4f}')
    ax.set_xlabel('Температура (°C)')
    ax.set_ylabel('Об\'єм населення (м³)')
    ax.legend(fontsize=8, loc='upper right')
    ax.grid(alpha=0.2)

for j in range(i + 1, len(axes)):
    axes[j].set_visible(False)

plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'regression_lines.png', dpi=150, bbox_inches='tight')
print(f'Збережено графік: {OUTPUT_DIR / "regression_lines.png"}')
plt.close()

# Графік важливості фіч (глобальная модель)
print('\n=== Створення графіка важливості фіч ===')
sorted_features = sorted(feature_importance_global.items(), key=lambda x: x[1], reverse=True)

fig, ax = plt.subplots(figsize=(10, 6))
features = [f[0] for f in sorted_features]
importances = [f[1] for f in sorted_features]
ax.barh(features, importances, color='steelblue', alpha=0.7)
ax.set_xlabel('Важливість (абсолютне значення коефіцієнта Ridge регресії)')
ax.set_title('Важливість фіч для прогнозування споживання газу (єдина модель)')
ax.invert_yaxis()
plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'feature_importance.png', dpi=150, bbox_inches='tight')
print(f'Збережено графік: {OUTPUT_DIR / "feature_importance.png"}')
plt.close()

# Графіки: actual vs predicted для тестового періоду (січень 2026)
print('\n=== Створення графіків actual vs predicted ===')

# 1. Загальний графік — найкраща модель (selective correction)
fig, ax = plt.subplots(figsize=(8, 6))
ax.scatter(all_y_test, all_y_pred_best, alpha=0.5, s=20, edgecolors='k', linewidths=0.5, color='steelblue', label='Best model')
ax.plot([min(all_y_test), max(all_y_test)], [min(all_y_test), max(all_y_test)], 'r--', lw=2, label='Ідеальний прогноз')
ax.set_xlabel('Фактичний об\'єм (м³)')
ax.set_ylabel('Прогнозований об\'єм (м³)')
ax.set_title(f'Selective correction (тест: {TEST_START}..{TEST_END})\nR²={r2_test_overall_best:.4f}, MAE={mae_test_overall_best:.0f} м³')
ax.legend()
ax.grid(alpha=0.3)
plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'actual_vs_predicted_overall.png', dpi=150, bbox_inches='tight')
print(f'Збережено графік: {OUTPUT_DIR / "actual_vs_predicted_overall.png"}')
plt.close()

# 2. Графіки для кожної ГРС окремо
n_models = len(models_corrected)
ncols = 2
nrows = (n_models + ncols - 1) // ncols

fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows))
axes = axes.flatten()

for i, (lid, (base_model, corr_model, train_grs, test_grs, X_train, X_test, y_train, y_test, y_pred_final)) in enumerate(sorted(models_corrected.items())):
    ax = axes[i]

    dates = test_grs['date'].values

    # Базовий прогноз
    y_pred_base = np.maximum(0, base_model.predict(X_test))

    row_grs = reg_df_corrected[reg_df_corrected['line_id'] == lid].iloc[0]
    used_corr = row_grs['used_correction']

    ax.plot(dates, y_test, 'b-o', markersize=4, linewidth=2, label='Actual', zorder=3)
    ax.plot(dates, y_pred_base, 'r--s', markersize=3, alpha=0.5, label='Base', zorder=1)
    if used_corr:
        ax.plot(dates, y_pred_final, 'g-^', markersize=3, linewidth=1.5, label='Corrected', zorder=2)

    r2_test_val = row_grs['R2_test']
    mae_test_val = row_grs['MAE_test']
    model_label = 'corrected' if used_corr else 'base'

    ax.set_title(f'{lineid_to_grs.get(lid, lid)} — Січень 2026 [{model_label}]\nR²={r2_test_val:.4f}, MAE={mae_test_val:.0f}')
    ax.set_xlabel('Дата')
    ax.set_ylabel('Об\'єм населення (м³)')
    ax.legend(fontsize=8)
    ax.tick_params(axis='x', rotation=45)

for j in range(i + 1, len(axes)):
    axes[j].set_visible(False)

plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'actual_vs_predicted.png', dpi=150, bbox_inches='tight')
print(f'Збережено графік: {OUTPUT_DIR / "actual_vs_predicted.png"}')
plt.close()

# Діагностика: розподіл помилок на тесті
print('\n=== Діагностика помилок на тесті ===')
for lid, (model, train, test, X_train, X_test, y_train, y_test) in sorted(models.items()):
    y_pred = model.predict(X_test)
    residuals = y_test - y_pred
    print(f'{lineid_to_grs.get(lid, lid)} (line_id={lid}):')
    print(f'  Residuals: mean={residuals.mean():.2f}, std={residuals.std():.2f}, '
          f'min={residuals.min():.2f}, max={residuals.max():.2f}')
    print(f'  MAPE: {(np.abs(residuals) / np.abs(y_test + 1e-9)).mean() * 100:.1f}%')
    print()

# ============================================================================
# Блок 9 — Зведені результати
# ============================================================================

# Фінальна таблиця: GRS, Pearson r, p-value, MAE, R² test (найкраща модель)
final = corr_df[['line_id', 'GRS', 'Pearson_r', 'p_value', 'Significant']].merge(
    reg_df_corrected[['line_id', 'MAE_test_base', 'MAE_test', 'R2_test_base', 'R2_test', 'R2_improvement', 'R2_train', 'used_correction']],
    on='line_id',
    how='left'
)

print('\n' + '=' * 100)
print('ЗВЕДЕНА ТАБЛИЦЯ: Кореляція та регресія — температура vs об\'єм газу населення')
print('(корекція застосована тільки де покращує результат)')
print('=' * 100)
print(final)

# Інтерпретація результатів
print('\n' + '=' * 60)
print('ІНТЕРПРЕТАЦІЯ РЕЗУЛЬТАТІВ')
print('=' * 60)
print()
for _, row in final.iterrows():
    grs = row['GRS']
    r = row['Pearson_r']
    p = row['p_value']
    sig = row['Significant']

    abs_r = abs(r)
    if abs_r >= 0.7:
        strength = 'сильна'
    elif abs_r >= 0.4:
        strength = 'помірна'
    elif abs_r >= 0.2:
        strength = 'слабка'
    else:
        strength = 'дуже слабка'

    direction = 'обернена' if r < 0 else 'пряма'

    print(f'{grs}:')
    print(f'  Кореляція: {strength} {direction} (r={r:.4f}, p={p:.6f}, {"значуща" if sig == "Yes" else "незначуща"})')

    if pd.notna(row.get('R2_test')):
        r2 = row['R2_test']
        mae = row['MAE_test']
        r2_train = row['R2_train']
        used_corr = row.get('used_correction', False)
        model_type = 'corrected' if used_corr else 'base'
        print(f'  Регресія [{model_type}]: R²_test={r2:.4f}, R²_train={r2_train:.4f}, MAE_test={mae:.2f} м³')
        if used_corr:
            r2_base = row.get('R2_test_base', 'N/A')
            improvement = row.get('R2_improvement', 0)
            print(f'  Покращення: Base R²={r2_base:.4f} -> Corrected R²={r2:.4f} (delta={improvement:+.4f})')
    print()

# Експорт у Excel
output_file = DATA_DIR / 'analysis_population_gas_results.xlsx'

feature_imp_df = pd.DataFrame([
    {'Feature': feat, 'Importance': round(importance, 4)}
    for feat, importance in sorted(feature_importance_global.items(), key=lambda x: x[1], reverse=True)
])

overall_metrics_df = pd.DataFrame([{
    'Metric': 'Base Model - R²_train',
    'Value': round(r2_train_overall, 4)
}, {
    'Metric': 'Base Model - R²_test',
    'Value': round(r2_test_overall, 4)
}, {
    'Metric': 'Base Model - MAE_test',
    'Value': round(mae_test_overall, 2)
}, {
    'Metric': 'Best (selective) - R²_test',
    'Value': round(r2_test_overall_best, 4)
}, {
    'Metric': 'Best (selective) - MAE_test',
    'Value': round(mae_test_overall_best, 2)
}, {
    'Metric': 'Improvement - delta R²',
    'Value': round(r2_test_overall_best - r2_test_overall, 4)
}, {
    'Metric': 'Improvement - delta MAE',
    'Value': round(mae_test_overall_best - mae_test_overall, 2)
}, {
    'Metric': 'Train samples',
    'Value': len(train)
}, {
    'Metric': 'Test samples',
    'Value': len(test)
}, {
    'Metric': 'GRS with correction applied',
    'Value': n_corrected
}, {
    'Metric': 'GRS with base model kept',
    'Value': n_base
}, {
    'Metric': 'GRS with R² >= 0.75',
    'Value': good_grs
}])

with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
    final.to_excel(writer, sheet_name='Summary', index=False)
    overall_metrics_df.to_excel(writer, sheet_name='Overall_Metrics', index=False)
    corr_df.to_excel(writer, sheet_name='Correlation', index=False)
    reg_df.to_excel(writer, sheet_name='Regression_Base', index=False)
    reg_df_corrected.to_excel(writer, sheet_name='Regression_Best', index=False)
    feature_imp_df.to_excel(writer, sheet_name='Feature_Importance', index=False)
    enterprise_daily.to_excel(writer, sheet_name='Enterprise_Daily', index=False)

print(f'\n{"="*80}')
print(f'Результати збережено у {output_file}')
print(f'Графіки збережено у директорії {OUTPUT_DIR}/')
print(f'{"="*80}')
print(f'\n=== ПІДСУМОК ===')
print(f'Модель: Ridge з кусочно-лінійними температурними фічами')
print(f'Корекція: застосовується вибірково (тільки де покращує R² на тесті)')
print(f'  Корекція застосована: {n_corrected}/{len(reg_df_corrected)} ГРС')
print(f'  Базова модель збережена: {n_base}/{len(reg_df_corrected)} ГРС')
print(f'\nДані:')
print(f'  Навчальна вибірка: {len(train)} семплів')
print(f'  Тестова вибірка: {len(test)} семплів')
print(f'  Базова модель - фіч: {len(FEATURE_COLS)}')
print(f'  Коррекція - фіч: {len(CORRECTION_COLS)}')
print(f'\nЗагальні метрики:')
print(f'  Базова модель:         R²={r2_test_overall:.4f}, MAE={mae_test_overall:.2f} м³')
print(f'  Selective correction:  R²={r2_test_overall_best:.4f}, MAE={mae_test_overall_best:.2f} м³')
print(f'  Покращення:            delta R²={r2_test_overall_best - r2_test_overall:+.4f}, delta MAE={mae_test_overall_best - mae_test_overall:+.2f} м³')
print(f'\nДосягнуто цілі:')
print(f'  ГРС з R²_test >= 0.75: {good_grs}/{len(reg_df_corrected)}')
if len(bad_grs) > 0:
    print(f'  Проблемні ГРС (R² < 0.75): {", ".join(bad_grs["GRS"].tolist())}')
print(f'\nАналіз завершено успішно!')
