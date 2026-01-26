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

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 200)

# === Конфігурація ===
# Шляхи відносно розташування скрипта
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / 'data'
OUTPUT_DIR = DATA_DIR / 'analysis_output'

DB_URL = 'postgresql+psycopg2://postgres:assembler@localhost:5432/hostlib_db'
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
analysis['month'] = analysis['date'].dt.month

print(f'analysis: {analysis.shape}')
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
# Блок 8 — Лінійна регресія
# ============================================================================

# Лінійна регресія для кожної ГРС
# Feature: temp_kelvin, Target: population_volume
# Train: місяці 1-11, Test: місяць 12

reg_results = []
models = {}

for lid in sorted(analysis['line_id'].unique()):
    subset = analysis[analysis['line_id'] == lid].dropna(subset=['temp_kelvin', 'population_volume'])

    train = subset[subset['month'].isin(TRAIN_MONTHS)]
    test = subset[subset['month'] == TEST_MONTH]

    if len(train) < 10 or len(test) < 1:
        print(f'⚠ line_id={lid}: недостатньо даних (train={len(train)}, test={len(test)}), пропуск')
        continue

    X_train = train[['temp_kelvin']].values
    y_train = train['population_volume'].values
    X_test = test[['temp_kelvin']].values
    y_test = test['population_volume'].values

    model = LinearRegression()
    model.fit(X_train, y_train)

    y_pred_train = model.predict(X_train)
    y_pred_test = model.predict(X_test)

    mae_test = mean_absolute_error(y_test, y_pred_test)
    r2_train = r2_score(y_train, y_pred_train)
    r2_test = r2_score(y_test, y_pred_test)

    reg_results.append({
        'line_id': lid,
        'GRS': lineid_to_grs.get(lid, '?'),
        'coef': round(model.coef_[0], 4),
        'intercept': round(model.intercept_, 2),
        'MAE_test': round(mae_test, 2),
        'R2_train': round(r2_train, 4),
        'R2_test': round(r2_test, 4),
        'N_train': len(train),
        'N_test': len(test),
    })
    models[lid] = (model, train, test)

reg_df = pd.DataFrame(reg_results)
print('\nРезультати лінійної регресії: temp_kelvin → population_volume')
print(reg_df)

# Графіки: regression line + scatter (train/test) для кожної ГРС
n_models = len(models)
ncols = 2
nrows = (n_models + ncols - 1) // ncols

fig, axes = plt.subplots(nrows, ncols, figsize=(14, 5 * nrows))
axes = axes.flatten()

for i, (lid, (model, train, test)) in enumerate(sorted(models.items())):
    ax = axes[i]

    # Train scatter
    ax.scatter(train['temp_kelvin'], train['population_volume'],
               alpha=0.4, s=15, color='blue', label='Train (міс. 1-11)')
    # Test scatter
    ax.scatter(test['temp_kelvin'], test['population_volume'],
               alpha=0.7, s=25, color='red', marker='x', label='Test (міс. 12)')

    # Regression line
    x_range = np.linspace(
        analysis[analysis['line_id'] == lid]['temp_kelvin'].min(),
        analysis[analysis['line_id'] == lid]['temp_kelvin'].max(),
        100
    ).reshape(-1, 1)
    ax.plot(x_range, model.predict(x_range), 'g--', linewidth=2, label='Regression')

    r2_test_val = reg_df[reg_df['line_id'] == lid].iloc[0]['R2_test']
    ax.set_title(f'{lineid_to_grs.get(lid, lid)} (R² test={r2_test_val:.4f})')
    ax.set_xlabel('Температура (K)')
    ax.set_ylabel('Об\'єм населення')
    ax.legend(fontsize=8)

for j in range(i + 1, len(axes)):
    axes[j].set_visible(False)

plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'regression_lines.png', dpi=150, bbox_inches='tight')
print(f'\nЗбережено графік: {OUTPUT_DIR / "regression_lines.png"}')
plt.close()

# Графіки: actual vs predicted для грудня (Test)
n_models = len(models)
ncols = 2
nrows = (n_models + ncols - 1) // ncols

fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows))
axes = axes.flatten()

for i, (lid, (model, train, test)) in enumerate(sorted(models.items())):
    ax = axes[i]

    y_pred = model.predict(test[['temp_kelvin']].values)
    dates = test['date'].values

    ax.plot(dates, test['population_volume'].values, 'b-o', markersize=3, label='Actual')
    ax.plot(dates, y_pred, 'r--s', markersize=3, label='Predicted')
    ax.set_title(f'{lineid_to_grs.get(lid, lid)} — Грудень 2025')
    ax.set_xlabel('Дата')
    ax.set_ylabel('Об\'єм населення')
    ax.legend(fontsize=8)
    ax.tick_params(axis='x', rotation=45)

for j in range(i + 1, len(axes)):
    axes[j].set_visible(False)

plt.tight_layout()
plt.savefig(OUTPUT_DIR / 'actual_vs_predicted.png', dpi=150, bbox_inches='tight')
print(f'Збережено графік: {OUTPUT_DIR / "actual_vs_predicted.png"}')
plt.close()

# Діагностика: розподіл помилок на тесті
print('\nДіагностика помилок на тесті:')
for lid, (model, train, test) in sorted(models.items()):
    y_pred = model.predict(test[['temp_kelvin']].values)
    residuals = test['population_volume'].values - y_pred
    print(f'{lineid_to_grs.get(lid, lid)} (line_id={lid}):')
    print(f'  Residuals: mean={residuals.mean():.2f}, std={residuals.std():.2f}, '
          f'min={residuals.min():.2f}, max={residuals.max():.2f}')
    print(f'  MAPE: {(np.abs(residuals) / np.abs(test["population_volume"].values + 1e-9)).mean() * 100:.1f}%')
    print()

# ============================================================================
# Блок 9 — Зведені результати
# ============================================================================

# Фінальна таблиця: GRS, Pearson r, p-value, MAE, R² test
final = corr_df[['line_id', 'GRS', 'Pearson_r', 'p_value', 'Significant']].merge(
    reg_df[['line_id', 'MAE_test', 'R2_train', 'R2_test', 'coef', 'intercept']],
    on='line_id',
    how='left'
)

print('=' * 100)
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
        coef = row['coef']
        print(f'  Регресія: R²_test={r2:.4f}, MAE={mae:.2f}')
        print(f'  Коефіцієнт: {coef:.4f} (при зростанні T на 1K об\'єм змінюється на {coef:.2f})')
    print()

# Експорт у Excel
output_file = DATA_DIR / 'analysis_population_gas_results.xlsx'
with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
    final.to_excel(writer, sheet_name='Summary', index=False)
    corr_df.to_excel(writer, sheet_name='Correlation', index=False)
    reg_df.to_excel(writer, sheet_name='Regression', index=False)
    enterprise_daily.to_excel(writer, sheet_name='Enterprise_Daily', index=False)

print(f'\nРезультати збережено у {output_file}')
print(f'Графіки збережено у директорії {OUTPUT_DIR}/')
print('\nАналіз завершено успішно!')
