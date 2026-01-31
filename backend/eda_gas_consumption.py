#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDA (Exploratory Data Analysis) для данных потребления газа населением

Цель: Глубокий визуальный и статистический анализ для выявления:
- Корреляций температуры и потребления
- Сезонных паттернов
- Выбросов и аномалий
- Различий между ГРС
- Скрытых зависимостей

Выход:
- Excel: backend/data/eda_gas_consumption_results.xlsx (8 sheets)
- PNG: backend/data/eda_output/ (~80 файлов)
"""

import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from scipy import stats
from scipy.stats import pearsonr, spearmanr
from statsmodels.tsa.seasonal import seasonal_decompose
from sklearn.linear_model import Ridge

# Исправление кодировки Windows консоли для корректного отображения кириллицы и эмодзи
from utils.encoding_fix import setup_console_encoding
setup_console_encoding()
from sklearn.metrics import r2_score, mean_absolute_error

# Подавление warnings
warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)

# Настройка визуализации
plt.rcParams['figure.dpi'] = 100
plt.rcParams['savefig.dpi'] = 150
plt.rcParams['font.size'] = 10
sns.set_style('whitegrid')

# ============================================================================
# PATHS
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
OUTPUT_DIR = DATA_DIR / 'eda_output'

# Создание выходных папок
for subdir in ['timeseries', 'seasonal', 'scatter', 'distributions',
               'heatmaps', 'outliers', 'comparison']:
    (OUTPUT_DIR / subdir).mkdir(parents=True, exist_ok=True)

print(f'[+] Выходные файлы будут сохранены в: {OUTPUT_DIR}')

# ============================================================================
# DATABASE CONNECTION
# ============================================================================
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Загрузка переменных окружения
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / '.env')

# Построение DB_URL из переменных окружения
DB_USER = os.getenv('POSTGRES_USER', 'postgres')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'assembler')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('POSTGRES_DB', 'hostlib_db')
DB_URL = f'postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'

engine = create_engine(DB_URL)
print(f'[+] DB engine создан: {engine.url}')

# ============================================================================
# CONFIGURATION
# ============================================================================
LINE_IDS = [16, 17, 18, 19, 21, 23, 24, 25, 1003]

LINEID_TO_GRS = {
    16: 'ГРС 16 Колосовка',
    17: 'ГРС 17 Павловка',
    18: 'ГРС 18 Веселая долина',
    19: 'ГРС 19 Марфинка',
    21: 'ГРС 21 Ракитянка',
    23: 'ГРС 23 Северное',
    24: 'ГРС 24 Малиновка',
    25: 'ГРС 25 Вольное',
    1003: 'ГРС 1003 Бобрик'
}

WEATHER_FILE = DATA_DIR / 'weather_2025_daily_contractual.csv'
VOLUME_FILE = DATA_DIR / 'volume_2025.xlsx'
LINE_ID_FILE = DATA_DIR / 'line_id_2025.xlsx'

# ============================================================================
# STEP 1: LOAD DATA
# ============================================================================
print('\n' + '='*80)
print('STEP 1: ЗАГРУЗКА ДАННЫХ')
print('='*80)

# Загрузка daily данных напрямую из БД
print('\n[*] Загрузка daily_archive...')

line_ids_str = ','.join(map(str, LINE_IDS))
query = text(f"""
SELECT period AS date, line_id, volume AS gas_volume
FROM daily_archive
WHERE line_id IN ({line_ids_str})
ORDER BY period, line_id
""")

with engine.connect() as conn:
    daily = pd.read_sql(query, conn)

daily['date'] = pd.to_datetime(daily['date']).dt.normalize()
print(f'[OK] Загружено {len(daily)} дневных записей')

# Статистика по ГРС
for lid in LINE_IDS:
    count = len(daily[daily['line_id'] == lid])
    if count > 0:
        print(f'  ГРС {LINEID_TO_GRS[lid]}: {count} записей')

# Загрузка маппинга ГРС (для enterprise volume)
print('\n[*] Загрузка line_id маппинга...')
if not LINE_ID_FILE.exists():
    print(f'[ERROR] Файл не найден: {LINE_ID_FILE}')
    sys.exit(1)

grs_map_df = pd.read_excel(LINE_ID_FILE, sheet_name='line_id', header=None, names=['grs_name', 'line_id'])
grs_to_lineid = dict(zip(grs_map_df['grs_name'], grs_map_df['line_id']))
print(f'[OK] Загружено {len(grs_map_df)} маппингов ГРС')

# Загрузка enterprise данных из volume_2025.xlsx
print('\n[*] Загрузка volume_2025.xlsx...')
if not VOLUME_FILE.exists():
    print(f'[ERROR] Файл не найден: {VOLUME_FILE}')
    sys.exit(1)

vol_raw = pd.read_excel(VOLUME_FILE, sheet_name='TDSheet')

# Переименование первых 3 колонок
cols = list(vol_raw.columns)
cols[0] = 'enterprise_name'
cols[1] = 'enterprise_line'
cols[2] = 'grs'
vol_raw.columns = cols

# Парсинг колонок дат
import re
date_cols = []
date_map = {}
for col in vol_raw.columns[3:]:
    match = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', str(col))
    if match:
        day, month, year = match.groups()
        date_obj = pd.to_datetime(f'{year}-{month}-{day}')
        date_map[col] = date_obj
        date_cols.append(col)

# Фильтрация по ГРС
vol_filtered = vol_raw[vol_raw['grs'].isin(grs_to_lineid.keys())].copy()
vol_filtered['line_id'] = vol_filtered['grs'].map(grs_to_lineid)

# Преобразование wide -> long
ent_long = vol_filtered.melt(
    id_vars=['enterprise_name', 'enterprise_line', 'grs', 'line_id'],
    value_vars=date_cols,
    var_name='date_col',
    value_name='enterprise_volume'
)

ent_long['date'] = pd.to_datetime(ent_long['date_col'].map(date_map)).dt.normalize()
ent_long['enterprise_volume'] = pd.to_numeric(ent_long['enterprise_volume'], errors='coerce').fillna(0)

# Агрегация по line_id + date
enterprise_agg = (
    ent_long
    .groupby(['line_id', 'date'], as_index=False)['enterprise_volume']
    .sum()
)

print(f'[OK] Агрегировано {len(enterprise_agg)} записей предприятий')

# Расчет population_volume
print('\n[*] Расчет population_volume = line_volume - enterprise_volume...')
merged = daily.merge(enterprise_agg, on=['date', 'line_id'], how='left')
merged['enterprise_volume'] = merged['enterprise_volume'].fillna(0)
merged['population_volume'] = merged['gas_volume'] - merged['enterprise_volume']

# Убираем отрицательные значения
neg_count = (merged['population_volume'] < 0).sum()
if neg_count > 0:
    print(f'[WARNING] Обнаружено {neg_count} отрицательных значений, приравниваем к 0')
    merged['population_volume'] = merged['population_volume'].clip(lower=0)

print(f'[OK] Расчет завершен')

# Загрузка погодных данных
print('\n[*] Загрузка weather_2025_daily_contractual.csv...')
if not WEATHER_FILE.exists():
    print(f'[ERROR] Файл не найден: {WEATHER_FILE}')
    sys.exit(1)

weather = pd.read_csv(WEATHER_FILE, parse_dates=['date'])
# Нормализация дат: убираем время, оставляем только дату
weather['date'] = pd.to_datetime(weather['date']).dt.normalize()
print(f'[OK] Загружено {len(weather)} погодных записей')
print(f'  Диапазон дат погоды: {weather["date"].min()} - {weather["date"].max()}')

# Merge с погодой
print('\n[*] Объединение с погодными данными...')
print(f'  merged dates: {merged["date"].min()} - {merged["date"].max()} ({len(merged)} records)')
print(f'  weather dates: {weather["date"].min()} - {weather["date"].max()} ({len(weather)} records)')

analysis = merged.merge(weather, on='date', how='inner')  # Используем inner join для отладки
print(f'  После merge: {len(analysis)} записей')

if len(analysis) == 0:
    print('[ERROR] После merge с погодой 0 записей!')
    print('[ERROR] Возможно, даты не совпадают. Показываю примеры дат:')
    print(f'  merged samples: {merged["date"].head().tolist()}')
    print(f'  weather samples: {weather["date"].head().tolist()}')
    sys.exit(1)

# Проверка на NaN в temperature
nan_count = analysis['temperature'].isna().sum()
if nan_count > 0:
    print(f'  [WARNING] Найдено {nan_count} NaN в temperature, удаляем')
    analysis = analysis.dropna(subset=['temperature'])

print(f'[OK] Итоговый датасет: {len(analysis)} записей')

# Добавление временных признаков
analysis['year'] = analysis['date'].dt.year
analysis['month'] = analysis['date'].dt.month
analysis['day_of_week'] = analysis['date'].dt.dayofweek
analysis['day_of_year'] = analysis['date'].dt.dayofyear

print(f'\n[*] Период данных: {analysis["date"].min()} - {analysis["date"].max()}')
print(f'[*] Количество ГРС: {analysis["line_id"].nunique()}')
print(f'[*] Среднее потребление: {analysis["population_volume"].mean():.2f} тыс.м^3')
print(f'[*] Средняя температура: {analysis["temperature"].mean():.2f} C')

# ============================================================================
# STEP 2: ОБЩАЯ СТАТИСТИКА
# ============================================================================
print('\n' + '='*80)
print('STEP 2: ОБЩАЯ СТАТИСТИКА ПО ГРС')
print('='*80)

stats_list = []

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid]['population_volume']
    temp_data = analysis[analysis['line_id'] == lid]['temperature']

    # IQR метод для outliers
    Q1 = grs_data.quantile(0.25)
    Q3 = grs_data.quantile(0.75)
    IQR = Q3 - Q1
    outliers = ((grs_data < (Q1 - 1.5 * IQR)) | (grs_data > (Q3 + 1.5 * IQR))).sum()

    stats_list.append({
        'line_id': lid,
        'GRS': LINEID_TO_GRS.get(lid, f'Line {lid}'),
        'count': len(grs_data),
        'mean_volume': grs_data.mean(),
        'std_volume': grs_data.std(),
        'min_volume': grs_data.min(),
        'q25_volume': Q1,
        'median_volume': grs_data.median(),
        'q75_volume': Q3,
        'max_volume': grs_data.max(),
        'mean_temp': temp_data.mean(),
        'std_temp': temp_data.std(),
        'min_temp': temp_data.min(),
        'max_temp': temp_data.max(),
        'negative_values': (grs_data < 0).sum(),
        'zero_values': (grs_data == 0).sum(),
        'outliers_count': outliers,
        'outliers_pct': outliers / len(grs_data) * 100
    })

stats_df = pd.DataFrame(stats_list)
print(stats_df.to_string(index=False))

# ============================================================================
# STEP 3: КОРРЕЛЯЦИОННЫЙ АНАЛИЗ
# ============================================================================
print('\n' + '='*80)
print('STEP 3: КОРРЕЛЯЦИОННЫЙ АНАЛИЗ')
print('='*80)

correlation_list = []

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid]
    temp = grs_data['temperature']
    volume = grs_data['population_volume']

    # Удаляем NaN для корреляции
    mask = ~(temp.isna() | volume.isna())
    temp_clean = temp[mask]
    volume_clean = volume[mask]

    if len(temp_clean) > 3:
        pearson_r, pearson_p = pearsonr(temp_clean, volume_clean)
        spearman_r, spearman_p = spearmanr(temp_clean, volume_clean)
    else:
        pearson_r = pearson_p = spearman_r = spearman_p = np.nan

    correlation_list.append({
        'line_id': lid,
        'GRS': LINEID_TO_GRS.get(lid, f'Line {lid}'),
        'pearson_r': pearson_r,
        'pearson_p': pearson_p,
        'pearson_sig': '***' if pearson_p < 0.001 else '**' if pearson_p < 0.01 else '*' if pearson_p < 0.05 else 'ns',
        'spearman_r': spearman_r,
        'spearman_p': spearman_p,
        'spearman_sig': '***' if spearman_p < 0.001 else '**' if spearman_p < 0.01 else '*' if spearman_p < 0.05 else 'ns'
    })

correlation_df = pd.DataFrame(correlation_list)
print(correlation_df.to_string(index=False))

# ============================================================================
# STEP 4: ВРЕМЕННЫЕ РЯДЫ (PNG)
# ============================================================================
print('\n' + '='*80)
print('STEP 4: ВИЗУАЛИЗАЦИЯ ВРЕМЕННЫХ РЯДОВ')
print('='*80)

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    # Outliers (IQR method)
    Q1 = grs_data['population_volume'].quantile(0.25)
    Q3 = grs_data['population_volume'].quantile(0.75)
    IQR = Q3 - Q1
    outlier_mask = (grs_data['population_volume'] < (Q1 - 1.5 * IQR)) | \
                   (grs_data['population_volume'] > (Q3 + 1.5 * IQR))

    fig, axes = plt.subplots(2, 1, figsize=(14, 8))

    # Plot 1: Потребление
    axes[0].plot(grs_data['date'], grs_data['population_volume'],
                 alpha=0.6, linewidth=1, label='Потребление')
    axes[0].axhline(grs_data['population_volume'].mean(),
                    color='red', linestyle='--', linewidth=2, label='Среднее')
    axes[0].scatter(grs_data.loc[outlier_mask, 'date'],
                    grs_data.loc[outlier_mask, 'population_volume'],
                    color='red', s=50, alpha=0.7, label='Выбросы', zorder=5)
    axes[0].set_title(f'{grs_name}: Потребление газа населением во времени', fontsize=12, fontweight='bold')
    axes[0].set_ylabel('Потребление (тыс. м^3)')
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    # Plot 2: Температура
    axes[1].plot(grs_data['date'], grs_data['temperature'],
                 color='orange', alpha=0.6, linewidth=1, label='Температура')
    axes[1].axhline(15, color='green', linestyle='--', linewidth=2,
                    label='Порог отопления (15C)')
    axes[1].axhline(0, color='blue', linestyle='--', linewidth=2,
                    label='Точка замерзания (0C)')
    axes[1].set_title('Температура во времени', fontsize=12, fontweight='bold')
    axes[1].set_xlabel('Дата')
    axes[1].set_ylabel('Температура (C)')
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'timeseries' / f'timeseries_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: timeseries_line{lid}.png')

# ============================================================================
# STEP 5: СЕЗОННАЯ ДЕКОМПОЗИЦИЯ (PNG)
# ============================================================================
print('\n' + '='*80)
print('STEP 5: СЕЗОННАЯ ДЕКОМПОЗИЦИЯ')
print('='*80)

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    # Подготовка временного ряда
    ts = grs_data.set_index('date')['population_volume'].sort_index()

    # Убираем дубликаты по дате (берем среднее)
    ts = ts.groupby(ts.index).mean()

    # Заполняем пропуски
    ts = ts.asfreq('D').fillna(method='ffill')

    if len(ts) < 14:  # Минимум для декомпозиции
        print(f'[WARNING] Недостаточно данных для {grs_name}, пропускаем')
        continue

    try:
        # Декомпозиция
        decomposition = seasonal_decompose(ts, model='additive', period=7, extrapolate_trend='freq')

        fig, axes = plt.subplots(4, 1, figsize=(14, 10))

        # Исходный ряд
        decomposition.observed.plot(ax=axes[0], title='Исходный ряд')
        axes[0].set_ylabel('Значение')
        axes[0].grid(True, alpha=0.3)

        # Тренд
        decomposition.trend.plot(ax=axes[1], title='Тренд', color='orange')
        axes[1].set_ylabel('Тренд')
        axes[1].grid(True, alpha=0.3)

        # Сезонность
        decomposition.seasonal.plot(ax=axes[2], title='Сезонность (период=7 дней)', color='green')
        axes[2].set_ylabel('Сезонность')
        axes[2].grid(True, alpha=0.3)

        # Остатки
        decomposition.resid.plot(ax=axes[3], title='Остатки', color='red', alpha=0.5)
        axes[3].set_ylabel('Остатки')
        axes[3].set_xlabel('Дата')
        axes[3].grid(True, alpha=0.3)

        fig.suptitle(f'{grs_name}: Сезонная декомпозиция', fontsize=14, fontweight='bold')
        plt.tight_layout()
        plt.savefig(OUTPUT_DIR / 'seasonal' / f'seasonal_decomposition_line{lid}.png', bbox_inches='tight')
        plt.close()

        print(f'[OK] Сохранен график: seasonal_decomposition_line{lid}.png')
    except Exception as e:
        print(f'[ERROR] Ошибка декомпозиции для {grs_name}: {e}')

# ============================================================================
# STEP 6: SCATTER PLOTS (PNG)
# ============================================================================
print('\n' + '='*80)
print('STEP 6: SCATTER PLOTS (Температура vs Потребление)')
print('='*80)

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    # Цветовая кодировка по температурным зонам
    def temp_zone(t):
        if t < 0:
            return 'Ниже 0C'
        elif t < 10:
            return '0-10C'
        elif t < 15:
            return '10-15C'
        elif t < 25:
            return '15-25C'
        else:
            return 'Выше 25C'

    grs_data['temp_zone'] = grs_data['temperature'].apply(temp_zone)

    zone_colors = {
        'Ниже 0C': 'blue',
        '0-10C': 'cyan',
        '10-15C': 'green',
        '15-25C': 'yellow',
        'Выше 25C': 'red'
    }

    fig, ax = plt.subplots(figsize=(12, 8))

    for zone, color in zone_colors.items():
        mask = grs_data['temp_zone'] == zone
        if mask.sum() > 0:
            ax.scatter(grs_data.loc[mask, 'temperature'],
                      grs_data.loc[mask, 'population_volume'],
                      c=color, label=zone, alpha=0.6, s=30)

    # Линия регрессии
    z = np.polyfit(grs_data['temperature'], grs_data['population_volume'], 1)
    p = np.poly1d(z)
    temp_range = np.linspace(grs_data['temperature'].min(), grs_data['temperature'].max(), 100)
    ax.plot(temp_range, p(temp_range), "r--", linewidth=2, label='Линия регрессии')

    # Корреляция
    corr, _ = pearsonr(grs_data['temperature'], grs_data['population_volume'])

    ax.set_title(f'{grs_name}: Температура vs Потребление (r={corr:.3f})',
                fontsize=12, fontweight='bold')
    ax.set_xlabel('Температура (C)')
    ax.set_ylabel('Потребление (тыс. м^3)')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'scatter' / f'scatter_temp_zones_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: scatter_temp_zones_line{lid}.png')

# 3D scatter plots
print('\n[*] Создание 3D scatter plots...')

from mpl_toolkits.mplot3d import Axes3D

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    fig = plt.figure(figsize=(12, 8))
    ax = fig.add_subplot(111, projection='3d')

    scatter = ax.scatter(grs_data['temperature'],
                        grs_data['day_of_week'],
                        grs_data['population_volume'],
                        c=grs_data['population_volume'],
                        cmap='viridis', alpha=0.6, s=30)

    ax.set_xlabel('Температура (C)')
    ax.set_ylabel('День недели (0=Пн)')
    ax.set_zlabel('Потребление (тыс. м^3)')
    ax.set_title(f'{grs_name}: 3D Scatter (Температура, День недели, Потребление)',
                fontweight='bold')

    fig.colorbar(scatter, ax=ax, label='Потребление')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'scatter' / f'scatter_3d_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: scatter_3d_line{lid}.png')

# ============================================================================
# STEP 7: РАСПРЕДЕЛЕНИЯ (PNG)
# ============================================================================
print('\n' + '='*80)
print('STEP 7: РАСПРЕДЕЛЕНИЯ ПО ТЕМПЕРАТУРНЫМ БИНАМ')
print('='*80)

temp_bins = [
    (-np.inf, 0, 'Ниже 0C'),
    (0, 5, '0-5C'),
    (5, 10, '5-10C'),
    (10, 15, '10-15C'),
    (15, 20, '15-20C'),
    (20, 25, '20-25C'),
    (25, np.inf, 'Выше 25C')
]

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    fig, axes = plt.subplots(3, 3, figsize=(15, 12))
    axes = axes.flatten()

    for idx, (low, high, label) in enumerate(temp_bins):
        if idx >= len(axes):
            break

        mask = (grs_data['temperature'] >= low) & (grs_data['temperature'] < high)
        bin_data = grs_data.loc[mask, 'population_volume']

        if len(bin_data) > 0:
            axes[idx].hist(bin_data, bins=20, alpha=0.7, color='steelblue', edgecolor='black')
            axes[idx].axvline(bin_data.mean(), color='red', linestyle='--', linewidth=2,
                            label=f'Среднее: {bin_data.mean():.2f}')
            axes[idx].set_title(f'{label} (n={len(bin_data)})', fontweight='bold')
            axes[idx].set_xlabel('Потребление (тыс. м^3)')
            axes[idx].set_ylabel('Частота')
            axes[idx].legend()
            axes[idx].grid(True, alpha=0.3)
        else:
            axes[idx].text(0.5, 0.5, 'Нет данных', ha='center', va='center',
                         transform=axes[idx].transAxes, fontsize=12)
            axes[idx].set_title(label, fontweight='bold')

    # Убираем лишние оси
    for idx in range(len(temp_bins), len(axes)):
        fig.delaxes(axes[idx])

    fig.suptitle(f'{grs_name}: Распределение потребления по температурным зонам',
                fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'distributions' / f'distribution_by_temp_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: distribution_by_temp_line{lid}.png')

# Месячные boxplots
print('\n[*] Создание месячных boxplots...')

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    fig, ax = plt.subplots(figsize=(12, 6))

    grs_data.boxplot(column='population_volume', by='month', ax=ax)
    ax.set_title(f'{grs_name}: Распределение потребления по месяцам', fontweight='bold')
    ax.set_xlabel('Месяц')
    ax.set_ylabel('Потребление (тыс. м^3)')
    plt.suptitle('')  # Убираем стандартный заголовок
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'distributions' / f'monthly_boxplot_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: monthly_boxplot_line{lid}.png')

# Недельные boxplots
print('\n[*] Создание недельных boxplots...')

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    fig, ax = plt.subplots(figsize=(12, 6))

    grs_data.boxplot(column='population_volume', by='day_of_week', ax=ax)
    ax.set_title(f'{grs_name}: Распределение потребления по дням недели', fontweight='bold')
    ax.set_xlabel('День недели (0=Пн, 6=Вс)')
    ax.set_ylabel('Потребление (тыс. м^3)')
    ax.set_xticklabels(['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'])
    plt.suptitle('')
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'distributions' / f'weekly_boxplot_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: weekly_boxplot_line{lid}.png')

# ============================================================================
# STEP 8: HEATMAPS (PNG)
# ============================================================================
print('\n' + '='*80)
print('STEP 8: CORRELATION HEATMAPS')
print('='*80)

# Корреляционные матрицы для каждой ГРС
basic_features = ['temperature', 'population_volume', 'month', 'day_of_week']

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid][basic_features].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    corr_matrix = grs_data.corr()

    fig, ax = plt.subplots(figsize=(8, 6))
    sns.heatmap(corr_matrix, annot=True, fmt='.3f', cmap='coolwarm',
                center=0, square=True, linewidths=1, ax=ax,
                cbar_kws={'label': 'Корреляция'})
    ax.set_title(f'{grs_name}: Корреляционная матрица', fontsize=12, fontweight='bold')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'heatmaps' / f'correlation_matrix_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: correlation_matrix_line{lid}.png')

# Heatmap месяц x день недели
print('\n[*] Создание heatmaps месяц x день недели...')

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    pivot = grs_data.pivot_table(values='population_volume',
                                 index='month', columns='day_of_week',
                                 aggfunc='mean')

    fig, ax = plt.subplots(figsize=(10, 8))
    sns.heatmap(pivot, annot=True, fmt='.1f', cmap='YlOrRd', ax=ax,
                cbar_kws={'label': 'Среднее потребление (тыс. м^3)'})
    ax.set_title(f'{grs_name}: Среднее потребление (Месяц x День недели)',
                fontsize=12, fontweight='bold')
    ax.set_xlabel('День недели (0=Пн)')
    ax.set_ylabel('Месяц')
    ax.set_xticklabels(['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'])

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'heatmaps' / f'heatmap_month_dow_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: heatmap_month_dow_line{lid}.png')

# ============================================================================
# STEP 9: АНАЛИЗ ВЫБРОСОВ (PNG)
# ============================================================================
print('\n' + '='*80)
print('STEP 9: АНАЛИЗ ВЫБРОСОВ')
print('='*80)

outliers_list = []

for lid in sorted(analysis['line_id'].unique()):
    grs_data = analysis[analysis['line_id'] == lid].copy()
    grs_name = LINEID_TO_GRS.get(lid, f'Line {lid}')

    # IQR метод
    Q1 = grs_data['population_volume'].quantile(0.25)
    Q3 = grs_data['population_volume'].quantile(0.75)
    IQR = Q3 - Q1
    iqr_mask = (grs_data['population_volume'] < (Q1 - 1.5 * IQR)) | \
               (grs_data['population_volume'] > (Q3 + 1.5 * IQR))

    # Z-score метод
    z_scores = np.abs(stats.zscore(grs_data['population_volume']))
    zscore_mask = z_scores > 3

    # Комбинированный список выбросов
    outlier_dates = grs_data.loc[iqr_mask | zscore_mask, 'date'].values

    for date in outlier_dates:
        row = grs_data[grs_data['date'] == date].iloc[0]
        outliers_list.append({
            'line_id': lid,
            'GRS': grs_name,
            'date': date,
            'population_volume': row['population_volume'],
            'temperature': row['temperature'],
            'method': 'IQR' if iqr_mask[grs_data['date'] == date].any() else 'Z-score'
        })

    # Визуализация выбросов
    fig, ax = plt.subplots(figsize=(12, 8))

    # Нормальные точки
    normal_mask = ~(iqr_mask | zscore_mask)
    ax.scatter(grs_data.loc[normal_mask, 'temperature'],
              grs_data.loc[normal_mask, 'population_volume'],
              alpha=0.5, s=30, label='Нормальные значения')

    # Выбросы
    ax.scatter(grs_data.loc[iqr_mask | zscore_mask, 'temperature'],
              grs_data.loc[iqr_mask | zscore_mask, 'population_volume'],
              color='red', marker='x', s=100, linewidths=3, label='Выбросы', zorder=5)

    ax.set_title(f'{grs_name}: Выбросы (красные крестики)', fontsize=12, fontweight='bold')
    ax.set_xlabel('Температура (C)')
    ax.set_ylabel('Потребление (тыс. м^3)')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'outliers' / f'outliers_line{lid}.png', bbox_inches='tight')
    plt.close()

    print(f'[OK] Сохранен график: outliers_line{lid}.png (выбросов: {len(outlier_dates)})')

outliers_df = pd.DataFrame(outliers_list)

# ============================================================================
# STEP 10: СРАВНЕНИЕ ГРС (PNG)
# ============================================================================
print('\n' + '='*80)
print('STEP 10: СРАВНЕНИЕ МЕЖДУ ГРС')
print('='*80)

# Загрузка результатов из analysis_population_gas_results.xlsx (если есть)
results_file = DATA_DIR / 'analysis_population_gas_results.xlsx'

if results_file.exists():
    print('[*] Загрузка метрик из analysis_population_gas_results.xlsx...')
    results_df = pd.read_excel(results_file, sheet_name='Summary')

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # R² comparison
    axes[0].bar(range(len(results_df)), results_df['R2_test'], color='steelblue')
    axes[0].set_xticks(range(len(results_df)))
    axes[0].set_xticklabels(results_df['GRS'], rotation=45, ha='right')
    axes[0].set_ylabel('R² Score')
    axes[0].set_title('Сравнение R² по ГРС', fontweight='bold')
    axes[0].axhline(0.75, color='green', linestyle='--', linewidth=2, label='Целевой R² (0.75)')
    axes[0].axhline(results_df['R2_test'].mean(), color='red', linestyle='--',
                   linewidth=2, label=f'Среднее ({results_df["R2_test"].mean():.3f})')
    axes[0].legend()
    axes[0].grid(True, alpha=0.3, axis='y')

    # MAE comparison
    axes[1].bar(range(len(results_df)), results_df['MAE_test'], color='coral')
    axes[1].set_xticks(range(len(results_df)))
    axes[1].set_xticklabels(results_df['GRS'], rotation=45, ha='right')
    axes[1].set_ylabel('MAE (тыс. м^3)')
    axes[1].set_title('Сравнение MAE по ГРС', fontweight='bold')
    axes[1].axhline(results_df['MAE_test'].mean(), color='red', linestyle='--',
                   linewidth=2, label=f'Среднее ({results_df["MAE_test"].mean():.2f})')
    axes[1].legend()
    axes[1].grid(True, alpha=0.3, axis='y')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'comparison' / 'comparison_metrics.png', bbox_inches='tight')
    plt.close()

    print('[OK] Сохранен график: comparison_metrics.png')
else:
    print('[WARNING] Файл analysis_population_gas_results.xlsx не найден, пропускаем сравнение метрик')

# Boxplot сравнение потребления
fig, ax = plt.subplots(figsize=(14, 6))

data_for_boxplot = [analysis[analysis['line_id'] == lid]['population_volume'].values
                    for lid in sorted(analysis['line_id'].unique())]
labels = [LINEID_TO_GRS.get(lid, f'Line {lid}') for lid in sorted(analysis['line_id'].unique())]

bp = ax.boxplot(data_for_boxplot, labels=labels, patch_artist=True)

for patch in bp['boxes']:
    patch.set_facecolor('lightblue')

ax.set_title('Сравнение распределения потребления между ГРС', fontsize=12, fontweight='bold')
ax.set_xlabel('ГРС')
ax.set_ylabel('Потребление (тыс. м^3)')
ax.tick_params(axis='x', rotation=45)
plt.xticks(rotation=45, ha='right')
ax.grid(True, alpha=0.3, axis='y')

plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'comparison' / 'comparison_consumption.png', bbox_inches='tight')
plt.close()

print('[OK] Сохранен график: comparison_consumption.png')

# ============================================================================
# STEP 11: ЭКСПОРТ В EXCEL
# ============================================================================
print('\n' + '='*80)
print('STEP 11: ЭКСПОРТ РЕЗУЛЬТАТОВ В EXCEL')
print('='*80)

output_excel = DATA_DIR / 'eda_gas_consumption_results.xlsx'

with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
    # Sheet 1: Summary
    stats_df.to_excel(writer, sheet_name='Summary', index=False)

    # Sheet 2: Correlations
    correlation_df.to_excel(writer, sheet_name='Correlations', index=False)

    # Sheet 3: Statistics per GRS (детальная)
    stats_df.to_excel(writer, sheet_name='Statistics_per_GRS', index=False)

    # Sheet 4: Outliers
    if not outliers_df.empty:
        outliers_df.to_excel(writer, sheet_name='Outliers', index=False)

    # Sheet 5: Seasonal Patterns
    seasonal_patterns = []
    for lid in sorted(analysis['line_id'].unique()):
        grs_data = analysis[analysis['line_id'] == lid]
        monthly_stats = grs_data.groupby('month')['population_volume'].agg(['mean', 'std', 'count'])
        monthly_stats['line_id'] = lid
        monthly_stats['GRS'] = LINEID_TO_GRS.get(lid, f'Line {lid}')
        monthly_stats = monthly_stats.reset_index()
        seasonal_patterns.append(monthly_stats)

    seasonal_df = pd.concat(seasonal_patterns, ignore_index=True)
    seasonal_df.to_excel(writer, sheet_name='Seasonal_Patterns', index=False)

    # Sheet 6: Weekly Patterns
    weekly_patterns = []
    for lid in sorted(analysis['line_id'].unique()):
        grs_data = analysis[analysis['line_id'] == lid]
        weekly_stats = grs_data.groupby('day_of_week')['population_volume'].agg(['mean', 'std', 'count'])
        weekly_stats['line_id'] = lid
        weekly_stats['GRS'] = LINEID_TO_GRS.get(lid, f'Line {lid}')
        weekly_stats = weekly_stats.reset_index()
        weekly_patterns.append(weekly_stats)

    weekly_df = pd.concat(weekly_patterns, ignore_index=True)
    weekly_df.to_excel(writer, sheet_name='Weekly_Patterns', index=False)

    # Sheet 7: Feature Correlations (для всех ГРС вместе)
    all_corr = analysis[basic_features].corr()
    all_corr.to_excel(writer, sheet_name='Feature_Correlations')

    # Sheet 8: Temperature Bins
    temp_bin_stats = []
    for lid in sorted(analysis['line_id'].unique()):
        grs_data = analysis[analysis['line_id'] == lid].copy()

        for low, high, label in temp_bins:
            mask = (grs_data['temperature'] >= low) & (grs_data['temperature'] < high)
            bin_data = grs_data.loc[mask, 'population_volume']

            if len(bin_data) > 0:
                temp_bin_stats.append({
                    'line_id': lid,
                    'GRS': LINEID_TO_GRS.get(lid, f'Line {lid}'),
                    'temp_bin': label,
                    'count': len(bin_data),
                    'mean': bin_data.mean(),
                    'std': bin_data.std(),
                    'min': bin_data.min(),
                    'max': bin_data.max()
                })

    temp_bins_df = pd.DataFrame(temp_bin_stats)
    temp_bins_df.to_excel(writer, sheet_name='Temperature_Bins', index=False)

print(f'[OK] Excel файл сохранен: {output_excel}')

# ============================================================================
# SUMMARY
# ============================================================================
print('\n' + '='*80)
print('EDA ЗАВЕРШЕН')
print('='*80)

print(f'\n[+] Результаты сохранены в:')
print(f'   Excel: {output_excel}')
print(f'   Графики: {OUTPUT_DIR}/')

print(f'\n[*] Статистика:')
print(f'   Всего ГРС: {len(LINE_IDS)}')
print(f'   Всего записей: {len(analysis)}')
print(f'   Период: {analysis["date"].min()} - {analysis["date"].max()}')

print(f'\n[*] Графики созданы:')
graph_count = sum([
    len(list((OUTPUT_DIR / subdir).glob('*.png')))
    for subdir in ['timeseries', 'seasonal', 'scatter', 'distributions',
                   'heatmaps', 'outliers', 'comparison']
])
print(f'   Всего PNG файлов: {graph_count}')

print(f'\n[OK] EDA успешно завершен!')
print(f'\n[INFO] Следующие шаги:')
print(f'   1. Проанализируйте Excel файл с результатами')
print(f'   2. Изучите графики в {OUTPUT_DIR}/')
print(f'   3. Найдите инсайты для улучшения модели')
print(f'   4. Обратите внимание на проблемную ГРС 25 (R^2 = -0.32)')
