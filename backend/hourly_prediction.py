"""
Прогнозирование потребления газа населением на основе часовых данных

Ключевые изменения:
1. Исключены линии 20 и 22
2. Используется часовой архив по линиям из БД
3. Признаки: температура (hourly), час дня, день недели
4. Объемы предприятий (суточные) делятся на 24
5. Клип по 0 для объемов населения
6. Простая линейная регрессия с подбором фич по R²
7. Валидация на том же тренировочном участке что и обучение
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sqlalchemy import create_engine, text
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score, mean_squared_error
from sklearn.preprocessing import StandardScaler
import json
import warnings
from pathlib import Path
import os
from dotenv import load_dotenv
from datetime import datetime

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 30)
pd.set_option('display.width', 250)

# === Конфигурация ===
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = SCRIPT_DIR / 'data'
OUTPUT_DIR = DATA_DIR / 'hourly_prediction_output'

# Загрузить переменные из .env
load_dotenv(PROJECT_ROOT / '.env')

# Построить DB_URL
DB_USER = os.getenv('POSTGRES_USER', 'postgres')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'assembler')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('POSTGRES_DB', 'hostlib_db')
DB_URL = f'postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'

# Файлы данных
LINE_ID_FILE = DATA_DIR / 'line_id_2025.xlsx'
VOLUME_FILE = DATA_DIR / 'volume_2025.xlsx'
VIRTUAL_LINES_FILE = DATA_DIR / 'virtual_lines.json'
WEATHER_HOURLY_FILE = DATA_DIR / 'weather_2025_h.csv'

# Временные диапазоны (только train, валидация на нем же)
TRAIN_START = pd.to_datetime('2025-01-01')
TRAIN_END = pd.to_datetime('2025-12-31')

# Целевые линии (ИСКЛЮЧЕНЫ 20 и 22)
TARGET_LINE_IDS = [1003, 1004, 16, 21, 23, 24, 25]

# Создать директорию для вывода
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(DB_URL)
print('DB engine создан:', engine.url)
print(f'Целевые линии (без 20 и 22): {TARGET_LINE_IDS}')

# ============================================================================
# Блок 1: Загрузка виртуальных линий
# ============================================================================
VIRTUAL_LINES = {}
if VIRTUAL_LINES_FILE.exists():
    with open(VIRTUAL_LINES_FILE, 'r', encoding='utf-8') as f:
        vl_data = json.load(f)
    for vid_str, info in vl_data.get('virtual_lines', {}).items():
        vid = int(vid_str)
        VIRTUAL_LINES[vid] = info['physical_line_ids']
    print(f'\nЗагружено {len(VIRTUAL_LINES)} виртуальных линий:')
    for vid, phys in VIRTUAL_LINES.items():
        print(f'  {vid} -> физические линии {phys}')

# Расширенный набор line_id
expanded_line_ids = set(TARGET_LINE_IDS)
for vid in TARGET_LINE_IDS:
    if vid >= 1000 and vid in VIRTUAL_LINES:
        expanded_line_ids.update(VIRTUAL_LINES[vid])

# ============================================================================
# Блок 2: Маппинг ГРС -> line_id
# ============================================================================
grs_map_df = pd.read_excel(LINE_ID_FILE, sheet_name='line_id', header=None, names=['grs_name', 'line_id'])
print(f'\nВсего записей в файле маппинга: {len(grs_map_df)}')

grs_map_df = grs_map_df[grs_map_df['line_id'].isin(expanded_line_ids)].copy()
grs_map_df = grs_map_df.reset_index(drop=True)

grs_to_lineid = dict(zip(grs_map_df['grs_name'], grs_map_df['line_id']))
lineid_to_grs = dict(zip(grs_map_df['line_id'], grs_map_df['grs_name']))

print(f'\nМаппинг ГРС -> line_id:')
for grs, lid in sorted(grs_to_lineid.items(), key=lambda x: x[1]):
    print(f'  {grs:30} -> {lid}')

# ============================================================================
# Блок 3: Загрузка часовых данных по линиям из БД
# ============================================================================
print('\n' + '='*80)
print('Загрузка часовых данных по линиям из БД...')
print('='*80)

physical_target_ids = [lid for lid in TARGET_LINE_IDS if lid < 1000]
virtual_target_ids = [lid for lid in TARGET_LINE_IDS if lid >= 1000]

# SQL запрос для физических линий
physical_ids_str = ','.join(map(str, physical_target_ids))
query_physical = f"""
SELECT
    ha.line_id,
    ha.period as datetime,
    ha.volume,
    ha.temperature
FROM hourly_archive ha
INNER JOIN gas_volume_line gvl ON ha.line_id = gvl.id
WHERE gvl.line IN ({physical_ids_str})
  AND ha.period >= '{TRAIN_START}'
  AND ha.period <= '{TRAIN_END}'
ORDER BY ha.line_id, ha.period
"""

print(f'\nЗапрос часовых данных для физических линий: {physical_target_ids}')
with engine.connect() as conn:
    df_physical = pd.read_sql(text(query_physical), conn)

print(f'Загружено {len(df_physical)} записей для физических линий')
print(f'Период: {df_physical["datetime"].min()} - {df_physical["datetime"].max()}')
print(f'Уникальных line_id: {df_physical["line_id"].nunique()}')

# Обработка виртуальных линий
df_virtual_list = []
for vid in virtual_target_ids:
    if vid not in VIRTUAL_LINES:
        print(f'[!] Виртуальная линия {vid} не найдена в конфигурации')
        continue

    phys_ids = VIRTUAL_LINES[vid]
    phys_ids_str = ','.join(map(str, phys_ids))

    query_virt = f"""
    SELECT
        ha.period as datetime,
        SUM(ha.volume) as volume,
        AVG(ha.temperature) as temperature
    FROM hourly_archive ha
    INNER JOIN gas_volume_line gvl ON ha.line_id = gvl.id
    WHERE gvl.line IN ({phys_ids_str})
      AND ha.period >= '{TRAIN_START}'
      AND ha.period <= '{TRAIN_END}'
    GROUP BY ha.period
    ORDER BY ha.period
    """

    with engine.connect() as conn:
        df_virt = pd.read_sql(text(query_virt), conn)

    df_virt['line_id'] = vid
    df_virtual_list.append(df_virt)
    print(f'Виртуальная линия {vid}: {len(df_virt)} записей (агрегация физических {phys_ids})')

# Объединение физических и виртуальных
if df_virtual_list:
    df_virtual = pd.concat(df_virtual_list, ignore_index=True)
    hourly_lines = pd.concat([df_physical, df_virtual], ignore_index=True)
else:
    hourly_lines = df_physical.copy()

hourly_lines['datetime'] = pd.to_datetime(hourly_lines['datetime'])
hourly_lines = hourly_lines.sort_values(['line_id', 'datetime']).reset_index(drop=True)

print(f'\n[OK] Всего часовых записей по линиям: {len(hourly_lines)}')
print(f'[OK] Уникальных line_id: {hourly_lines["line_id"].nunique()}')
print(f'[OK] Диапазон дат: {hourly_lines["datetime"].min()} - {hourly_lines["datetime"].max()}')

# ============================================================================
# Блок 4: Загрузка часовых данных о погоде
# ============================================================================
print('\n' + '='*80)
print('Загрузка часовых данных о погоде...')
print('='*80)

if not WEATHER_HOURLY_FILE.exists():
    print(f'[!] Файл часовой погоды не найден: {WEATHER_HOURLY_FILE}')
    print('Используем температуру из архива линий')
    weather_hourly = None
else:
    weather_hourly = pd.read_csv(WEATHER_HOURLY_FILE)
    # Проверяем название колонки с датой
    date_col = 'date' if 'date' in weather_hourly.columns else 'datetime'
    weather_hourly[date_col] = pd.to_datetime(weather_hourly[date_col])
    # Переименовываем для единообразия
    if date_col == 'date':
        weather_hourly = weather_hourly.rename(columns={'date': 'datetime'})
    print(f'[OK] Загружено {len(weather_hourly)} записей погоды')
    print(f'[OK] Период: {weather_hourly["datetime"].min()} - {weather_hourly["datetime"].max()}')
    print(f'Колонки: {list(weather_hourly.columns)}')

# ============================================================================
# Блок 5: Загрузка данных по предприятиям (суточные)
# ============================================================================
print('\n' + '='*80)
print('Загрузка данных по предприятиям...')
print('='*80)

vol_raw = pd.read_excel(VOLUME_FILE, sheet_name='TDSheet')
print(f'Загружено {len(vol_raw)} строк из {VOLUME_FILE}')

# Обработка заголовков (как в оригинальном скрипте)
cols = list(vol_raw.columns)
cols[0] = 'enterprise_name'
cols[1] = 'enterprise_line'
vol_raw.columns = cols

# Фильтруем строки с данными (пропускаем заголовки)
vol_raw = vol_raw[vol_raw['enterprise_name'].notna()].copy()
vol_raw = vol_raw[vol_raw['enterprise_name'] != 'Підприємство'].copy()

# Добавляем колонку ГРС и line_id
if 'ГРС' not in vol_raw.columns:
    # Пытаемся найти колонку с ГРС
    for col in vol_raw.columns:
        if 'грс' in str(col).lower() or 'grs' in str(col).lower():
            vol_raw = vol_raw.rename(columns={col: 'ГРС'})
            break

# Если нет колонки ГРС, создаем её из enterprise_line
if 'ГРС' not in vol_raw.columns:
    vol_raw['ГРС'] = vol_raw['enterprise_name']

# Маппинг ГРС на line_id
vol_raw['line_id'] = vol_raw['ГРС'].map(grs_to_lineid)

# Определяем колонки с датами
date_cols = []
for col in vol_raw.columns:
    if isinstance(col, datetime):
        date_cols.append(col)
    elif isinstance(col, str) and (col.count('.') == 2 or col.count('/') == 2 or col.count('-') == 2):
        try:
            pd.to_datetime(col)
            date_cols.append(col)
        except:
            pass

print(f'Найдено {len(date_cols)} колонок с датами')

if not date_cols:
    print('[!] Не найдены колонки с датами')
    enterprise_volumes = pd.DataFrame()
else:
    # Преобразуем в длинный формат
    ent_long = vol_raw.melt(
        id_vars=['enterprise_name', 'enterprise_line', 'ГРС', 'line_id'],
        value_vars=date_cols,
        var_name='date',
        value_name='enterprise_volume'
    )

    # Преобразование дат и объемов
    ent_long['date'] = pd.to_datetime(ent_long['date'], errors='coerce')
    ent_long = ent_long.dropna(subset=['date', 'line_id'])
    ent_long['enterprise_volume'] = pd.to_numeric(ent_long['enterprise_volume'], errors='coerce').fillna(0)

    # Агрегация по line_id и date (суточные объемы)
    enterprise_volumes = (
        ent_long
        .groupby(['line_id', 'date'], as_index=False)['enterprise_volume']
        .sum()
        .rename(columns={'enterprise_volume': 'daily_enterprise_volume'})
    )

    # Ремаппинг физических line_id -> виртуальных
    phys_to_virt = {}
    for vid, phys_ids in VIRTUAL_LINES.items():
        for pid in phys_ids:
            phys_to_virt[pid] = vid

    if phys_to_virt:
        remapped = enterprise_volumes['line_id'].map(phys_to_virt)
        has_remap = remapped.notna()
        if has_remap.any():
            enterprise_volumes.loc[has_remap, 'line_id'] = remapped[has_remap].astype(int)
            # Повторная агрегация после ремаппинга
            enterprise_volumes = (
                enterprise_volumes
                .groupby(['line_id', 'date'], as_index=False)['daily_enterprise_volume']
                .sum()
            )
            print(f'Ремаппинг enterprise: {has_remap.sum()} записей физических линий -> виртуальные')

    # Фильтрация по периоду
    enterprise_volumes = enterprise_volumes[
        (enterprise_volumes['date'] >= TRAIN_START) &
        (enterprise_volumes['date'] <= TRAIN_END)
    ]

    print(f'\n[OK] Обработано {len(enterprise_volumes)} суточных записей по предприятиям')
    print(f'[OK] Уникальных line_id: {enterprise_volumes["line_id"].nunique()}')
    if len(enterprise_volumes) > 0:
        print(f'[OK] Период: {enterprise_volumes["date"].min()} - {enterprise_volumes["date"].max()}')
        print(f'[OK] line_id в данных: {sorted(enterprise_volumes["line_id"].unique())}')

# ============================================================================
# Блок 6: Объединение данных и создание признаков
# ============================================================================
print('\n' + '='*80)
print('Объединение данных и создание признаков...')
print('='*80)

# Создаем дату без времени для джойна с предприятиями
hourly_lines['date'] = hourly_lines['datetime'].dt.date
hourly_lines['date'] = pd.to_datetime(hourly_lines['date'])

# Джойним с предприятиями (суточные данные)
if not enterprise_volumes.empty:
    hourly_data = hourly_lines.merge(
        enterprise_volumes,
        on=['line_id', 'date'],
        how='left'
    )
    hourly_data['daily_enterprise_volume'] = hourly_data['daily_enterprise_volume'].fillna(0)

    # ДЕЛИМ СУТОЧНЫЙ ОБЪЕМ ПРЕДПРИЯТИЙ НА 24 для каждого часа
    hourly_data['hourly_enterprise_volume'] = hourly_data['daily_enterprise_volume'] / 24
else:
    hourly_data = hourly_lines.copy()
    hourly_data['hourly_enterprise_volume'] = 0

# Рассчитываем объем населения = общий объем - объем предприятий
hourly_data['population_volume'] = hourly_data['volume'] - hourly_data['hourly_enterprise_volume']

# КЛИП ПО 0: объем населения не может быть отрицательным
hourly_data['population_volume'] = hourly_data['population_volume'].clip(lower=0)

# Добавляем временные признаки
hourly_data['hour'] = hourly_data['datetime'].dt.hour
hourly_data['day_of_week'] = hourly_data['datetime'].dt.dayofweek
hourly_data['day_of_year'] = hourly_data['datetime'].dt.dayofyear
hourly_data['month'] = hourly_data['datetime'].dt.month
hourly_data['is_weekend'] = (hourly_data['day_of_week'] >= 5).astype(int)

# Циклические признаки для часа и месяца
hourly_data['hour_sin'] = np.sin(2 * np.pi * hourly_data['hour'] / 24)
hourly_data['hour_cos'] = np.cos(2 * np.pi * hourly_data['hour'] / 24)
hourly_data['month_sin'] = np.sin(2 * np.pi * hourly_data['month'] / 12)
hourly_data['month_cos'] = np.cos(2 * np.pi * hourly_data['month'] / 12)

# Джойним с погодой если есть
if weather_hourly is not None and 'temperature' in weather_hourly.columns:
    hourly_data = hourly_data.merge(
        weather_hourly[['datetime', 'temperature']].rename(columns={'temperature': 'weather_temp'}),
        on='datetime',
        how='left'
    )
    # Если нет данных погоды, используем температуру из архива
    hourly_data['temperature'] = hourly_data['weather_temp'].fillna(hourly_data['temperature'])
    hourly_data = hourly_data.drop(columns=['weather_temp'])

print(f'\n[OK] Создано {len(hourly_data)} часовых записей')
print(f'[OK] Колонки: {list(hourly_data.columns)}')
print(f'\nПроверка отрицательных значений после клипа:')
print(f'  Минимум population_volume: {hourly_data["population_volume"].min():.2f}')
print(f'  Количество нулевых: {(hourly_data["population_volume"] == 0).sum()}')

# Удаляем записи с пропусками в целевой переменной или температуре
hourly_data = hourly_data.dropna(subset=['population_volume', 'temperature'])

print(f'[OK] После удаления пропусков: {len(hourly_data)} записей')

# ============================================================================
# Блок 7: Подготовка данных для моделирования
# ============================================================================
print('\n' + '='*80)
print('Подготовка данных для моделирования...')
print('='*80)

# Список доступных признаков
AVAILABLE_FEATURES = [
    'temperature',
    'hour',
    'day_of_week',
    'day_of_year',
    'month',
    'is_weekend',
    'hour_sin',
    'hour_cos',
    'month_sin',
    'month_cos',
    'hourly_enterprise_volume'
]

# Проверяем наличие признаков
available_in_data = [f for f in AVAILABLE_FEATURES if f in hourly_data.columns]
print(f'\nДоступные признаки ({len(available_in_data)}):')
for feat in available_in_data:
    print(f'  - {feat}')

# Используем весь период для обучения (валидация на нем же)
train_data = hourly_data[
    (hourly_data['datetime'] >= TRAIN_START) &
    (hourly_data['datetime'] <= TRAIN_END)
].copy()

print(f'\nДанные для обучения и валидации:')
print(f'  Период: {TRAIN_START.strftime("%Y-%m-%d")} - {TRAIN_END.strftime("%Y-%m-%d")}')
print(f'  Всего записей: {len(train_data)}')
print(f'  Уникальных линий: {train_data["line_id"].nunique()}')

if len(train_data) == 0:
    raise ValueError('Нет данных для обучения!')

# ============================================================================
# Блок 8: Функция подбора признаков по R²
# ============================================================================

def greedy_feature_selection_r2(X, y, available_features, max_features=10):
    """
    Жадный подбор признаков по R² на тех же данных (train = validation)

    Args:
        X: матрица признаков
        y: целевая переменная
        available_features: список названий признаков
        max_features: максимальное количество признаков

    Returns:
        selected_features: список выбранных признаков
        best_r2: лучший R²
    """
    print('\n' + '='*80)
    print('Подбор признаков по R² (жадный алгоритм)')
    print('='*80)

    selected_indices = []
    selected_features = []
    remaining_indices = list(range(X.shape[1]))

    model = LinearRegression()
    best_r2 = -np.inf

    for iteration in range(min(max_features, len(available_features))):
        best_new_r2 = -np.inf
        best_new_idx = None

        # Пробуем добавить каждый оставшийся признак
        for idx in remaining_indices:
            trial_indices = selected_indices + [idx]

            # Обучаем на всех данных
            model.fit(X[:, trial_indices], y)

            # Валидируем на тех же данных
            y_pred = model.predict(X[:, trial_indices])
            r2 = r2_score(y, y_pred)

            if r2 > best_new_r2:
                best_new_r2 = r2
                best_new_idx = idx

        # Если нашли улучшение, добавляем признак
        if best_new_idx is not None and best_new_r2 > best_r2:
            selected_indices.append(best_new_idx)
            selected_features.append(available_features[best_new_idx])
            remaining_indices.remove(best_new_idx)
            best_r2 = best_new_r2

            print(f'\nИтерация {iteration + 1}:')
            print(f'  Добавлен признак: {available_features[best_new_idx]}')
            print(f'  R²: {best_r2:.6f}')
            print(f'  Всего признаков: {len(selected_features)}')
        else:
            print(f'\nОстановка на итерации {iteration + 1}: нет улучшения R²')
            break

    print(f'\n{"="*80}')
    print(f'Выбрано признаков: {len(selected_features)}')
    print(f'Финальный R²: {best_r2:.6f}')
    print(f'{"="*80}')

    return selected_features, best_r2

# ============================================================================
# Блок 9: Обучение модели по каждой линии
# ============================================================================
print('\n' + '='*80)
print('Обучение моделей по линиям...')
print('='*80)

results = []

for line_id in sorted(train_data['line_id'].unique()):
    print(f'\n{"="*80}')
    print(f'ЛИНИЯ {line_id} - {lineid_to_grs.get(line_id, "Unknown")}')
    print(f'{"="*80}')

    # Фильтруем данные для текущей линии
    line_train = train_data[train_data['line_id'] == line_id].copy()

    print(f'Записей для обучения: {len(line_train)}')

    if len(line_train) < 100:
        print(f'[!] Недостаточно данных для линии {line_id}, пропускаем')
        continue

    # Подготовка матриц
    X_train = line_train[available_in_data].values
    y_train = line_train['population_volume'].values

    print(f'\nСтатистика целевой переменной:')
    print(f'  Min: {y_train.min():.2f} м³')
    print(f'  Max: {y_train.max():.2f} м³')
    print(f'  Mean: {y_train.mean():.2f} м³')
    print(f'  Std: {y_train.std():.2f} м³')

    # Стандартизация признаков
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)

    # Подбор признаков
    selected_features, best_r2 = greedy_feature_selection_r2(
        X_train_scaled,
        y_train,
        available_in_data,
        max_features=10
    )

    print(f'\nВыбранные признаки для линии {line_id}:')
    for feat in selected_features:
        print(f'  - {feat}')

    # Обучение финальной модели на выбранных признаках
    selected_indices = [available_in_data.index(f) for f in selected_features]
    X_train_selected = X_train_scaled[:, selected_indices]

    model = LinearRegression()
    model.fit(X_train_selected, y_train)

    # Предсказание на тренировочных данных (валидация на них же)
    y_pred_raw = model.predict(X_train_selected)

    # Клип по 0
    y_pred = np.maximum(0, y_pred_raw)

    # Метрики
    mae = mean_absolute_error(y_train, y_pred)
    rmse = np.sqrt(mean_squared_error(y_train, y_pred))
    r2 = r2_score(y_train, y_pred)
    mape = np.mean(np.abs((y_train - y_pred) / (y_train + 1e-10))) * 100

    # Статистика отрицательных прогнозов
    n_negative = (y_pred_raw < 0).sum()
    pct_negative = (n_negative / len(y_pred_raw)) * 100

    print(f'\n{"="*60}')
    print(f'РЕЗУЛЬТАТЫ ДЛЯ ЛИНИИ {line_id}')
    print(f'{"="*60}')
    print(f'Метрики на тренировочных данных:')
    print(f'  R²:   {r2:.4f}')
    print(f'  MAE:  {mae:.2f} м³')
    print(f'  RMSE: {rmse:.2f} м³')
    print(f'  MAPE: {mape:.2f}%')
    print(f'\nОтрицательных прогнозов (до клипа): {n_negative}/{len(y_pred_raw)} ({pct_negative:.1f}%)')
    print(f'Min прогноз (до клипа): {y_pred_raw.min():.2f} м³')
    print(f'Max прогноз (до клипа): {y_pred_raw.max():.2f} м³')

    # Сохраняем результаты
    results.append({
        'line_id': line_id,
        'grs_name': lineid_to_grs.get(line_id, 'Unknown'),
        'n_samples': len(line_train),
        'n_features': len(selected_features),
        'features': ', '.join(selected_features),
        'r2': r2,
        'mae': mae,
        'rmse': rmse,
        'mape': mape,
        'n_negative': n_negative,
        'pct_negative': pct_negative,
        'min_pred_raw': y_pred_raw.min(),
        'max_pred_raw': y_pred_raw.max()
    })

    # Визуализация
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle(f'Линия {line_id} - {lineid_to_grs.get(line_id, "Unknown")}', fontsize=14, weight='bold')

    # График 1: Факт vs Прогноз
    axes[0, 0].scatter(y_train, y_pred, alpha=0.5, s=10)
    axes[0, 0].plot([y_train.min(), y_train.max()], [y_train.min(), y_train.max()], 'r--', lw=2)
    axes[0, 0].set_xlabel('Факт (м³)')
    axes[0, 0].set_ylabel('Прогноз (м³)')
    axes[0, 0].set_title(f'Факт vs Прогноз (R²={r2:.4f})')
    axes[0, 0].grid(True, alpha=0.3)

    # График 2: Временной ряд (первые 7 дней)
    first_week = line_train.head(24*7)
    if len(first_week) > 0:
        axes[0, 1].plot(first_week['datetime'], first_week['population_volume'], label='Факт', linewidth=1)
        y_pred_week = model.predict(scaler.transform(first_week[available_in_data].values)[:, selected_indices])
        y_pred_week = np.maximum(0, y_pred_week)
        axes[0, 1].plot(first_week['datetime'], y_pred_week, label='Прогноз', linewidth=1, alpha=0.7)
        axes[0, 1].set_xlabel('Дата и время')
        axes[0, 1].set_ylabel('Объем (м³)')
        axes[0, 1].set_title('Первые 7 дней (часовые данные)')
        axes[0, 1].legend()
        axes[0, 1].grid(True, alpha=0.3)
        axes[0, 1].tick_params(axis='x', rotation=45)

    # График 3: Распределение ошибок
    errors = y_train - y_pred
    axes[1, 0].hist(errors, bins=50, edgecolor='black', alpha=0.7)
    axes[1, 0].axvline(0, color='r', linestyle='--', linewidth=2)
    axes[1, 0].set_xlabel('Ошибка (м³)')
    axes[1, 0].set_ylabel('Частота')
    axes[1, 0].set_title(f'Распределение ошибок (MAE={mae:.2f})')
    axes[1, 0].grid(True, alpha=0.3)

    # График 4: Важность признаков (коэффициенты)
    if len(model.coef_) > 0:
        coef_df = pd.DataFrame({
            'feature': selected_features,
            'coefficient': model.coef_
        }).sort_values('coefficient', key=abs, ascending=False)

        axes[1, 1].barh(coef_df['feature'], coef_df['coefficient'])
        axes[1, 1].set_xlabel('Коэффициент')
        axes[1, 1].set_title('Коэффициенты модели')
        axes[1, 1].grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = OUTPUT_DIR / f'line_{line_id}_results.png'
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'\n[OK] График сохранен: {output_file}')

# ============================================================================
# Блок 10: Сводка результатов
# ============================================================================
print('\n' + '='*80)
print('СВОДКА РЕЗУЛЬТАТОВ')
print('='*80)

if results:
    results_df = pd.DataFrame(results)

    print('\nМетрики по линиям:')
    print(results_df[['line_id', 'grs_name', 'n_samples', 'n_features', 'r2', 'mae', 'rmse', 'mape']].to_string(index=False))

    print('\nСредние метрики:')
    print(f'  R²:   {results_df["r2"].mean():.4f} ± {results_df["r2"].std():.4f}')
    print(f'  MAE:  {results_df["mae"].mean():.2f} ± {results_df["mae"].std():.2f} м³')
    print(f'  RMSE: {results_df["rmse"].mean():.2f} ± {results_df["rmse"].std():.2f} м³')
    print(f'  MAPE: {results_df["mape"].mean():.2f} ± {results_df["mape"].std():.2f}%')

    # Сохранение результатов в Excel
    output_excel = OUTPUT_DIR / 'hourly_prediction_results.xlsx'
    with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
        results_df.to_excel(writer, sheet_name='Метрики по линиям', index=False)

        # Добавляем сводку
        summary_df = pd.DataFrame({
            'Метрика': ['R²', 'MAE', 'RMSE', 'MAPE'],
            'Среднее': [
                results_df['r2'].mean(),
                results_df['mae'].mean(),
                results_df['rmse'].mean(),
                results_df['mape'].mean()
            ],
            'Стд. откл.': [
                results_df['r2'].std(),
                results_df['mae'].std(),
                results_df['rmse'].std(),
                results_df['mape'].std()
            ]
        })
        summary_df.to_excel(writer, sheet_name='Сводка', index=False)

    print(f'\n[OK] Результаты сохранены: {output_excel}')
else:
    print('\n[!] Нет результатов для сохранения')

print('\n' + '='*80)
print('ЗАВЕРШЕНО')
print('='*80)
