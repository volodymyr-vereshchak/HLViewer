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

TEMPERATURE_LINE_ID = 19
TRAIN_MONTHS = list(range(1, 12))  # січень–листопад
TEST_MONTH = 12                     # грудень

# Створити директорію для виводу
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(DB_URL)
print('DB engine створено:', engine.url)

# ============================================================================
# Блок 2 — Маппінг ГРС → line_id
# ============================================================================

# Завантаження маппінгу з Excel (без заголовка, 2 колонки: назва ГРС, line_id)
grs_map_df = pd.read_excel(LINE_ID_FILE, sheet_name='line_id', header=None, names=['grs_name', 'line_id'])
print(f'Всього записів у файлі: {len(grs_map_df)}')

# Цільові 8 ГРС (line_id 15-25, без 1003)
TARGET_LINE_IDS = [15, 16, 20, 21, 22, 23, 24, 25]
grs_map_df = grs_map_df[grs_map_df['line_id'].isin(TARGET_LINE_IDS)].copy()
grs_map_df = grs_map_df.reset_index(drop=True)

# Словник: назва ГРС → line_id
grs_to_lineid = dict(zip(grs_map_df['grs_name'], grs_map_df['line_id']))
# Зворотній словник: line_id → назва ГРС
lineid_to_grs = dict(zip(grs_map_df['line_id'], grs_map_df['grs_name']))

print(f'Цільових ГРС: {len(grs_map_df)}')
print()
for name, lid in grs_to_lineid.items():
    print(f'  {name} → line_id={lid}')

# Перевірка дублікатів назв
dup_names = grs_map_df['grs_name'].duplicated(keep=False)
if dup_names.any():
    print('\n⚠ Знайдено дублікати назв ГРС:')
    print(grs_map_df[dup_names])
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
ent_long['date'] = ent_long['date_col'].map(date_map)
ent_long['enterprise_volume'] = pd.to_numeric(ent_long['enterprise_volume'], errors='coerce').fillna(0)

# Агрегація (sum) по line_id + date
enterprise_daily = (
    ent_long
    .groupby(['line_id', 'date'], as_index=False)['enterprise_volume']
    .sum()
)

print(f'enterprise_daily: {enterprise_daily.shape}')
print(f'line_id у даних: {sorted(enterprise_daily["line_id"].unique())}')

# ============================================================================
# Блок 4 — Об'єми ліній з БД
# ============================================================================

# SQL запит до daily_archive для цільових line_id
line_ids_str = ', '.join(str(x) for x in TARGET_LINE_IDS)

sql_lines = text(f"""
    SELECT line_id, period AS date, volume AS line_volume
    FROM daily_archive
    WHERE line_id IN ({line_ids_str})
      AND period BETWEEN '2025-01-01' AND '2025-12-31'
    ORDER BY line_id, period
""")

with engine.connect() as conn:
    line_volumes = pd.read_sql(sql_lines, conn)

line_volumes['date'] = pd.to_datetime(line_volumes['date'])
print(f'line_volumes: {line_volumes.shape}')
print(f'line_id у БД: {sorted(line_volumes["line_id"].unique())}')

# Перевірка яких line_id немає в БД
missing_in_db = set(TARGET_LINE_IDS) - set(line_volumes['line_id'].unique())
if missing_in_db:
    print(f'⚠ Наступні line_id відсутні в БД: {missing_in_db}')
    for lid in missing_in_db:
        print(f'  line_id={lid} ({lineid_to_grs.get(lid, "?")})')
else:
    print('Всі цільові line_id знайдено в БД.')

# Статистика по кожному line_id
for lid in sorted(line_volumes['line_id'].unique()):
    subset = line_volumes[line_volumes['line_id'] == lid]
    print(f'  line_id={lid} ({lineid_to_grs.get(lid, "?")}): {len(subset)} днів, '
          f'volume range=[{subset["line_volume"].min():.1f}, {subset["line_volume"].max():.1f}]')

# ============================================================================
# Блок 5 — Температура з БД
# ============================================================================

# SQL запит до daily_archive для line_id=19 (температура)
sql_temp = text(f"""
    SELECT period AS date, temperature
    FROM daily_archive
    WHERE line_id = {TEMPERATURE_LINE_ID}
      AND period BETWEEN '2025-01-01' AND '2025-12-31'
    ORDER BY period
""")

with engine.connect() as conn:
    temp_df = pd.read_sql(sql_temp, conn)

temp_df['date'] = pd.to_datetime(temp_df['date'])
print(f'Записів температури: {len(temp_df)}')
print(f'Діапазон дат: {temp_df["date"].min().date()} .. {temp_df["date"].max().date()}')
print(f'Температура (°C): min={temp_df["temperature"].min():.1f}, max={temp_df["temperature"].max():.1f}, mean={temp_df["temperature"].mean():.1f}')

# Конвертація °C → K
temp_df['temp_kelvin'] = temp_df['temperature'] + 273.15

# Можливі дублікати по даті (кілька записів для одного дня) — агрегуємо
temp_daily = temp_df.groupby('date', as_index=False).agg(
    temperature=('temperature', 'mean'),
    temp_kelvin=('temp_kelvin', 'mean')
)

print(f'Унікальних днів температури: {len(temp_daily)}')
print(f'temp_kelvin: min={temp_daily["temp_kelvin"].min():.2f}, max={temp_daily["temp_kelvin"].max():.2f}')

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

# Merge з температурою по date
analysis = merged.merge(temp_daily[['date', 'temp_kelvin', 'temperature']], on='date', how='inner')

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
ncols = 2
nrows = (n_grs + ncols - 1) // ncols

fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows))
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

# ============================================================================
# Блок 8 — Лінійна регресія з розширеними фічами
# ============================================================================

# Упрощенный список фич для единой модели (7 фич)
FEATURE_COLS = [
    'temperature',        # Основная температура (°C)
    'temp_ma7',          # Скользящее среднее 7 дней
    'day_of_week',       # День недели (0-6)
    'pop_volume_lag7',   # Потребление 7 дней назад
    'pop_volume_ma7',    # Среднее потребление 7 дней
    'line_id_feature',   # ID ГРС
    'grs_avg_volume',    # Средний объем ГРС
]

print(f'\n=== Навчання ЄДИНОЇ моделі для всіх ГРС з {len(FEATURE_COLS)} спрощеними фічами ===')
print(f'Фічі: {", ".join(FEATURE_COLS)}')

# Подготовка данных для ВСЕХ ГРС сразу (единая модель)
from sklearn.linear_model import Ridge

analysis_clean = analysis.dropna(subset=['population_volume'] + FEATURE_COLS)

train = analysis_clean[analysis_clean['month'].isin(TRAIN_MONTHS)]
test = analysis_clean[analysis_clean['month'] == TEST_MONTH]

print(f'\nРозмір тренувальної вибірки: {len(train)} сэмплов ({len(train)//365:.0f} ГРС × ~365 днів)')
print(f'Розмір тестової вибірки: {len(test)} сэмплов ({len(test)//31:.0f} ГРС × ~31 день)')

X_train = train[FEATURE_COLS].values
y_train = train['population_volume'].values
X_test = test[FEATURE_COLS].values
y_test = test['population_volume'].values

# Обучение ОДНОЙ модели с Ridge регрессией (регуляризация против переобучения)
model = Ridge(alpha=1.0)
model.fit(X_train, y_train)

# Прогноз
y_pred_train = model.predict(X_train)
y_pred_test = model.predict(X_test)

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

    y_pred_train_grs = model.predict(X_train_grs)
    y_pred_test_grs = model.predict(X_test_grs)

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
print('\n=== Таблиця результатів для кожної ГРС ===')
print(reg_df)

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

# Графіки: actual vs predicted для грудня (Test) + загальний графік
print('\n=== Створення графіків actual vs predicted ===')

# 1. Загальний графік для всіх ГРС
fig, ax = plt.subplots(figsize=(12, 6))
ax.scatter(y_test, y_pred_test, alpha=0.5, s=20, edgecolors='k', linewidths=0.5)
ax.plot([y_test.min(), y_test.max()], [y_test.min(), y_test.max()], 'r--', lw=2, label='Ідеальний прогноз')
ax.set_xlabel('Фактичний об\'єм (м³)')
ax.set_ylabel('Прогнозований об\'єм (м³)')
ax.set_title(f'Actual vs Predicted (всі ГРС, грудень 2025)\nR²={r2_test_overall:.4f}, MAE={mae_test_overall:.0f} м³')
ax.legend()
ax.grid(alpha=0.3)
plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'actual_vs_predicted_overall.png', dpi=150, bbox_inches='tight')
print(f'Збережено графік: {OUTPUT_DIR / "actual_vs_predicted_overall.png"}')
plt.close()

# 2. Графіки для кожної ГРС окремо
n_models = len(models)
ncols = 2
nrows = (n_models + ncols - 1) // ncols

fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows))
axes = axes.flatten()

for i, (lid, (model, train_grs, test_grs, X_train, X_test, y_train, y_test)) in enumerate(sorted(models.items())):
    ax = axes[i]

    y_pred = model.predict(X_test)
    dates = test_grs['date'].values

    ax.plot(dates, y_test, 'b-o', markersize=3, label='Actual')
    ax.plot(dates, y_pred, 'r--s', markersize=3, label='Predicted')

    r2_test_val = reg_df[reg_df['line_id'] == lid].iloc[0]['R2_test']
    mae_test_val = reg_df[reg_df['line_id'] == lid].iloc[0]['MAE_test']

    ax.set_title(f'{lineid_to_grs.get(lid, lid)} — Грудень 2025 (єдина модель)\nR²={r2_test_val:.4f}, MAE={mae_test_val:.0f}')
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

# Фінальна таблиця: GRS, Pearson r, p-value, MAE, R² test
final = corr_df[['line_id', 'GRS', 'Pearson_r', 'p_value', 'Significant']].merge(
    reg_df[['line_id', 'MAE_test', 'R2_train', 'R2_test']],
    on='line_id',
    how='left'
)

print('\n' + '=' * 100)
print('ЗВЕДЕНА ТАБЛИЦЯ: Кореляція та регресія — температура vs об\'єм газу населення')
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

    # Сила кореляції
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
        print(f'  Регресія (єдина модель): R²_test={r2:.4f}, R²_train={r2_train:.4f}, MAE_test={mae:.2f} м³')
    print()

# Експорт у Excel
output_file = DATA_DIR / 'analysis_population_gas_results.xlsx'

# Підготовка таблиці важливості фіч (глобальна модель)
feature_imp_df = pd.DataFrame([
    {'Feature': feat, 'Importance': round(importance, 4)}
    for feat, importance in sorted(feature_importance_global.items(), key=lambda x: x[1], reverse=True)
])

# Додаткові загальні метрики
overall_metrics_df = pd.DataFrame([{
    'Metric': 'Overall R²_train',
    'Value': round(r2_train_overall, 4)
}, {
    'Metric': 'Overall R²_test',
    'Value': round(r2_test_overall, 4)
}, {
    'Metric': 'Overall MAE_test',
    'Value': round(mae_test_overall, 2)
}, {
    'Metric': 'Train samples',
    'Value': len(train)
}, {
    'Metric': 'Test samples',
    'Value': len(test)
}])

with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
    final.to_excel(writer, sheet_name='Summary', index=False)
    overall_metrics_df.to_excel(writer, sheet_name='Overall_Metrics', index=False)
    corr_df.to_excel(writer, sheet_name='Correlation', index=False)
    reg_df.to_excel(writer, sheet_name='Regression', index=False)
    feature_imp_df.to_excel(writer, sheet_name='Feature_Importance', index=False)
    enterprise_daily.to_excel(writer, sheet_name='Enterprise_Daily', index=False)

print(f'\n{"="*80}')
print(f'Результати збережено у {output_file}')
print(f'Графіки збережено у директорії {OUTPUT_DIR}/')
print(f'  - scatter_temp_vs_population.png')
print(f'  - correlation_heatmap.png')
print(f'  - feature_importance.png (важливість фіч єдиної моделі)')
print(f'  - actual_vs_predicted_overall.png (НОВИЙ - загальний графік для всіх ГРС)')
print(f'  - actual_vs_predicted.png (графіки для кожної ГРС)')
print(f'{"="*80}')
print(f'\n=== ПІДСУМОК ===')
print(f'Використано ЄДИНУ модель Ridge регресії для всіх {len(models)} ГРС')
print(f'Навчальна вибірка: {len(train)} сэмплов')
print(f'Тестова вибірка: {len(test)} сэмплов')
print(f'Кількість фіч: {len(FEATURE_COLS)}')
print(f'\nЗагальні метрики:')
print(f'  R²_test:  {r2_test_overall:.4f}')
print(f'  MAE_test: {mae_test_overall:.2f} м³')
print(f'  Переобучення (R²_train - R²_test): {r2_train_overall - r2_test_overall:.4f}')
print(f'\nАналіз завершено успішно!')
