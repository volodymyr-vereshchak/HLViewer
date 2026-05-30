"""
Трёхрежимная гибридная модель: холод + переходный + лето
=======================================================

Режимы (по данным диагностики):
1. ХОЛОД (T < 15C, не лето): corr -0.886...-0.924, полный набор признаков
2. ПЕРЕХОДНЫЙ (T >= 15C, не лето): апрель-май, сентябрь-октябрь
3. ЛЕТО (июнь-август): отдельная модель, т.к.:
   - потребление = 5-7% от зимнего, очень low variance (CV 0.05-0.14)
   - корреляция T-vol = -0.65...-0.82 (есть сигнал!)
   - двухрежимная warm-модель размывала летний паттерн переходными месяцами

Baseline для сравнения:
  Двухрежимная: R2_d=0.669, R2_h=0.473, MAPE=8.0% (отопит.)
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from sqlalchemy import create_engine, text
from sklearn.linear_model import RidgeCV, LassoCV, ElasticNetCV
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error, r2_score
import json
import warnings
from pathlib import Path
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')
plt.rcParams['font.family'] = 'DejaVu Sans'
plt.rcParams['axes.unicode_minus'] = False

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = SCRIPT_DIR / 'data'
OUTPUT_DIR = DATA_DIR / 'tri_regime_output'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(PROJECT_ROOT / '.env')
DB_USER = os.getenv('POSTGRES_USER', 'postgres')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'assembler')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('POSTGRES_DB', 'hostlib_db')
DB_URL = f'postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'

LINE_ID_FILE = DATA_DIR / 'line_id_2025.xlsx'
VOLUME_FILE = DATA_DIR / 'volume_2025.xlsx'
VIRTUAL_LINES_FILE = DATA_DIR / 'virtual_lines.json'
WEATHER_HOURLY_FILE = DATA_DIR / 'weather_2025_h.csv'

DATA_START = pd.to_datetime('2025-01-01')
DATA_END = pd.to_datetime('2025-12-31')
TARGET_LINE_IDS = [1003, 1004, 16, 21, 23, 24, 25]
EXCLUDE_PRESSURE_LINES = [16, 1003]
ALPHA_GRID = np.logspace(-2, 4, 50)
TEMP_THRESHOLD = 15.0
SUMMER_MONTHS = [6, 7, 8]

print('=' * 80)
print('ТРЁХРЕЖИМНАЯ МОДЕЛЬ: ХОЛОД + ПЕРЕХОДНЫЙ + ЛЕТО + ЧАСОВОЙ ПРОФИЛЬ')
print('=' * 80)

engine = create_engine(DB_URL)

# ============================================================================
# 1. ЗАГРУЗКА ДАННЫХ (идентично dual_regime)
# ============================================================================
print('\n[1/7] Загрузка данных...')

VIRTUAL_LINES = {}
if VIRTUAL_LINES_FILE.exists():
    with open(VIRTUAL_LINES_FILE, 'r', encoding='utf-8') as f:
        vl_data = json.load(f)
    for vid_str, info in vl_data.get('virtual_lines', {}).items():
        VIRTUAL_LINES[int(vid_str)] = info['physical_line_ids']

expanded_line_ids = set(TARGET_LINE_IDS)
for vid in TARGET_LINE_IDS:
    if vid >= 1000 and vid in VIRTUAL_LINES:
        expanded_line_ids.update(VIRTUAL_LINES[vid])

grs_map_df = pd.read_excel(LINE_ID_FILE, sheet_name='line_id', header=None,
                            names=['grs_name', 'line_id'])
grs_map_df = grs_map_df[grs_map_df['line_id'].isin(expanded_line_ids)].copy()
grs_to_lineid = dict(zip(grs_map_df['grs_name'], grs_map_df['line_id']))
lineid_to_grs = dict(zip(grs_map_df['line_id'], grs_map_df['grs_name']))

with open(VIRTUAL_LINES_FILE, 'r', encoding='utf-8') as f:
    vl_full = json.load(f)
for vid in TARGET_LINE_IDS:
    if vid >= 1000 and vid not in lineid_to_grs:
        lineid_to_grs[vid] = vl_full['virtual_lines'].get(str(vid), {}).get('name', f'V_{vid}')

physical_target_ids = [lid for lid in TARGET_LINE_IDS if lid < 1000]
virtual_target_ids = [lid for lid in TARGET_LINE_IDS if lid >= 1000]

physical_ids_str = ','.join(map(str, physical_target_ids))
with engine.connect() as conn:
    df_physical = pd.read_sql(text(f"""
    SELECT gvl.id as line_id, ha.period as datetime, ha.volume, ha.temperature, ha.pressure
    FROM hourly_archive ha INNER JOIN gas_volume_line gvl ON ha.line_id = gvl.id
    WHERE gvl.id IN ({physical_ids_str})
      AND ha.period >= '{DATA_START}' AND ha.period <= '{DATA_END}'
    ORDER BY gvl.id, ha.period"""), conn)
print(f'Физические линии: {len(df_physical):,}')

df_virtual_list = []
for vid in virtual_target_ids:
    if vid not in VIRTUAL_LINES: continue
    phys_ids_str = ','.join(map(str, VIRTUAL_LINES[vid]))
    with engine.connect() as conn:
        df_virt = pd.read_sql(text(f"""
        SELECT ha.period as datetime, SUM(ha.volume) as volume,
               AVG(ha.temperature) as temperature, AVG(ha.pressure) as pressure
        FROM hourly_archive ha INNER JOIN gas_volume_line gvl ON ha.line_id = gvl.id
        WHERE gvl.id IN ({phys_ids_str})
          AND ha.period >= '{DATA_START}' AND ha.period <= '{DATA_END}'
        GROUP BY ha.period ORDER BY ha.period"""), conn)
    df_virt['line_id'] = vid
    df_virtual_list.append(df_virt)
    print(f'Виртуальная линия {vid}: {len(df_virt):,}')

hourly_lines = pd.concat([df_physical] + df_virtual_list, ignore_index=True) if df_virtual_list else df_physical.copy()
hourly_lines['datetime'] = pd.to_datetime(hourly_lines['datetime'])
hourly_lines = hourly_lines.sort_values(['line_id', 'datetime']).reset_index(drop=True)

if WEATHER_HOURLY_FILE.exists():
    weather = pd.read_csv(WEATHER_HOURLY_FILE)
    dcol = 'date' if 'date' in weather.columns else 'datetime'
    weather[dcol] = pd.to_datetime(weather[dcol])
    if dcol == 'date': weather = weather.rename(columns={'date': 'datetime'})
    hourly_lines = hourly_lines.merge(
        weather[['datetime', 'temperature']].rename(columns={'temperature': 'wt'}),
        on='datetime', how='left')
    hourly_lines['temperature'] = hourly_lines['wt'].fillna(hourly_lines['temperature'])
    hourly_lines.drop(columns=['wt'], inplace=True)

print(f'Всего: {len(hourly_lines):,}')

# ============================================================================
# 2. ВЫЧИТАНИЕ ПРЕДПРИЯТИЙ
# ============================================================================
print('\n[2/7] Вычитание предприятий...')

hourly_lines['date'] = pd.to_datetime(hourly_lines['datetime'].dt.date)
hourly_lines['month'] = hourly_lines['datetime'].dt.month
hourly_lines['hour'] = hourly_lines['datetime'].dt.hour
hourly_lines['contract_date'] = hourly_lines['date'].copy()
hourly_lines.loc[hourly_lines['hour'] < 7, 'contract_date'] = (
    hourly_lines.loc[hourly_lines['hour'] < 7, 'date'] - pd.Timedelta(days=1))

vol_raw = pd.read_excel(VOLUME_FILE, sheet_name='TDSheet')
cols = list(vol_raw.columns); cols[0] = 'enterprise_name'; cols[1] = 'enterprise_line'
vol_raw.columns = cols
vol_raw = vol_raw[vol_raw['enterprise_name'].notna()].copy()
vol_raw = vol_raw[vol_raw['enterprise_name'] != 'Підприємство'].copy()
for col in vol_raw.columns:
    if str(col).lower() in ('грс', 'grs'):
        vol_raw = vol_raw.rename(columns={col: 'grs_col'}); break
if 'grs_col' not in vol_raw.columns: vol_raw['grs_col'] = vol_raw['enterprise_name']
vol_raw['line_id'] = vol_raw['grs_col'].map(grs_to_lineid)

date_cols = []
for col in vol_raw.columns:
    if isinstance(col, datetime): date_cols.append(col)
    elif isinstance(col, str):
        cc = col.replace(' р.', '').strip()
        if cc.count('.') == 2 or cc.count('/') == 2 or cc.count('-') == 2:
            try: pd.to_datetime(cc, dayfirst=True); date_cols.append(col)
            except: pass

enterprise_volumes = pd.DataFrame()
if date_cols:
    ent_long = vol_raw.melt(
        id_vars=['enterprise_name', 'enterprise_line', 'grs_col', 'line_id'],
        value_vars=date_cols, var_name='date', value_name='ev')
    ent_long['date'] = ent_long['date'].astype(str).str.replace(' р.', '').str.strip()
    ent_long['date'] = pd.to_datetime(ent_long['date'], dayfirst=True, errors='coerce')
    ent_long = ent_long.dropna(subset=['date', 'line_id'])
    ent_long['ev'] = pd.to_numeric(ent_long['ev'], errors='coerce').fillna(0)
    enterprise_volumes = ent_long.groupby(['line_id', 'date'], as_index=False)['ev'].sum()
    enterprise_volumes = enterprise_volumes.rename(columns={'ev': 'daily_ent'})
    phys_to_virt = {}
    for vid, pids in VIRTUAL_LINES.items():
        for pid in pids: phys_to_virt[pid] = vid
    if phys_to_virt:
        remapped = enterprise_volumes['line_id'].map(phys_to_virt)
        has = remapped.notna()
        if has.any():
            enterprise_volumes.loc[has, 'line_id'] = remapped[has].astype(int)
            enterprise_volumes = enterprise_volumes.groupby(['line_id', 'date'], as_index=False)['daily_ent'].sum()
    enterprise_volumes = enterprise_volumes[
        (enterprise_volumes['date'] >= DATA_START) & (enterprise_volumes['date'] <= DATA_END)]
    print(f'Предприятия: {len(enterprise_volumes):,}')

if not enterprise_volumes.empty:
    hourly_data = hourly_lines.merge(
        enterprise_volumes.rename(columns={'date': 'contract_date'}),
        on=['line_id', 'contract_date'], how='left')
    hourly_data['daily_ent'] = hourly_data['daily_ent'].fillna(0)
    hourly_data['hourly_ent'] = hourly_data['daily_ent'] / 24
else:
    hourly_data = hourly_lines.copy()
    hourly_data['hourly_ent'] = 0

hourly_data['pop_vol'] = (hourly_data['volume'] - hourly_data['hourly_ent']).clip(lower=0)

# ============================================================================
# 3. ОЧИСТКА АНОМАЛИЙ
# ============================================================================
print('\n[3/7] Очистка аномалий...')
from data_cleaning import clean_data_pipeline
hourly_data = clean_data_pipeline(
    hourly_data, line_id_col='line_id', value_col='pop_vol',
    pressure_col='pressure', datetime_col='datetime', month_col='month',
    method='hybrid', max_gap=2, verbose=True)

# ============================================================================
# 4. АГРЕГАЦИЯ В КОНТРАКТНЫЕ СУТКИ
# ============================================================================
print('\n[4/7] Агрегация + признаки...')

hourly_data['hour'] = hourly_data['datetime'].dt.hour
hourly_data['contract_date'] = hourly_data['datetime'].apply(
    lambda x: pd.Timestamp(x.date()) if x.hour >= 7 else pd.Timestamp((x - timedelta(days=1)).date()))

daily_data = hourly_data.groupby(['contract_date', 'line_id']).agg(
    volume_sum=('pop_vol', 'sum'),
    temp_mean=('temperature', 'mean'),
    temp_min=('temperature', 'min'),
    temp_max=('temperature', 'max'),
    temp_std=('temperature', 'std'),
    pressure_mean=('pressure', 'mean'),
    pressure_std=('pressure', 'std'),
    pressure_min=('pressure', 'min'),
    pressure_max=('pressure', 'max'),
    n_hours=('pop_vol', 'count')
).reset_index()

daily_data = daily_data[daily_data['n_hours'] >= 20].copy()
daily_data['temp_range'] = daily_data['temp_max'] - daily_data['temp_min']
daily_data['pressure_range'] = daily_data['pressure_max'] - daily_data['pressure_min']
daily_data['month'] = daily_data['contract_date'].dt.month

# Три режима
daily_data['is_summer'] = daily_data['month'].isin(SUMMER_MONTHS).astype(int)
daily_data['is_cold'] = ((daily_data['temp_mean'] < TEMP_THRESHOLD) & (daily_data['is_summer'] == 0)).astype(int)
daily_data['is_transition'] = ((daily_data['temp_mean'] >= TEMP_THRESHOLD) & (daily_data['is_summer'] == 0)).astype(int)

n_cold = daily_data['is_cold'].sum()
n_trans = daily_data['is_transition'].sum()
n_summer = daily_data['is_summer'].sum()
print(f'Суточных записей: {len(daily_data):,}')
print(f'Холод: {n_cold}, Переходный: {n_trans}, Лето: {n_summer}')


def create_daily_features(df):
    df = df.copy().sort_values('contract_date').reset_index(drop=True)
    df['day_of_week'] = df['contract_date'].dt.dayofweek
    df['day_of_year'] = df['contract_date'].dt.dayofyear
    df['is_weekend'] = (df['day_of_week'] >= 5).astype(int)
    df['month_sin'] = np.sin(2 * np.pi * df['month'] / 12)
    df['month_cos'] = np.cos(2 * np.pi * df['month'] / 12)
    df['doy_sin'] = np.sin(2 * np.pi * df['day_of_year'] / 365)
    df['doy_cos'] = np.cos(2 * np.pi * df['day_of_year'] / 365)
    df['is_heating'] = df['month'].isin([1, 2, 3, 10, 11, 12]).astype(int)
    df['is_winter'] = df['month'].isin([12, 1, 2]).astype(int)
    for lag in [1, 2, 3, 7]:
        df[f'temp_lag_{lag}d'] = df['temp_mean'].shift(lag)
    df['temp_ma_3d'] = df['temp_mean'].rolling(3, min_periods=1).mean()
    df['temp_ma_7d'] = df['temp_mean'].rolling(7, min_periods=1).mean()
    df['temp_diff_1d'] = df['temp_mean'] - df['temp_mean'].shift(1)
    df['temp_diff_3d'] = df['temp_mean'] - df['temp_mean'].shift(3)
    df['temp_sq'] = df['temp_mean'] ** 2
    df['temp_x_month_cos'] = df['temp_mean'] * df['month_cos']
    df['temp_x_month_sin'] = df['temp_mean'] * df['month_sin']
    df['temp_x_weekend'] = df['temp_mean'] * df['is_weekend']
    df['pressure_lag_1d'] = df['pressure_mean'].shift(1)
    df['pressure_lag_3d'] = df['pressure_mean'].shift(3)
    df['pressure_ma_3d'] = df['pressure_mean'].rolling(3, min_periods=1).mean()
    df['pressure_diff_1d'] = df['pressure_mean'] - df['pressure_mean'].shift(1)
    return df


# Признаки COLD (полный набор)
COLD_BASE_FEATURES = [
    'temp_mean', 'temp_min', 'temp_max', 'temp_range', 'temp_std',
    'temp_lag_1d', 'temp_lag_2d', 'temp_lag_3d', 'temp_lag_7d',
    'temp_ma_3d', 'temp_ma_7d', 'temp_diff_1d', 'temp_diff_3d',
    'temp_sq', 'temp_x_month_cos', 'temp_x_month_sin', 'temp_x_weekend',
    'day_of_week', 'is_weekend', 'month_sin', 'month_cos', 'doy_sin', 'doy_cos', 'is_winter',
]

# Признаки TRANSITION (средний набор)
TRANS_BASE_FEATURES = [
    'temp_mean', 'temp_min', 'temp_max',
    'temp_lag_1d', 'temp_ma_3d',
    'day_of_week', 'is_weekend', 'month_sin', 'month_cos',
]

# Признаки SUMMER HOURLY (прямой часовой предикт — 24 Ridge моделей)
SUMMER_HOURLY_FEATURES = [
    'temperature', 'temp_lag_1h', 'temp_lag_3h', 'temp_ma_6h', 'temp_ma_24h',
    'temp_diff_1h', 'is_weekend',
]

PRESSURE_FEATURES = [
    'pressure_mean', 'pressure_std', 'pressure_range',
    'pressure_lag_1d', 'pressure_lag_3d', 'pressure_ma_3d', 'pressure_diff_1d',
]

# ============================================================================
# 5. MONTHLY CV
# ============================================================================
print('\n[5/7] Monthly CV (трёхрежимная модель)...')

months = list(range(1, 13))
month_names = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
               'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

all_hourly_preds = []
all_metrics = []


def fit_best_model(X_train, y_train, alpha_grid):
    best_model, best_score, best_name = None, -np.inf, ''
    for MC, nm in [(RidgeCV, 'Ridge'), (LassoCV, 'Lasso'), (ElasticNetCV, 'ElasticNet')]:
        try:
            if nm == 'ElasticNet':
                m = MC(alphas=alpha_grid, l1_ratio=[0.1, 0.5, 0.9], cv=5, max_iter=5000)
            elif nm == 'Lasso':
                m = MC(alphas=alpha_grid, cv=5, max_iter=5000)
            else:
                m = MC(alphas=alpha_grid, cv=5)
            m.fit(X_train, y_train)
            sc = m.score(X_train, y_train)
            if sc > best_score:
                best_score, best_model, best_name = sc, m, nm
        except Exception:
            continue
    return best_model, best_name, best_score


def predict_regime(train_df, test_df, features, alpha_grid, regime_name):
    """Обучает и предсказывает для одного режима. Возвращает предикты и название модели."""
    train_clean = train_df.dropna(subset=features + ['volume_sum'])
    test_clean = test_df.dropna(subset=features + ['volume_sum'])

    if len(test_clean) == 0:
        return np.array([]), np.array([]), np.array([]), '-', 0

    y_te = test_clean['volume_sum'].values
    dates_te = test_clean['contract_date'].values

    if len(train_clean) < 15:
        # Fallback: mean
        mean_val = train_clean['volume_sum'].mean() if len(train_clean) > 0 else test_clean['volume_sum'].mean()
        pred = np.full(len(y_te), mean_val)
        return pred, y_te, dates_te, 'Mean', 0

    X_tr = train_clean[features].values
    y_tr = train_clean['volume_sum'].values
    X_te = test_clean[features].values

    scaler = StandardScaler()
    X_tr_s = scaler.fit_transform(X_tr)
    X_te_s = scaler.transform(X_te)

    model, model_name, _ = fit_best_model(X_tr_s, y_tr, alpha_grid)

    if model is None:
        pred = np.full(len(y_te), y_tr.mean())
        return pred, y_te, dates_te, 'Fallback', 0

    pred = model.predict(X_te_s).clip(min=0)
    alpha = model.alpha_ if hasattr(model, 'alpha_') else 0
    return pred, y_te, dates_te, model_name, alpha


def build_hourly_profile(hourly_df, regime):
    """Строит часовой профиль для указанного режима (cold/transition/summer)."""
    hd = hourly_df.copy()
    hd['contract_date'] = hd['datetime'].apply(
        lambda x: pd.Timestamp(x.date()) if x.hour >= 7 else pd.Timestamp((x - timedelta(days=1)).date()))

    daily_vol = hd.groupby('contract_date')['pop_vol'].sum().rename('day_vol')
    hd = hd.merge(daily_vol, on='contract_date', how='left')
    hd['hour_share'] = hd['pop_vol'] / hd['day_vol'].clip(lower=1)

    daily_temp = hd.groupby('contract_date')['temperature'].mean().rename('day_temp')
    daily_month = hd.groupby('contract_date')['month'].first().rename('day_month')
    hd = hd.merge(daily_temp, on='contract_date', how='left')
    hd = hd.merge(daily_month, on='contract_date', how='left', suffixes=('', '_d'))

    if regime == 'cold':
        profile_data = hd[(hd['day_temp'] < TEMP_THRESHOLD) & (~hd['day_month'].isin(SUMMER_MONTHS))]
    elif regime == 'summer':
        profile_data = hd[hd['day_month'].isin(SUMMER_MONTHS)]
    else:  # transition
        profile_data = hd[(hd['day_temp'] >= TEMP_THRESHOLD) & (~hd['day_month'].isin(SUMMER_MONTHS))]

    if len(profile_data) < 200:
        profile_data = hd

    profile = profile_data.groupby('hour')['hour_share'].mean()
    for h in range(24):
        if h not in profile.index:
            profile[h] = 1.0 / 24
    profile = profile / profile.sum()
    return profile


for line_id in sorted(daily_data['line_id'].unique()):
    line_name = lineid_to_grs.get(line_id, str(line_id))
    line_daily = daily_data[daily_data['line_id'] == line_id].copy()
    line_daily = create_daily_features(line_daily)
    line_hourly = hourly_data[hourly_data['line_id'] == line_id].copy()

    has_pressure = line_id not in EXCLUDE_PRESSURE_LINES

    if has_pressure:
        cold_features = [f for f in COLD_BASE_FEATURES + PRESSURE_FEATURES if f in line_daily.columns]
        trans_features = [f for f in TRANS_BASE_FEATURES + ['pressure_mean'] if f in line_daily.columns]
        summer_h_features = SUMMER_HOURLY_FEATURES + ['pressure']
    else:
        cold_features = [f for f in COLD_BASE_FEATURES if f in line_daily.columns]
        trans_features = [f for f in TRANS_BASE_FEATURES if f in line_daily.columns]
        summer_h_features = SUMMER_HOURLY_FEATURES[:]

    # Подготовка часовых признаков для летней модели
    line_hourly_feat = line_hourly.copy()
    line_hourly_feat = line_hourly_feat.sort_values('datetime')
    line_hourly_feat['is_weekend'] = (line_hourly_feat['datetime'].dt.dayofweek >= 5).astype(int)
    line_hourly_feat['temp_lag_1h'] = line_hourly_feat['temperature'].shift(1)
    line_hourly_feat['temp_lag_3h'] = line_hourly_feat['temperature'].shift(3)
    line_hourly_feat['temp_lag_6h'] = line_hourly_feat['temperature'].shift(6)
    line_hourly_feat['temp_ma_6h'] = line_hourly_feat['temperature'].rolling(6, min_periods=1).mean()
    line_hourly_feat['temp_ma_24h'] = line_hourly_feat['temperature'].rolling(24, min_periods=1).mean()
    line_hourly_feat['temp_diff_1h'] = line_hourly_feat['temperature'].diff(1)

    # Pressure hourly features (for summer model)
    if has_pressure:
        line_hourly_feat['pressure_lag_1h'] = line_hourly_feat['pressure'].shift(1)
        line_hourly_feat['pressure_ma_6h'] = line_hourly_feat['pressure'].rolling(6, min_periods=1).mean()
        line_hourly_feat['pressure_diff_1h'] = line_hourly_feat['pressure'].diff(1)

    print(f'\n{"=" * 80}')
    print(f'ЛИНИЯ {line_id} - {line_name}')
    print(f'  Cold: {len(cold_features)} feat, Trans: {len(trans_features)} feat, Summer: 24xRidge({len(summer_h_features)} feat/hour)')
    print(f'{"=" * 80}')

    for test_month in months:
        test_name = month_names[test_month - 1]

        train_all = line_daily[line_daily['month'] != test_month].copy()
        test_all = line_daily[line_daily['month'] == test_month].copy()

        if len(test_all) < 5:
            continue

        # Разделяем тест по режимам
        test_is_summer = test_month in SUMMER_MONTHS
        if test_is_summer:
            test_summer = test_all.copy()
            test_cold = test_all.iloc[0:0]
            test_trans = test_all.iloc[0:0]
        else:
            test_summer = test_all.iloc[0:0]
            test_cold = test_all[test_all['temp_mean'] < TEMP_THRESHOLD]
            test_trans = test_all[test_all['temp_mean'] >= TEMP_THRESHOLD]

        # Разделяем train по режимам
        train_cold = train_all[(train_all['is_cold'] == 1)]
        train_trans = train_all[(train_all['is_transition'] == 1)]
        train_summer = train_all[(train_all['is_summer'] == 1)]

        all_pred = []
        all_true = []
        all_dates = []
        regime_dates = {}  # contract_date -> regime name
        models_used = {'cold': '-', 'trans': '-', 'summer': '-'}

        # COLD
        if len(test_cold) > 0:
            pred_c, true_c, dates_c, mn_c, _ = predict_regime(
                train_cold, test_cold, cold_features, ALPHA_GRID, 'cold')
            if len(pred_c) > 0:
                all_pred.append(pred_c)
                all_true.append(true_c)
                all_dates.append(dates_c)
                for d in dates_c: regime_dates[d] = 'cold'
                models_used['cold'] = mn_c

        # TRANSITION
        if len(test_trans) > 0:
            pred_t, true_t, dates_t, mn_t, _ = predict_regime(
                train_trans, test_trans, trans_features, ALPHA_GRID, 'transition')
            if len(pred_t) > 0:
                all_pred.append(pred_t)
                all_true.append(true_t)
                all_dates.append(dates_t)
                for d in dates_t: regime_dates[d] = 'transition'
                models_used['trans'] = mn_t

        # SUMMER: прямой часовой предикт (24 Ridge моделей)
        summer_hourly_preds = {}  # datetime -> predicted hourly volume
        if test_is_summer:
            train_summer_h = line_hourly_feat[
                (line_hourly_feat['month'].isin(SUMMER_MONTHS)) &
                (line_hourly_feat['month'] != test_month)
            ].dropna(subset=summer_h_features + ['pop_vol'])
            test_summer_h = line_hourly_feat[
                line_hourly_feat['month'] == test_month
            ].dropna(subset=summer_h_features + ['pop_vol'])

            if len(train_summer_h) > 100 and len(test_summer_h) > 100:
                models_used['summer'] = '24xRidge+'
                n_const_hours = 0
                n_outliers_removed = 0
                for h in range(24):
                    tr_h = train_summer_h[train_summer_h['hour'] == h]
                    te_h = test_summer_h[test_summer_h['hour'] == h]
                    if len(tr_h) < 10 or len(te_h) == 0:
                        mean_val = tr_h['pop_vol'].mean() if len(tr_h) > 0 else train_summer_h['pop_vol'].mean()
                        for _, row in te_h.iterrows():
                            summer_hourly_preds[row['datetime']] = mean_val
                        continue

                    # Outlier removal (3-sigma per hour)
                    n_before = len(tr_h)
                    mean_h = tr_h['pop_vol'].mean()
                    std_h = tr_h['pop_vol'].std()
                    if std_h > 0:
                        tr_h = tr_h[(tr_h['pop_vol'] >= mean_h - 3 * std_h) &
                                    (tr_h['pop_vol'] <= mean_h + 3 * std_h)]
                    n_outliers_removed += (n_before - len(tr_h))

                    # Stable hours with tiny volume: use median (e.g. Ternivka night 0-6)
                    mean_vol_h = tr_h['pop_vol'].mean()
                    cv_h = tr_h['pop_vol'].std() / max(mean_vol_h, 0.01)
                    if cv_h < 0.25 and mean_vol_h < 10:
                        median_val = tr_h['pop_vol'].median()
                        for _, row in te_h.iterrows():
                            summer_hourly_preds[row['datetime']] = median_val
                        n_const_hours += 1
                        continue

                    X_tr = tr_h[summer_h_features].values
                    y_tr = tr_h['pop_vol'].values
                    X_te = te_h[summer_h_features].values
                    sc = StandardScaler()
                    X_tr_s = sc.fit_transform(X_tr)
                    X_te_s = sc.transform(X_te)
                    m = RidgeCV(alphas=ALPHA_GRID, cv=5)
                    m.fit(X_tr_s, y_tr)
                    preds = m.predict(X_te_s).clip(min=0)
                    for dt_val, pred_val in zip(te_h['datetime'].values, preds):
                        summer_hourly_preds[dt_val] = pred_val
                if n_const_hours > 0 or n_outliers_removed > 0:
                    print(f'    Summer: {n_const_hours} const hours, {n_outliers_removed} outliers removed')

                # Агрегация в суточные для daily metrics
                test_summer_h_copy = test_summer_h.copy()
                test_summer_h_copy['pred_h'] = test_summer_h_copy['datetime'].map(summer_hourly_preds)
                test_summer_h_copy['contract_date'] = test_summer_h_copy['datetime'].apply(
                    lambda x: pd.Timestamp(x.date()) if x.hour >= 7 else pd.Timestamp((x - timedelta(days=1)).date()))
                daily_summer_agg = test_summer_h_copy.groupby('contract_date').agg(
                    vol_sum=('pop_vol', 'sum'), pred_sum=('pred_h', 'sum'), nh=('pop_vol', 'count')
                ).reset_index()
                daily_summer_agg = daily_summer_agg[daily_summer_agg['nh'] >= 20]

                if len(daily_summer_agg) > 0:
                    all_pred.append(daily_summer_agg['pred_sum'].values)
                    all_true.append(daily_summer_agg['vol_sum'].values)
                    all_dates.append(daily_summer_agg['contract_date'].values)
                    for d in daily_summer_agg['contract_date'].values:
                        regime_dates[d] = 'summer'

        if not all_pred:
            continue

        y_pred_daily = np.concatenate(all_pred)
        y_true_daily = np.concatenate(all_true)
        all_dates_arr = np.concatenate(all_dates)

        # Daily metrics
        r2_d = r2_score(y_true_daily, y_pred_daily)
        mae_d = mean_absolute_error(y_true_daily, y_pred_daily)
        mape_d = np.mean(np.abs(
            (y_true_daily - y_pred_daily) / np.clip(np.abs(y_true_daily), 1, None))) * 100

        # --- HOURLY PREDICTIONS ---
        train_hourly_raw = line_hourly[line_hourly['month'] != test_month].copy()
        profile_cold = build_hourly_profile(train_hourly_raw, 'cold')
        profile_trans = build_hourly_profile(train_hourly_raw, 'transition')

        # Map daily predictions (for cold/transition)
        pred_map = dict(zip(all_dates_arr, y_pred_daily))

        # Build hourly test set
        test_hourly = line_hourly[line_hourly['month'] == test_month].copy()
        test_hourly['contract_date'] = test_hourly['datetime'].apply(
            lambda x: pd.Timestamp(x.date()) if x.hour >= 7 else pd.Timestamp((x - timedelta(days=1)).date()))

        if test_is_summer:
            # Summer: direct hourly predictions
            test_hourly['pred_hourly'] = test_hourly['datetime'].map(
                lambda dt: summer_hourly_preds.get(dt, np.nan))
            test_hourly = test_hourly.dropna(subset=['pred_hourly'])
        else:
            # Cold/Transition: daily_pred * profile
            test_hourly['pred_daily'] = test_hourly['contract_date'].map(lambda cd: pred_map.get(cd, np.nan))
            test_hourly = test_hourly.dropna(subset=['pred_daily'])

            def get_hourly_pred(row):
                cd = row['contract_date']
                regime = regime_dates.get(cd, 'cold')
                if regime == 'transition':
                    return row['pred_daily'] * profile_trans.get(row['hour'], 1.0 / 24)
                else:
                    return row['pred_daily'] * profile_cold.get(row['hour'], 1.0 / 24)

            test_hourly['pred_hourly'] = test_hourly.apply(get_hourly_pred, axis=1)

        # Hourly metrics
        r2_h = r2_score(test_hourly['pop_vol'], test_hourly['pred_hourly']) if len(test_hourly) > 48 else np.nan
        mae_h = mean_absolute_error(test_hourly['pop_vol'], test_hourly['pred_hourly']) if len(test_hourly) > 48 else np.nan

        n_c = len(test_cold)
        n_t = len(test_trans)
        n_s = len(test_summer)

        all_metrics.append({
            'line_id': line_id, 'line_name': line_name,
            'month_num': test_month, 'month': test_name,
            'cold_model': models_used['cold'], 'trans_model': models_used['trans'],
            'summer_model': models_used['summer'],
            'n_cold': n_c, 'n_trans': n_t, 'n_summer': n_s,
            'r2_daily': r2_d, 'mae_daily': mae_d, 'mape_daily': mape_d,
            'r2_hourly': r2_h, 'mae_hourly': mae_h,
            'n_test_days': n_c + n_t + n_s, 'n_test_hours': len(test_hourly),
        })

        for _, row in test_hourly.iterrows():
            cd = row['contract_date']
            all_hourly_preds.append({
                'datetime': row['datetime'], 'line_id': line_id, 'line_name': line_name,
                'month_num': test_month, 'month': test_name, 'hour': row['hour'],
                'actual': row['pop_vol'], 'predicted': row['pred_hourly'],
                'temperature': row['temperature'],
                'regime': regime_dates.get(cd, 'cold'),
            })

    # Per-line summary
    lm = [m for m in all_metrics if m['line_id'] == line_id]
    if lm:
        heating = [m for m in lm if m['month_num'] in [1, 2, 3, 10, 11, 12]]
        summer = [m for m in lm if m['month_num'] in SUMMER_MONTHS]
        print(f'\n  {"Мес":<10} {"R2_d":>6} {"MAE_d":>7} {"MAPE":>6} | {"R2_h":>6} | {"C/T/S":>7} {"Models":>25}')
        print(f'  {"-" * 75}')
        for m in sorted(lm, key=lambda x: x['month_num']):
            r2h = f'{m["r2_hourly"]:.3f}' if not np.isnan(m.get('r2_hourly', np.nan)) else ' N/A'
            regime = f'{m["n_cold"]}/{m["n_trans"]}/{m["n_summer"]}'
            mdls = '/'.join(filter(lambda x: x != '-', [m['cold_model'], m['trans_model'], m['summer_model']]))
            print(f'  {m["month"]:<10} {m["r2_daily"]:6.3f} {m["mae_daily"]:7.0f} {m["mape_daily"]:5.1f}% | {r2h:>6} | {regime:>7} {mdls:>25}')
        if heating:
            print(f'  {"-" * 75}')
            print(f'  {"ОТОПИТ.":<10} {np.mean([m["r2_daily"] for m in heating]):6.3f} '
                  f'{np.mean([m["mae_daily"] for m in heating]):7.0f} '
                  f'{np.mean([m["mape_daily"] for m in heating]):5.1f}% | '
                  f'{np.nanmean([m["r2_hourly"] for m in heating]):6.3f}')
        if summer:
            print(f'  {"ЛЕТО":<10} {np.mean([m["r2_daily"] for m in summer]):6.3f} '
                  f'{np.mean([m["mae_daily"] for m in summer]):7.0f} '
                  f'{np.mean([m["mape_daily"] for m in summer]):5.1f}% | '
                  f'{np.nanmean([m["r2_hourly"] for m in summer]):6.3f}')

# ============================================================================
# 6. АГРЕГАЦИЯ ЧАСОВЫХ ПРЕДИКТОВ В СУТКИ
# ============================================================================
print('\n\n[6/7] Агрегация часовых -> суточные...')

preds_df = pd.DataFrame(all_hourly_preds)
preds_df.to_csv(OUTPUT_DIR / 'hourly_predictions.csv', index=False, encoding='utf-8-sig')

preds_df['contract_date'] = preds_df['datetime'].apply(
    lambda x: pd.Timestamp(x.date()) if x.hour >= 7 else pd.Timestamp((x - timedelta(days=1)).date()))

daily_agg = preds_df.groupby(['contract_date', 'line_id', 'line_name', 'month_num', 'month', 'regime']).agg(
    actual_daily=('actual', 'sum'), predicted_daily=('predicted', 'sum'), n_hours=('actual', 'count'),
).reset_index()
daily_agg = daily_agg[daily_agg['n_hours'] >= 20].copy()

# ============================================================================
# 7. СВОДНЫЕ РЕЗУЛЬТАТЫ
# ============================================================================
print('\n' + '=' * 80)
print('СВОДНЫЕ РЕЗУЛЬТАТЫ — ТРЁХРЕЖИМНАЯ МОДЕЛЬ')
print('=' * 80)

metrics_df = pd.DataFrame(all_metrics)

# По месяцам
print('\n--- По месяцам ---')
ms = metrics_df.groupby(['month_num', 'month']).agg(
    r2_daily=('r2_daily', 'mean'), mae_daily=('mae_daily', 'mean'),
    mape_daily=('mape_daily', 'mean'), r2_hourly=('r2_hourly', 'mean'),
    n_cold=('n_cold', 'mean'), n_trans=('n_trans', 'mean'), n_summer=('n_summer', 'mean'),
).reset_index().sort_values('month_num')

print(f'{"Мес":<10} {"R2_d":>6} {"MAE_d":>7} {"MAPE":>6} | {"R2_h":>6} | {"C/T/S":>8}')
print('-' * 52)
for _, r in ms.iterrows():
    r2h = f'{r["r2_hourly"]:.3f}' if not np.isnan(r['r2_hourly']) else ' N/A'
    print(f'{r["month"]:<10} {r["r2_daily"]:6.3f} {r["mae_daily"]:7.0f} {r["mape_daily"]:5.1f}% | {r2h:>6} | '
          f'{r["n_cold"]:2.0f}/{r["n_trans"]:2.0f}/{r["n_summer"]:2.0f}')

hm = metrics_df['month_num'].isin([1, 2, 3, 10, 11, 12])
sm = metrics_df['month_num'].isin(SUMMER_MONTHS)
tm = ~hm & ~sm  # transition months: 4, 5, 9

print('-' * 52)
if hm.any():
    h = metrics_df[hm]
    print(f'{"ОТОПИТ.":<10} {h["r2_daily"].mean():6.3f} {h["mae_daily"].mean():7.0f} '
          f'{h["mape_daily"].mean():5.1f}% | {h["r2_hourly"].mean():6.3f}')
if tm.any():
    t = metrics_df[tm]
    print(f'{"ПЕРЕХОД.":<10} {t["r2_daily"].mean():6.3f} {t["mae_daily"].mean():7.0f} '
          f'{t["mape_daily"].mean():5.1f}% | {t["r2_hourly"].mean():6.3f}')
if sm.any():
    s = metrics_df[sm]
    print(f'{"ЛЕТО":<10} {s["r2_daily"].mean():6.3f} {s["mae_daily"].mean():7.0f} '
          f'{s["mape_daily"].mean():5.1f}% | {s["r2_hourly"].mean():6.3f}')
print(f'{"ОБЩЕЕ":<10} {metrics_df["r2_daily"].mean():6.3f} {metrics_df["mae_daily"].mean():7.0f} '
      f'{metrics_df["mape_daily"].mean():5.1f}% | {metrics_df["r2_hourly"].mean():6.3f}')

# По линиям
for season_name, mask in [('ОТОПИТЕЛЬНЫЙ', hm), ('ЛЕТО', sm)]:
    if not mask.any(): continue
    print(f'\n--- По линиям ({season_name}) ---')
    lh = metrics_df[mask].groupby(['line_id', 'line_name']).agg(
        r2_daily=('r2_daily', 'mean'), mae_daily=('mae_daily', 'mean'),
        mape_daily=('mape_daily', 'mean'), r2_hourly=('r2_hourly', 'mean'),
    ).reset_index().sort_values('r2_daily', ascending=False)
    print(f'{"Линия":<25} {"R2_d":>6} {"MAE_d":>7} {"MAPE":>6} | {"R2_h":>6}')
    print('-' * 55)
    for _, r in lh.iterrows():
        r2h = f'{r["r2_hourly"]:.3f}' if not np.isnan(r['r2_hourly']) else ' N/A'
        print(f'{r["line_name"]:<25} {r["r2_daily"]:6.3f} {r["mae_daily"]:7.0f} {r["mape_daily"]:5.1f}% | {r2h:>6}')
    print('-' * 55)
    print(f'{"СРЕДНЕЕ":<25} {lh["r2_daily"].mean():6.3f} {lh["mae_daily"].mean():7.0f} '
          f'{lh["mape_daily"].mean():5.1f}% | {lh["r2_hourly"].mean():6.3f}')

# R2 агрегированный из часовых
print(f'\n--- R2 суточный (агрегированный из часовых, 7:00-7:00) ---')
agg_metrics = []
for line_id in sorted(daily_agg['line_id'].unique()):
    for test_month in months:
        md = daily_agg[(daily_agg['line_id'] == line_id) & (daily_agg['month_num'] == test_month)]
        if len(md) < 3: continue
        agg_metrics.append({
            'line_id': line_id, 'line_name': lineid_to_grs.get(line_id, str(line_id)),
            'month_num': test_month,
            'r2_agg': r2_score(md['actual_daily'], md['predicted_daily']),
            'mae_agg': mean_absolute_error(md['actual_daily'], md['predicted_daily']),
            'mape_agg': np.mean(np.abs((md['actual_daily'].values - md['predicted_daily'].values) /
                                       np.clip(np.abs(md['actual_daily'].values), 1, None))) * 100,
        })
agg_df = pd.DataFrame(agg_metrics)

for season_name, m_list in [('ОТОПИТ.', [1, 2, 3, 10, 11, 12]), ('ЛЕТО', SUMMER_MONTHS)]:
    amask = agg_df['month_num'].isin(m_list)
    if not amask.any(): continue
    by_line = agg_df[amask].groupby(['line_id', 'line_name']).agg(
        r2_agg=('r2_agg', 'mean'), mae_agg=('mae_agg', 'mean'), mape_agg=('mape_agg', 'mean'),
    ).reset_index().sort_values('r2_agg', ascending=False)
    print(f'\n  {season_name}:')
    print(f'  {"Линия":<25} {"R2_agg":>7} {"MAE":>7} {"MAPE":>6}')
    print(f'  {"-" * 48}')
    for _, r in by_line.iterrows():
        print(f'  {r["line_name"]:<25} {r["r2_agg"]:7.3f} {r["mae_agg"]:7.0f} {r["mape_agg"]:5.1f}%')
    print(f'  {"-" * 48}')
    print(f'  {"СРЕДНЕЕ":<25} {by_line["r2_agg"].mean():7.3f} {by_line["mae_agg"].mean():7.0f} {by_line["mape_agg"].mean():5.1f}%')

# Сравнение
print(f'\n{"=" * 80}')
print('СРАВНЕНИЕ С ПРЕДЫДУЩИМИ ПОДХОДАМИ')
print(f'{"=" * 80}')
print(f'  {"Подход":<30} {"R2_d отоп":>10} {"MAPE отоп":>10} {"R2_d лето":>10} {"MAPE лето":>10}')
print(f'  {"-" * 72}')
print(f'  {"Единая модель":<30} {"0.540":>10} {"9.2%":>10} {"-":>10} {"-":>10}')
print(f'  {"Двухрежимная":<30} {"0.669":>10} {"8.0%":>10} {"-":>10} {"-":>10}')
if hm.any() and sm.any():
    h = metrics_df[hm]; s = metrics_df[sm]
    print(f'  {"Трёхрежимная":<30} {h["r2_daily"].mean():10.3f} {h["mape_daily"].mean():9.1f}% '
          f'{s["r2_daily"].mean():10.3f} {s["mape_daily"].mean():9.1f}%')

# ============================================================================
# ВИЗУАЛИЗАЦИЯ
# ============================================================================
print('\n--- Визуализация ---')

# Heatmaps
for metric, label, center, vmin, vmax, fname in [
    ('r2_daily', 'R2 daily', 0.5, -1, 1, 'heatmap_r2_daily.png'),
    ('r2_hourly', 'R2 hourly', 0.3, -1, 1, 'heatmap_r2_hourly.png'),
    ('mape_daily', 'MAPE daily %', 15, 0, 50, 'heatmap_mape_daily.png'),
]:
    pivot = metrics_df.pivot_table(index='line_name', columns='month', values=metric)
    ordered = [m for m in month_names if m in pivot.columns]
    pivot = pivot[ordered]
    fig, ax = plt.subplots(figsize=(16, 6))
    cmap = 'RdYlGn' if 'r2' in metric else 'RdYlGn_r'
    sns.heatmap(pivot, annot=True, fmt='.2f', cmap=cmap, center=center, vmin=vmin, vmax=vmax,
                ax=ax, cbar_kws={'label': label})
    ax.set_title(f'{label} - Tri-regime (cold + transition + summer)', fontsize=12, fontweight='bold')
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / fname, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'[OK] {fname}')

# Summer detail: actual vs predicted daily
if sm.any():
    summer_agg = daily_agg[daily_agg['month_num'].isin(SUMMER_MONTHS)]
    unique_lines = sorted(summer_agg['line_id'].unique())
    n = len(unique_lines)
    fig, axes = plt.subplots(2, (n + 1) // 2, figsize=(5 * ((n + 1) // 2), 10))
    axes = axes.flatten()
    for i, lid in enumerate(unique_lines):
        ax = axes[i]
        sub = summer_agg[summer_agg['line_id'] == lid].sort_values('contract_date')
        ax.plot(sub['contract_date'], sub['actual_daily'], 'b-o', markersize=3, label='Факт')
        ax.plot(sub['contract_date'], sub['predicted_daily'], 'r--s', markersize=3, label='Прогноз')
        r2 = r2_score(sub['actual_daily'], sub['predicted_daily']) if len(sub) > 3 else np.nan
        mae = mean_absolute_error(sub['actual_daily'], sub['predicted_daily']) if len(sub) > 0 else 0
        ax.set_title(f'{lineid_to_grs.get(lid, str(lid))}\nR2={r2:.3f}, MAE={mae:.0f}', fontsize=10)
        ax.legend(fontsize=7)
        ax.grid(True, alpha=0.3)
        ax.tick_params(axis='x', rotation=45, labelsize=7)
    for j in range(n, len(axes)):
        axes[j].set_visible(False)
    fig.suptitle('Лето (июнь-август): факт vs прогноз (суточный)', fontsize=13, fontweight='bold')
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'summer_actual_vs_pred.png', dpi=150, bbox_inches='tight')
    plt.close()
    print('[OK] summer_actual_vs_pred.png')

# Hourly profiles
heating_preds = preds_df[preds_df['month_num'].isin([1, 2, 3, 10, 11, 12])]
if not heating_preds.empty:
    unique_lines = sorted(heating_preds['line_id'].unique())
    n = len(unique_lines)
    fig, axes = plt.subplots(2, (n + 1) // 2, figsize=(5 * ((n + 1) // 2), 10))
    axes = axes.flatten()
    for i, lid in enumerate(unique_lines):
        ax = axes[i]
        sub = heating_preds[heating_preds['line_id'] == lid]
        ax.plot(sub.groupby('hour')['actual'].mean(), 'b-o', markersize=4, label='Факт')
        ax.plot(sub.groupby('hour')['predicted'].mean(), 'r--s', markersize=4, label='Прогноз')
        r2 = r2_score(sub['actual'], sub['predicted'])
        ax.set_title(f'{lineid_to_grs.get(lid, str(lid))}\nR2_h={r2:.3f}', fontsize=10)
        ax.legend(fontsize=8); ax.grid(True, alpha=0.3); ax.set_xticks(range(0, 24, 3))
    for j in range(n, len(axes)): axes[j].set_visible(False)
    fig.suptitle('Часовой профиль: факт vs прогноз (отопительный сезон)', fontsize=13, fontweight='bold')
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / 'hourly_profiles_heating.png', dpi=150, bbox_inches='tight')
    plt.close()
    print('[OK] hourly_profiles_heating.png')

# Excel
with pd.ExcelWriter(OUTPUT_DIR / 'tri_regime_results.xlsx', engine='openpyxl') as writer:
    metrics_df.to_excel(writer, sheet_name='metrics', index=False)
    ms.to_excel(writer, sheet_name='by_month', index=False)
    agg_df.to_excel(writer, sheet_name='agg_daily', index=False)
print('[OK] tri_regime_results.xlsx')

print(f'\n{"=" * 80}')
print('ГОТОВО!')
print(f'{"=" * 80}')
print(f'Результаты: {OUTPUT_DIR}')
