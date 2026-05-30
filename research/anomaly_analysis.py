"""
Комплексный анализ аномалий в данных газораспределительных станций
================================================================

Категории аномалий:
1. Температура — физические границы, скорость изменения, сезонный Z-score, кросс-линейная
2. Давление — физические границы, скорость изменения, сезонный профиль
3. Зависание датчиков — run-length encoding для T, P, V
4. Кросс-линейное сравнение — нормализованный расход, суточный профиль, ранговая стабильность
5. Сдвиг базового потребления — CUSUM, скользящее сравнение, антикорреляция vol-temp
6. Качество данных — пропуски, отрицательный объём, плотность, нули вне лета

Использование:
    python backend/anomaly_analysis.py
    python backend/anomaly_analysis.py --start 2025-01-01 --end 2025-12-31
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import argparse
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from sqlalchemy import create_engine, text
from scipy import stats
import json
import warnings
from pathlib import Path
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')
plt.rcParams['font.family'] = 'DejaVu Sans'
plt.rcParams['axes.unicode_minus'] = False

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = SCRIPT_DIR / 'data'
OUTPUT_DIR = DATA_DIR / 'anomaly_output'
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

ANALYSIS_START = '2025-01-01'
ANALYSIS_END = '2025-12-31'
TARGET_LINE_IDS = [6, 11, 16, 17, 18, 19, 20, 21, 1002, 1003, 1004]  # 1001 (Запорожсталь) исключена — только промышленность
EXCLUDE_PRESSURE_LINES = [16, 1003]
SUMMER_MONTHS = [6, 7, 8]

# Household meters: max_dp <= this threshold → exclude from analysis
HOUSEHOLD_MAX_DP_THRESHOLD = 16.0

# Reference line for ambient temperature (used in WEAK_TEMP_CORRELATION)
AMBIENT_TEMP_LINE_ID = 19

# Adaptive pressure thresholds by pressure level
# DB units: кг/см² (1 кг/см² ≈ 0.0981 МПа)
# Low:    0.001–0.005 МПа ≈ 0.01–0.05 кг/см²
# Medium: 0.005–0.3 МПа   ≈ 0.05–3.06 кг/см²
# High:   0.3–5.0 МПа     ≈ 3.06–51.0 кг/см²
# (max_median, min_abs_rate_per_h, seasonal_z_threshold, bound_low, bound_high)
PRESSURE_REGIMES = [
    (0.05,  0.01, 5.0, 0.001, 0.1),    # Low: median < 0.05 кг/см²
    (3.06,  0.10, 4.0, 0.01,  5.0),    # Medium: 0.05–3.06 кг/см²
    (999,   0.50, 3.0, 0.1,  51.0),    # High: > 3.06 кг/см²
]

# Volume-aware thresholds for cross-line and baseline shift
CROSS_LINE_MIN_DAILY_VOL = 100.0    # m³ — below this, don't flag norm_ratio
BASELINE_SHIFT_MIN_ABS_DIFF = 50.0  # m³/day — minimum absolute diff for baseline shift

# Stuck volume: don't flag if hourly volume below this (small flows = stable heating)
STUCK_VOLUME_MIN_VALUE = 5.0        # m³/h — below this, stable flow is normal

# Severity base scores
SEVERITY_SCORES = {'CRITICAL': 90, 'HIGH': 70, 'MEDIUM': 40, 'LOW': 15}

# Референсный период: сколько дней контекста загружать перед start_date
REFERENCE_PERIOD_RULES = [
    # (max_analysis_days, reference_days)
    (7, 60),
    (30, 90),
]
# > 30 дней анализа — reference не нужен

# Минимальное количество дней АНАЛИЗА для работы детектора
DETECTOR_MIN_ANALYSIS_DAYS = {
    'CROSS_LINE_PROFILE_CORR': 7,       # недельная корреляция профиля
    'CROSS_LINE_RANK_INSTABILITY': 28,   # 4 недели рангов
}


def compute_reference_period(start_date, end_date):
    """
    Вычисляет референсный период и список активных/неактивных детекторов.

    Returns:
        reference_start: дата начала загрузки данных (включая reference)
        reference_days: количество дней reference (0 если не нужен)
        active_detectors: set детекторов, которые могут работать
        inactive_detectors: dict {detector: reason} для тех, что не могут
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    analysis_days = (end - start).days + 1

    # Определяем reference period
    reference_days = 0
    for max_days, ref_days in REFERENCE_PERIOD_RULES:
        if analysis_days <= max_days:
            reference_days = ref_days
            break

    if reference_days > 0:
        reference_start = (start - timedelta(days=reference_days)).strftime('%Y-%m-%d')
    else:
        reference_start = None

    # Определяем активные/неактивные детекторы
    all_detectors = {
        'TEMP_OUT_OF_BOUNDS', 'TEMP_RAPID_CHANGE', 'TEMP_SEASONAL_ZSCORE',
        'PRESSURE_OUT_OF_BOUNDS', 'PRESSURE_RAPID_CHANGE', 'PRESSURE_SEASONAL_DEVIATION',
        'STUCK_TEMPERATURE', 'STUCK_PRESSURE', 'STUCK_VOLUME',
        'DATA_GAP', 'NEGATIVE_VOLUME', 'DENSITY_ANOMALY', 'ZERO_NON_SUMMER',
        'CUSUM_SHIFT', 'BASELINE_SHIFT', 'WEAK_TEMP_CORRELATION',
        'CROSS_LINE_NORM_RATIO', 'CROSS_LINE_PROFILE_CORR', 'CROSS_LINE_RANK_INSTABILITY',
    }
    inactive_detectors = {}
    for detector, min_days in DETECTOR_MIN_ANALYSIS_DAYS.items():
        if analysis_days < min_days:
            inactive_detectors[detector] = f'нужно {min_days}+ дней анализа (есть {analysis_days})'

    active_detectors = all_detectors - set(inactive_detectors.keys())

    return reference_start, reference_days, active_detectors, inactive_detectors


# ============================================================================
# БЛОК 1: ЗАГРУЗКА ДАННЫХ
# ============================================================================

def get_household_lines(engine, line_ids):
    """Определяет бытовые счётчики по max_dp из таблицы params.

    Returns:
        set of line_ids where max_dp <= HOUSEHOLD_MAX_DP_THRESHOLD
    """
    physical_ids = [lid for lid in line_ids if lid < 1000]
    if not physical_ids:
        return set()
    ids_str = ','.join(map(str, physical_ids))
    with engine.connect() as conn:
        result = conn.execute(text(f"""
            SELECT DISTINCT p.line_id, p.max_dp
            FROM params p
            WHERE p.line_id IN ({ids_str})
              AND p.max_dp IS NOT NULL
            ORDER BY p.line_id
        """))
        rows = result.fetchall()

    # Take latest max_dp per line (params may have multiple periods)
    line_max_dp = {}
    for line_id, max_dp in rows:
        line_max_dp[line_id] = max_dp

    household = {lid for lid, dp in line_max_dp.items() if dp <= HOUSEHOLD_MAX_DP_THRESHOLD}
    return household


def load_data(engine, start_date, end_date, line_ids, reference_start=None):
    """Загрузка данных из БД и подготовка виртуальных линий.

    Args:
        reference_start: если задан, загружает данные от этой даты (для reference period)
    """
    load_start = reference_start or start_date
    print(f'\n[*] Загрузка данных из БД (от {load_start} до {end_date})...')

    # Virtual lines
    virtual_lines = {}
    if VIRTUAL_LINES_FILE.exists():
        with open(VIRTUAL_LINES_FILE, 'r', encoding='utf-8') as f:
            vl_data = json.load(f)
        for vid_str, info in vl_data.get('virtual_lines', {}).items():
            virtual_lines[int(vid_str)] = info['physical_line_ids']

    expanded_ids = set(line_ids)
    for vid in line_ids:
        if vid >= 1000 and vid in virtual_lines:
            expanded_ids.update(virtual_lines[vid])

    # GRS mapping
    grs_map_df = pd.read_excel(LINE_ID_FILE, sheet_name='line_id', header=None,
                                names=['grs_name', 'line_id'])
    grs_map_df = grs_map_df[grs_map_df['line_id'].isin(expanded_ids)].copy()
    grs_to_lineid = dict(zip(grs_map_df['grs_name'], grs_map_df['line_id']))
    lineid_to_grs = dict(zip(grs_map_df['line_id'], grs_map_df['grs_name']))

    # Add virtual line names
    with open(VIRTUAL_LINES_FILE, 'r', encoding='utf-8') as f:
        vl_full = json.load(f)
    for vid in line_ids:
        if vid >= 1000 and vid not in lineid_to_grs:
            lineid_to_grs[vid] = vl_full['virtual_lines'].get(str(vid), {}).get('name', f'V_{vid}')

    # Physical lines
    physical_ids = [lid for lid in line_ids if lid < 1000]
    virtual_ids = [lid for lid in line_ids if lid >= 1000]

    physical_ids_str = ','.join(map(str, physical_ids))
    with engine.connect() as conn:
        df_physical = pd.read_sql(text(f"""
        SELECT gvl.id as line_id, ha.period as datetime,
               ha.volume, ha.temperature, ha.pressure, ha.density
        FROM hourly_archive ha INNER JOIN gas_volume_line gvl ON ha.line_id = gvl.id
        WHERE gvl.id IN ({physical_ids_str})
          AND ha.period >= '{load_start}' AND ha.period <= '{end_date}'
        ORDER BY gvl.id, ha.period"""), conn)
    print(f'  Физические линии: {len(df_physical):,} записей')

    # Virtual lines
    df_virtual_list = []
    for vid in virtual_ids:
        if vid not in virtual_lines:
            continue
        phys_ids_str = ','.join(map(str, virtual_lines[vid]))
        with engine.connect() as conn:
            df_virt = pd.read_sql(text(f"""
            SELECT ha.period as datetime, SUM(ha.volume) as volume,
                   AVG(ha.temperature) as temperature, AVG(ha.pressure) as pressure,
                   AVG(ha.density) as density
            FROM hourly_archive ha INNER JOIN gas_volume_line gvl ON ha.line_id = gvl.id
            WHERE gvl.id IN ({phys_ids_str})
              AND ha.period >= '{load_start}' AND ha.period <= '{end_date}'
            GROUP BY ha.period ORDER BY ha.period"""), conn)
        df_virt['line_id'] = vid
        df_virtual_list.append(df_virt)
        print(f'  Виртуальная линия {vid} ({lineid_to_grs.get(vid, "")}): {len(df_virt):,}')

    if df_virtual_list:
        hourly = pd.concat([df_physical] + df_virtual_list, ignore_index=True)
    else:
        hourly = df_physical.copy()

    hourly['datetime'] = pd.to_datetime(hourly['datetime'])
    hourly = hourly.sort_values(['line_id', 'datetime']).reset_index(drop=True)

    # Маркируем строки, попадающие в период анализа (vs reference)
    analysis_start_ts = pd.Timestamp(start_date)
    analysis_end_ts = pd.Timestamp(end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    hourly['is_analysis_period'] = (
        (hourly['datetime'] >= analysis_start_ts) & (hourly['datetime'] <= analysis_end_ts)
    )

    n_ref = (~hourly['is_analysis_period']).sum()
    n_analysis = hourly['is_analysis_period'].sum()
    print(f'  Всего: {len(hourly):,} записей (анализ: {n_analysis:,}, reference: {n_ref:,})')
    print('[OK] Данные загружены')
    return hourly, lineid_to_grs, grs_to_lineid, virtual_lines


# ============================================================================
# БЛОК 2: ПОДГОТОВКА ДАННЫХ
# ============================================================================

def prepare_data(hourly, grs_to_lineid, virtual_lines):
    """Добавление временных признаков, contractual day, вычитание предприятий."""
    print('\n[*] Подготовка данных...')

    hourly['date'] = pd.to_datetime(hourly['datetime'].dt.date)
    hourly['month'] = hourly['datetime'].dt.month
    hourly['hour'] = hourly['datetime'].dt.hour
    hourly['day_of_week'] = hourly['datetime'].dt.dayofweek
    hourly['contract_date'] = hourly['date'].copy()
    hourly.loc[hourly['hour'] < 7, 'contract_date'] = (
        hourly.loc[hourly['hour'] < 7, 'date'] - pd.Timedelta(days=1))

    # Enterprise subtraction
    enterprise_volumes = pd.DataFrame()
    if VOLUME_FILE.exists():
        try:
            vol_raw = pd.read_excel(VOLUME_FILE, sheet_name='TDSheet')
            cols = list(vol_raw.columns)
            cols[0] = 'enterprise_name'
            cols[1] = 'enterprise_line'
            vol_raw.columns = cols
            vol_raw = vol_raw[vol_raw['enterprise_name'].notna()].copy()
            vol_raw = vol_raw[vol_raw['enterprise_name'] != 'Підприємство'].copy()
            for col in vol_raw.columns:
                if str(col).lower() in ('грс', 'grs'):
                    vol_raw = vol_raw.rename(columns={col: 'grs_col'})
                    break
            if 'grs_col' not in vol_raw.columns:
                vol_raw['grs_col'] = vol_raw['enterprise_name']
            vol_raw['line_id'] = vol_raw['grs_col'].map(grs_to_lineid)

            date_cols = []
            for col in vol_raw.columns:
                if isinstance(col, datetime):
                    date_cols.append(col)
                elif isinstance(col, str):
                    cc = col.replace(' р.', '').strip()
                    if cc.count('.') == 2 or cc.count('/') == 2 or cc.count('-') == 2:
                        try:
                            pd.to_datetime(cc, dayfirst=True)
                            date_cols.append(col)
                        except Exception:
                            pass

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

                # Remap physical to virtual
                phys_to_virt = {}
                for vid, pids in virtual_lines.items():
                    for pid in pids:
                        phys_to_virt[pid] = vid
                if phys_to_virt:
                    remapped = enterprise_volumes['line_id'].map(phys_to_virt)
                    has = remapped.notna()
                    if has.any():
                        enterprise_volumes.loc[has, 'line_id'] = remapped[has].astype(int)
                        enterprise_volumes = enterprise_volumes.groupby(
                            ['line_id', 'date'], as_index=False)['daily_ent'].sum()
                print(f'  Предприятия: {len(enterprise_volumes):,} записей')
        except Exception as e:
            print(f'  [!] Ошибка загрузки предприятий: {e}')

    if not enterprise_volumes.empty:
        hourly = hourly.merge(
            enterprise_volumes.rename(columns={'date': 'contract_date'}),
            on=['line_id', 'contract_date'], how='left')
        hourly['daily_ent'] = hourly['daily_ent'].fillna(0)
        hourly['hourly_ent'] = hourly['daily_ent'] / 24
    else:
        hourly['hourly_ent'] = 0

    hourly['pop_vol'] = (hourly['volume'] - hourly['hourly_ent']).clip(lower=0)

    print('[OK] Данные подготовлены')
    return hourly


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def make_anomaly(line_id, grs_name, category, anomaly_type, start_dt, end_dt,
                 affected_field, detected_value, expected_value, severity_label,
                 description, recommended_action):
    """Создание записи аномалии в стандартном формате."""
    if isinstance(start_dt, pd.Timestamp):
        start_dt = start_dt.to_pydatetime()
    if isinstance(end_dt, pd.Timestamp):
        end_dt = end_dt.to_pydatetime()

    duration = max((end_dt - start_dt).total_seconds() / 3600, 1.0)
    deviation_pct = 0.0
    if expected_value and expected_value != 0:
        deviation_pct = abs(detected_value - expected_value) / abs(expected_value) * 100

    return {
        'line_id': line_id,
        'grs_name': grs_name,
        'category': category,
        'anomaly_type': anomaly_type,
        'start_datetime': start_dt,
        'end_datetime': end_dt,
        'duration_hours': round(duration, 1),
        'affected_field': affected_field,
        'detected_value': round(detected_value, 2) if detected_value is not None else None,
        'expected_value': round(expected_value, 2) if expected_value is not None else None,
        'deviation_pct': round(deviation_pct, 1),
        'severity_label': severity_label,
        'severity_score': 0,  # will be filled by score_anomalies
        'description': description,
        'recommended_action': recommended_action,
    }


def run_length_encode(series):
    """Run-length encoding: returns groups with start/end indices and lengths."""
    if len(series) == 0:
        return pd.DataFrame(columns=['group', 'value', 'start', 'end', 'length'])
    groups = (series != series.shift()).cumsum()
    result = []
    for g, grp in series.groupby(groups):
        result.append({
            'group': g,
            'value': grp.iloc[0],
            'start': grp.index[0],
            'end': grp.index[-1],
            'length': len(grp),
        })
    return pd.DataFrame(result)


def group_flagged_rows(flagged_df, datetime_col='datetime', gap_hours=2):
    """Group consecutive flagged rows into events (within gap_hours of each other).
    Returns list of (start_dt, end_dt, sub_df) tuples."""
    if len(flagged_df) == 0:
        return []
    flagged_df = flagged_df.sort_values(datetime_col).copy()
    groups = (flagged_df[datetime_col].diff() > pd.Timedelta(hours=gap_hours)).cumsum()
    result = []
    for _, grp in flagged_df.groupby(groups):
        result.append((grp[datetime_col].iloc[0], grp[datetime_col].iloc[-1], grp))
    return result


# ============================================================================
# ДЕТЕКТОРЫ АНОМАЛИЙ
# ============================================================================

def detect_temperature_anomalies(line_df, line_id, grs_name, analysis_mask=None):
    """
    Аномалии температуры (только по отдельной линии, без кросс-валидации):
    - Физические границы: T < -40 или T > 50
    - Скорость изменения: |dT/1h| > 15
    - Сезонный Z-score: Z > 4.0 (по месяцу, per line)

    Baselines строятся на всех данных (reference + analysis).
    Аномалии фильтруются по analysis_mask (если задан).
    """
    anomalies = []
    df = line_df.sort_values('datetime').copy()
    if analysis_mask is not None:
        mask = analysis_mask.reindex(df.index, fill_value=True)
    else:
        mask = pd.Series(True, index=df.index)

    # 1. Physical bounds — group consecutive
    out_of_bounds = df[(df['temperature'] < -40) | (df['temperature'] > 50)]
    out_of_bounds = out_of_bounds[mask.reindex(out_of_bounds.index, fill_value=True)]
    for start_dt, end_dt, grp in group_flagged_rows(out_of_bounds):
        worst = grp.loc[grp['temperature'].abs().idxmax(), 'temperature']
        anomalies.append(make_anomaly(
            line_id, grs_name, 'Temperature', 'TEMP_OUT_OF_BOUNDS',
            start_dt, end_dt,
            'temperature', worst, 0.0, 'CRITICAL',
            f'Температура вне границ [-40, 50]: пик {worst:.1f}C, {len(grp)}ч',
            'Проверить датчик температуры, возможна неисправность'))

    # 2. Rate of change — group consecutive
    df['temp_diff'] = df['temperature'].diff().abs()
    rapid_change = df[df['temp_diff'] > 15]
    rapid_change = rapid_change[mask.reindex(rapid_change.index, fill_value=True)]
    for start_dt, end_dt, grp in group_flagged_rows(rapid_change):
        max_diff = grp['temp_diff'].max()
        anomalies.append(make_anomaly(
            line_id, grs_name, 'Temperature', 'TEMP_RAPID_CHANGE',
            start_dt, end_dt,
            'temperature', max_diff, 15.0, 'HIGH',
            f'Резкое изменение температуры: макс {max_diff:.1f}C/ч, {len(grp)} событий',
            'Проверить датчик температуры'))

    # 3. Seasonal Z-score — baselines on ALL data, outliers filtered to analysis period
    for month in df['month'].unique():
        month_data = df[df['month'] == month]
        if len(month_data) < 10:
            continue
        mean_t = month_data['temperature'].mean()
        std_t = month_data['temperature'].std()
        if std_t < 0.1:
            continue
        z_scores = ((month_data['temperature'] - mean_t) / std_t).abs()
        outliers = month_data[z_scores > 4.0]
        # Filter to analysis period
        outliers = outliers[mask.reindex(outliers.index, fill_value=True)]
        for start_dt, end_dt, grp in group_flagged_rows(outliers):
            z_vals = ((grp['temperature'] - mean_t) / std_t).abs()
            max_z = z_vals.max()
            sev = 'HIGH' if max_z > 5.0 else 'MEDIUM'
            anomalies.append(make_anomaly(
                line_id, grs_name, 'Temperature', 'TEMP_SEASONAL_ZSCORE',
                start_dt, end_dt,
                'temperature', grp['temperature'].iloc[z_vals.argmax()], mean_t, sev,
                f'Z-score до {max_z:.1f} для мес. {month} ({len(grp)}ч, среднее {mean_t:.1f}C)',
                'Проверить показания датчика'))

    return anomalies


def detect_pressure_anomalies(line_df, line_id, grs_name, analysis_mask=None):
    """
    Аномалии давления (не для EXCLUDE_PRESSURE_LINES):
    - Физические границы: P < 0.1 или P > 15
    - Скорость изменения: |dP/1h| > 3 * rolling_72h_std AND > min_abs_rate (by regime)
    - Сезонный профиль: > Z_threshold sigma от месячного среднего (by regime)

    Pressure regime is auto-detected from median pressure of the line.
    Baselines строятся на всех данных (reference + analysis).
    Аномалии фильтруются по analysis_mask (если задан).
    """
    if line_id in EXCLUDE_PRESSURE_LINES:
        return []

    anomalies = []
    df = line_df.sort_values('datetime').copy()

    if 'pressure' not in df.columns or df['pressure'].isna().all():
        return []

    if analysis_mask is not None:
        mask = analysis_mask.reindex(df.index, fill_value=True)
    else:
        mask = pd.Series(True, index=df.index)

    # Auto-detect pressure regime from median
    median_pressure = df['pressure'].median()
    min_abs_rate = 0.0
    seasonal_z_threshold = 3.0
    bound_low = 0.1
    bound_high = 51.0
    for max_median, mar, szt, bl, bh in PRESSURE_REGIMES:
        if median_pressure < max_median:
            min_abs_rate = mar
            seasonal_z_threshold = szt
            bound_low = bl
            bound_high = bh
            break

    # 1. Physical bounds per regime — group consecutive
    out_of_bounds = df[(df['pressure'] < bound_low) | (df['pressure'] > bound_high)]
    out_of_bounds = out_of_bounds[mask.reindex(out_of_bounds.index, fill_value=True)]
    for start_dt, end_dt, grp in group_flagged_rows(out_of_bounds):
        worst_idx = grp['pressure'].apply(lambda p: max(bound_low - p, p - bound_high, 0)).idxmax()
        worst = grp.loc[worst_idx, 'pressure']
        anomalies.append(make_anomaly(
            line_id, grs_name, 'Pressure', 'PRESSURE_OUT_OF_BOUNDS',
            start_dt, end_dt,
            'pressure', worst, 0.0, 'CRITICAL',
            f'Давление вне границ [{bound_low}, {bound_high}]: пик {worst:.3f} кг/см², {len(grp)}ч',
            'Немедленно проверить регулятор давления и датчик'))

    # 2. Rate of change vs rolling std + absolute minimum threshold
    df['p_diff'] = df['pressure'].diff().abs()
    df['p_rolling_std'] = df['pressure'].rolling(72, min_periods=24).std()
    df['p_threshold'] = df['p_rolling_std'] * 3
    rapid = df[
        (df['p_diff'] > df['p_threshold']) &
        (df['p_threshold'] > 0) &
        (df['p_diff'] > min_abs_rate)
    ]
    rapid = rapid[mask.reindex(rapid.index, fill_value=True)]
    for start_dt, end_dt, grp in group_flagged_rows(rapid):
        max_diff = grp['p_diff'].max()
        anomalies.append(make_anomaly(
            line_id, grs_name, 'Pressure', 'PRESSURE_RAPID_CHANGE',
            start_dt, end_dt,
            'pressure', max_diff, grp['p_threshold'].mean(), 'HIGH',
            f'Резкое изменение давления: макс {max_diff:.3f} кг/см²/ч, {len(grp)} событий',
            'Проверить регулятор давления, возможен сбой'))

    # 3. Seasonal profile deviation — adaptive Z-threshold by pressure regime
    for month in df['month'].unique():
        month_data = df[df['month'] == month]
        if len(month_data) < 10:
            continue
        mean_p = month_data['pressure'].mean()
        std_p = month_data['pressure'].std()
        if std_p < 0.001:
            continue
        deviations = ((month_data['pressure'] - mean_p) / std_p).abs()
        outliers = month_data[deviations > seasonal_z_threshold]
        outliers = outliers[mask.reindex(outliers.index, fill_value=True)]
        for start_dt, end_dt, grp in group_flagged_rows(outliers):
            z_vals = ((grp['pressure'] - mean_p) / std_p).abs()
            max_z = z_vals.max()
            anomalies.append(make_anomaly(
                line_id, grs_name, 'Pressure', 'PRESSURE_SEASONAL_DEVIATION',
                start_dt, end_dt,
                'pressure', grp['pressure'].iloc[z_vals.argmax()], mean_p, 'MEDIUM',
                f'Давление Z до {max_z:.1f} в мес. {month} ({len(grp)}ч, среднее {mean_p:.2f})',
                'Мониторить, возможны сезонные колебания нагрузки'))

    return anomalies


def detect_stuck_sensors(line_df, line_id, grs_name, analysis_mask=None):
    """
    Зависание датчиков — одинаковые ненулевые значения подряд:
    - Температура: >= 8h (MEDIUM 8-24h, HIGH >24h)
    - Давление low (<0.05 кг/см²): >= 12h (MEDIUM 12-48h, HIGH >48h)
    - Давление medium/high: >= 6h (MEDIUM 6-12h, HIGH >12h)  [skip EXCLUDE_PRESSURE_LINES]
    - Объём: >= 6h ненулевые, не лето (MEDIUM 6-12h, HIGH >12h)

    RLE считается на всех данных (reference + analysis).
    Репортятся только зависания, пересекающиеся с analysis period.
    """
    anomalies = []
    df = line_df.sort_values('datetime').reset_index(drop=True)

    # Для фильтрации stuck-периодов, пересекающихся с analysis
    if analysis_mask is not None:
        # Получаем временные границы analysis period из маски
        analysis_indices = df.index[analysis_mask.reindex(df.index, fill_value=True)]
        if len(analysis_indices) > 0:
            analysis_start_dt = df.loc[analysis_indices[0], 'datetime']
            analysis_end_dt = df.loc[analysis_indices[-1], 'datetime']
        else:
            return anomalies
    else:
        analysis_start_dt = None
        analysis_end_dt = None

    def overlaps_analysis(start_dt, end_dt):
        """Check if a stuck period overlaps with analysis period."""
        if analysis_start_dt is None:
            return True
        return start_dt <= analysis_end_dt and end_dt >= analysis_start_dt

    # Temperature stuck (MEDIUM >= 8h, HIGH > 24h)
    if df['temperature'].notna().sum() > 10:
        temp_nonzero = df[df['temperature'] != 0].copy()
        if len(temp_nonzero) > 10:
            rle = run_length_encode(temp_nonzero['temperature'])
            stuck = rle[rle['length'] >= 8]
            for _, run in stuck.iterrows():
                start_idx = run['start']
                end_idx = run['end']
                start_dt = temp_nonzero.loc[start_idx, 'datetime']
                end_dt = temp_nonzero.loc[end_idx, 'datetime']
                if not overlaps_analysis(start_dt, end_dt):
                    continue
                length = run['length']
                sev = 'HIGH' if length > 24 else 'MEDIUM'
                anomalies.append(make_anomaly(
                    line_id, grs_name, 'Sensor_Issues', 'STUCK_TEMPERATURE',
                    start_dt, end_dt,
                    'temperature', temp_nonzero.loc[start_idx, 'temperature'], 0.0, sev,
                    f'Температура = {temp_nonzero.loc[start_idx, "temperature"]:.1f}C не менялась {length}ч',
                    'Проверить датчик температуры на зависание'))

    # Pressure stuck (skip excluded lines, adaptive thresholds by pressure level)
    if line_id not in EXCLUDE_PRESSURE_LINES and df['pressure'].notna().sum() > 10:
        p_nonzero = df[df['pressure'] > 0].copy()
        if len(p_nonzero) > 10:
            # Adaptive thresholds: low pressure lines need longer runs
            median_p = p_nonzero['pressure'].median()
            if median_p < 0.05:
                stuck_min, stuck_high = 12, 48
            else:
                stuck_min, stuck_high = 6, 12
            # Round to 3 decimals to handle floating point noise
            p_rounded = p_nonzero['pressure'].round(3)
            rle = run_length_encode(p_rounded)
            stuck = rle[rle['length'] >= stuck_min]
            for _, run in stuck.iterrows():
                start_idx = run['start']
                end_idx = run['end']
                start_dt = p_nonzero.loc[start_idx, 'datetime']
                end_dt = p_nonzero.loc[end_idx, 'datetime']
                if not overlaps_analysis(start_dt, end_dt):
                    continue
                length = run['length']
                sev = 'HIGH' if length > stuck_high else 'MEDIUM'
                anomalies.append(make_anomaly(
                    line_id, grs_name, 'Sensor_Issues', 'STUCK_PRESSURE',
                    start_dt, end_dt,
                    'pressure', p_nonzero.loc[start_idx, 'pressure'], 0.0, sev,
                    f'Давление = {p_nonzero.loc[start_idx, "pressure"]:.3f} кг/см² не менялось {length}ч',
                    'Проверить датчик давления на зависание'))

    # Volume stuck (non-zero, non-summer) — MEDIUM >= 6h, HIGH > 12h
    # Skip small volumes (< STUCK_VOLUME_MIN_VALUE) — stable flow is normal for heating
    non_summer = df[~df['month'].isin(SUMMER_MONTHS)].copy()
    vol_nonzero = non_summer[non_summer['volume'] >= STUCK_VOLUME_MIN_VALUE].copy()
    if len(vol_nonzero) > 10:
        vol_rounded = vol_nonzero['volume'].round(1)
        rle = run_length_encode(vol_rounded)
        stuck = rle[rle['length'] >= 6]
        for _, run in stuck.iterrows():
            start_idx = run['start']
            end_idx = run['end']
            start_dt = vol_nonzero.loc[start_idx, 'datetime']
            end_dt = vol_nonzero.loc[end_idx, 'datetime']
            if not overlaps_analysis(start_dt, end_dt):
                continue
            length = run['length']
            sev = 'HIGH' if length > 12 else 'MEDIUM'
            anomalies.append(make_anomaly(
                line_id, grs_name, 'Sensor_Issues', 'STUCK_VOLUME',
                start_dt, end_dt,
                'volume', vol_nonzero.loc[start_idx, 'volume'], 0.0, sev,
                f'Объём = {vol_nonzero.loc[start_idx, "volume"]:.1f} м³ не менялся {length}ч',
                'Проверить расходомер'))

    return anomalies


def detect_data_quality(line_df, line_id, grs_name, analysis_mask=None):
    """
    Качество данных:
    - Пропуски > 2h
    - Отрицательный объём
    - Аномальная плотность [0.55, 0.90]
    - Нули вне лета

    Аномалии фильтруются по analysis_mask (если задан).
    """
    anomalies = []
    df = line_df.sort_values('datetime').copy()
    if analysis_mask is not None:
        mask = analysis_mask.reindex(df.index, fill_value=True)
    else:
        mask = pd.Series(True, index=df.index)

    # 1. Data gaps — only report gaps within analysis period
    dt_diff = df['datetime'].diff()
    gaps = df[dt_diff > pd.Timedelta(hours=2)]
    gaps = gaps[mask.reindex(gaps.index, fill_value=True)]
    for idx, row in gaps.iterrows():
        prev_idx = df.index[df.index.get_loc(idx) - 1]
        prev_dt = df.loc[prev_idx, 'datetime']
        gap_hours = (row['datetime'] - prev_dt).total_seconds() / 3600
        if gap_hours > 24:
            sev = 'CRITICAL'
        elif gap_hours > 6:
            sev = 'HIGH'
        else:
            sev = 'MEDIUM'
        anomalies.append(make_anomaly(
            line_id, grs_name, 'Data_Quality', 'DATA_GAP',
            prev_dt, row['datetime'],
            'period', gap_hours, 1.0, sev,
            f'Пропуск данных {gap_hours:.0f}ч ({prev_dt.strftime("%d.%m %H:%M")} — {row["datetime"].strftime("%d.%m %H:%M")})',
            'Проверить связь с прибором учёта'))

    # 2. Negative volume — group consecutive
    negatives = df[df['volume'] < 0]
    negatives = negatives[mask.reindex(negatives.index, fill_value=True)]
    for start_dt, end_dt, grp in group_flagged_rows(negatives):
        worst = grp['volume'].min()
        anomalies.append(make_anomaly(
            line_id, grs_name, 'Data_Quality', 'NEGATIVE_VOLUME',
            start_dt, end_dt,
            'volume', worst, 0.0, 'CRITICAL',
            f'Отрицательный объём: мин {worst:.1f} м³, {len(grp)}ч',
            'Проверить расходомер, возможен обратный поток или ошибка'))

    # 3. Density anomalies — disabled (data unreliable across lines)

    # 4. Non-summer zeros — group consecutive
    non_summer = df[~df['month'].isin(SUMMER_MONTHS)]
    zeros = non_summer[non_summer['volume'] == 0]
    zeros = zeros[mask.reindex(zeros.index, fill_value=True)]
    for start_dt, end_dt, grp in group_flagged_rows(zeros):
        length = len(grp)
        sev = 'HIGH' if length > 6 else 'MEDIUM'
        anomalies.append(make_anomaly(
            line_id, grs_name, 'Data_Quality', 'ZERO_NON_SUMMER',
            start_dt, end_dt,
            'volume', 0.0, None, sev,
            f'Нулевой расход {length}ч вне лета ({start_dt.strftime("%d.%m")})',
            'Проверить подачу газа и расходомер'))

    return anomalies


def detect_cross_line_deviations(all_df, lineid_to_grs,
                                  analysis_start=None, analysis_end=None,
                                  active_detectors=None):
    """
    Кросс-линейное сравнение:
    - Нормализованный расход: actual / median по temp bucket
    - Суточный профиль: корреляция с медианным профилем
    - Ранговая стабильность: Kendall tau

    Baselines (медианы по temp bucket) на всех данных.
    Outliers фильтруются к analysis period.
    PROFILE_CORR и RANK_INSTABILITY пропускаются если нет в active_detectors.
    """
    anomalies = []
    line_ids = all_df['line_id'].unique()
    if len(line_ids) < 3:
        return anomalies

    print('  Кросс-линейный анализ...')

    # Daily analysis mask
    if analysis_start and analysis_end:
        a_start = pd.Timestamp(analysis_start)
        a_end = pd.Timestamp(analysis_end)
    else:
        a_start = None
        a_end = None

    # 1. Daily weight deviation: compare each line's daily weight to system trend
    # System weight: V_day_all / V_total — shows how consumption distributes over time
    # Line weight:   V_day_line / V_line_total — same for individual line
    # If line follows the system trend, weights are similar. Deviation = anomaly.
    df = all_df.copy()

    # Daily volumes per line — on ALL data for baselines
    daily = df.groupby(['contract_date', 'line_id']).agg(
        daily_vol=('pop_vol', 'sum'),
    ).reset_index()

    # System daily weight (total across all lines)
    system_daily = daily.groupby('contract_date')['daily_vol'].sum().rename('sys_daily')
    sys_total = system_daily.sum()
    system_weight = (system_daily / sys_total).rename('sys_weight')

    # Per-line daily weight
    line_totals = daily.groupby('line_id')['daily_vol'].sum().rename('line_total')
    daily = daily.merge(line_totals, on='line_id')
    daily = daily.merge(system_weight, on='contract_date')
    daily['line_weight'] = daily['daily_vol'] / daily['line_total'].clip(lower=1)

    # Weight deviation: line_weight / sys_weight (1.0 = follows system trend)
    daily['weight_ratio'] = daily['line_weight'] / daily['sys_weight'].clip(lower=1e-9)

    # Daily analysis mask
    if a_start is not None:
        daily_in_analysis = (daily['contract_date'] >= a_start) & (daily['contract_date'] <= a_end)
    else:
        daily_in_analysis = pd.Series(True, index=daily.index)

    # Detect days where line deviates from system trend
    for lid in line_ids:
        line_daily = daily[(daily['line_id'] == lid) & daily_in_analysis]
        grs = lineid_to_grs.get(lid, str(lid))
        # Skip lines with very low total volume
        if line_daily['daily_vol'].sum() < CROSS_LINE_MIN_DAILY_VOL * len(line_daily):
            continue
        outliers = line_daily[
            (line_daily['weight_ratio'] > 2.0) | (line_daily['weight_ratio'] < 0.5)
        ].dropna(subset=['weight_ratio'])
        if len(outliers) > 0:
            outliers_sorted = outliers.sort_values('contract_date')
            groups = (outliers_sorted['contract_date'].diff() > pd.Timedelta(days=2)).cumsum()
            for _, grp in outliers_sorted.groupby(groups):
                start_dt = pd.Timestamp(grp['contract_date'].iloc[0])
                end_dt = pd.Timestamp(grp['contract_date'].iloc[-1]) + pd.Timedelta(hours=23)
                mean_wr = grp['weight_ratio'].mean()
                mean_lw = grp['line_weight'].mean() * 100
                mean_sw = grp['sys_weight'].mean() * 100
                direction = 'выше' if mean_wr > 1 else 'ниже'
                anomalies.append(make_anomaly(
                    lid, grs, 'Cross_Line', 'CROSS_LINE_NORM_RATIO',
                    start_dt, end_dt,
                    'volume', mean_wr, 1.0, 'HIGH',
                    f'Уд. вес линии {direction} тренда: {mean_wr:.2f}x '
                    f'(линия {mean_lw:.2f}% vs система {mean_sw:.2f}%), {len(grp)} дн.',
                    'Проверить аномальное потребление, возможна утечка или ограничение'))

    # 2. Monthly profile correlation — skip if not in active_detectors
    if active_detectors is None or 'CROSS_LINE_PROFILE_CORR' in active_detectors:
        hourly_profile = df.groupby(['contract_date', 'line_id', 'hour']).agg(
            vol=('pop_vol', 'sum')).reset_index()

        # Median profile across all lines per contract_date
        median_profile = hourly_profile.groupby(['contract_date', 'hour'])['vol'].median().rename('median_vol')
        hourly_profile = hourly_profile.merge(median_profile, on=['contract_date', 'hour'], how='left')

        # Monthly correlation per line (was weekly — too noisy)
        df_months = df.copy()
        df_months['year_month'] = df_months['datetime'].dt.to_period('M')

        for lid in line_ids:
            grs = lineid_to_grs.get(lid, str(lid))
            line_hp = hourly_profile[hourly_profile['line_id'] == lid]

            for ym in df_months[df_months['line_id'] == lid]['year_month'].unique():
                month_dates = df_months[
                    (df_months['year_month'] == ym) & (df_months['line_id'] == lid)
                ]['contract_date'].unique()

                # Filter to months overlapping analysis period
                if a_start is not None:
                    month_in_analysis = [d for d in month_dates if a_start <= pd.Timestamp(d) <= a_end]
                    if not month_in_analysis:
                        continue

                month_data = line_hp[line_hp['contract_date'].isin(month_dates)]
                if len(month_data) < 48:  # at least 2 days
                    continue

                # Merge line vol with median vol
                merged = month_data[['hour', 'vol', 'median_vol']].dropna()
                if len(merged) < 20:
                    continue

                corr = merged['vol'].corr(merged['median_vol'])
                if corr < 0.3:
                    month_start = pd.Timestamp(min(month_dates))
                    month_end = pd.Timestamp(max(month_dates)) + pd.Timedelta(hours=23)
                    anomalies.append(make_anomaly(
                        lid, grs, 'Cross_Line', 'CROSS_LINE_PROFILE_CORR',
                        month_start, month_end,
                        'volume', corr, 0.3, 'HIGH',
                        f'Корреляция суточного профиля с медианой = {corr:.2f} ({ym})',
                        'Проверить режим потребления, возможна аномалия нагрузки'))

    # 3. Rank stability (Kendall tau) — skip if not in active_detectors
    if active_detectors is None or 'CROSS_LINE_RANK_INSTABILITY' in active_detectors:
        # Weekly ranks per line
        weekly_vol = daily.groupby(['line_id', pd.Grouper(key='contract_date', freq='W')])['daily_vol'].mean().reset_index()
        weekly_vol.columns = ['line_id', 'week_start', 'daily_vol_avg']
        weeks = sorted(weekly_vol['week_start'].unique())

        if len(weeks) >= 4:
            for lid in line_ids:
                grs = lineid_to_grs.get(lid, str(lid))
                lid_weekly = weekly_vol[weekly_vol['line_id'] == lid].sort_values('week_start')
                if len(lid_weekly) < 4:
                    continue

                # Compare each line's rank sequence to overall median rank
                all_weekly = weekly_vol.pivot(index='week_start', columns='line_id', values='daily_vol_avg')
                ranks = all_weekly.rank(axis=1)
                if lid not in ranks.columns:
                    continue
                lid_ranks = ranks[lid].dropna()
                if len(lid_ranks) < 4:
                    continue

                # Compute Kendall tau of rank vs time
                tau, p_value = stats.kendalltau(range(len(lid_ranks)), lid_ranks)
                if abs(tau) < 0.5 and p_value < 0.05:
                    anomalies.append(make_anomaly(
                        lid, grs, 'Cross_Line', 'CROSS_LINE_RANK_INSTABILITY',
                        pd.Timestamp(weeks[0]), pd.Timestamp(weeks[-1]),
                        'volume', tau, 0.5, 'MEDIUM',
                        f'Нестабильный ранг потребления: tau={tau:.2f} (p={p_value:.3f})',
                        'Мониторить динамику, возможны системные изменения'))

    return anomalies


def detect_consumption_shifts(line_df, line_id, grs_name, analysis_mask=None,
                              system_ma_ratio=None, ambient_temp=None):
    """
    Сдвиг базового потребления:
    - CUSUM temperature-detrended residuals
    - Скользящее сравнение 7-дн vs 30-дн ОТНОСИТЕЛЬНО системного тренда
    - Антикорреляция vol-temp в отопительный сезон (using ambient temperature)

    Args:
        system_ma_ratio: Series indexed by contract_date with system-wide 7d/30d ratio.
            If provided, BASELINE_SHIFT uses line_ratio / system_ratio instead of raw ratio.
        ambient_temp: Series indexed by datetime with ambient temperature (from reference line).
            If provided, used instead of line's own temperature for WEAK_TEMP_CORRELATION.

    Baselines (регрессия, rolling) строятся на всех данных (reference + analysis).
    Аномалии фильтруются по analysis period.
    """
    anomalies = []
    df = line_df.sort_values('datetime').copy()

    if analysis_mask is not None:
        hourly_mask = analysis_mask.reindex(df.index, fill_value=True)
    else:
        hourly_mask = pd.Series(True, index=df.index)

    # Daily aggregation for consumption shift analysis
    agg_dict = {
        'daily_vol': ('pop_vol', 'sum'),
        'mean_temp': ('temperature', 'mean'),
        'month': ('month', 'first'),
    }
    if 'is_analysis_period' in df.columns:
        agg_dict['is_analysis'] = ('is_analysis_period', 'any')
    daily = df.groupby('contract_date').agg(**agg_dict).reset_index()
    daily = daily.sort_values('contract_date').reset_index(drop=True)

    # Создаём daily analysis mask
    if 'is_analysis' in daily.columns and analysis_mask is not None:
        daily_analysis = daily['is_analysis'].astype(bool)
    else:
        daily_analysis = pd.Series(True, index=daily.index)

    # Нужно минимум 30 дней ВСЕХ данных (reference + analysis) для consumption shift
    if len(daily) < 30:
        return anomalies

    # 1. CUSUM on temperature-detrended residuals
    # Regression on ALL data, violations filtered to analysis
    valid = daily.dropna(subset=['daily_vol', 'mean_temp'])
    if len(valid) > 30 and valid['mean_temp'].std() > 0.01:
        slope, intercept, _, _, _ = stats.linregress(valid['mean_temp'], valid['daily_vol'])
        valid = valid.copy()
        valid['expected'] = intercept + slope * valid['mean_temp']
        valid['residual'] = valid['daily_vol'] - valid['expected']

        sigma_res = valid['residual'].std()
        if sigma_res > 0:
            cusum = valid['residual'].cumsum()
            threshold = 4 * sigma_res * np.sqrt(np.arange(1, len(cusum) + 1))
            # Find where cusum exceeds threshold
            violations = valid[cusum.abs().values > threshold]
            # Filter to analysis period
            violations = violations[daily_analysis.reindex(violations.index, fill_value=True)]
            if len(violations) > 0:
                # Group consecutive violations
                viol_groups = (violations['contract_date'].diff() > pd.Timedelta(days=3)).cumsum()
                for _, grp in violations.groupby(viol_groups):
                    start_dt = pd.Timestamp(grp['contract_date'].iloc[0])
                    end_dt = pd.Timestamp(grp['contract_date'].iloc[-1]) + pd.Timedelta(hours=23)
                    cusum_val = cusum.loc[grp.index].iloc[-1]
                    anomalies.append(make_anomaly(
                        line_id, grs_name, 'Consumption_Shifts', 'CUSUM_SHIFT',
                        start_dt, end_dt,
                        'volume', cusum_val, 0.0, 'HIGH',
                        f'CUSUM сдвиг: значение {cusum_val:.0f} (порог 4*sigma={4*sigma_res:.0f}), {len(grp)} дней',
                        'Обследовать участок на утечку или несанкционированный отбор'))

    # 2. Rolling comparison relative to system trend
    daily['ma_7d'] = daily['daily_vol'].rolling(7, min_periods=5).mean()
    daily['ma_30d'] = daily['daily_vol'].rolling(30, min_periods=20).mean()
    daily['ma_ratio'] = daily['ma_7d'] / daily['ma_30d'].clip(lower=1)
    daily['ma_abs_diff'] = (daily['ma_7d'] - daily['ma_30d']).abs()

    # Normalize against system-wide trend: if all lines grow (cold week), don't flag
    if system_ma_ratio is not None:
        daily = daily.merge(
            system_ma_ratio.rename('sys_ratio'),
            left_on='contract_date', right_index=True, how='left')
        daily['sys_ratio'] = daily['sys_ratio'].fillna(1.0)
        daily['relative_ratio'] = daily['ma_ratio'] / daily['sys_ratio'].clip(lower=0.1)
    else:
        daily['relative_ratio'] = daily['ma_ratio']

    # Detect sustained shifts (5+ consecutive days of relative_ratio > 1.3 or < 0.7)
    abs_diff_ok = daily['ma_abs_diff'] > BASELINE_SHIFT_MIN_ABS_DIFF
    shift_high = (daily['relative_ratio'] > 1.3) & abs_diff_ok
    shift_low = (daily['relative_ratio'] < 0.7) & abs_diff_ok

    for shift_mask, direction in [(shift_high, 'рост'), (shift_low, 'падение')]:
        if shift_mask.sum() == 0:
            continue
        # Apply analysis filter
        filtered = shift_mask & daily_analysis
        if filtered.sum() == 0:
            continue
        groups = (filtered != filtered.shift()).cumsum()
        for _, grp in daily[filtered].groupby(groups):
            if len(grp) >= 5:
                start_dt = pd.Timestamp(grp['contract_date'].iloc[0])
                end_dt = pd.Timestamp(grp['contract_date'].iloc[-1]) + pd.Timedelta(hours=23)
                mean_ratio = grp['relative_ratio'].mean()
                mean_sys = grp['sys_ratio'].mean() if 'sys_ratio' in grp.columns else 1.0
                anomalies.append(make_anomaly(
                    line_id, grs_name, 'Consumption_Shifts', 'BASELINE_SHIFT',
                    start_dt, end_dt,
                    'volume', mean_ratio, 1.0, 'HIGH',
                    f'Устойчивый {direction} относительно системы: '
                    f'отн. ratio={mean_ratio:.2f} (сист. тренд={mean_sys:.2f}), {len(grp)} дн.',
                    'Проверить на утечку (рост) или ограничение подачи (падение)'))

    # 3. Anti-correlation vol-ambient_temp in heating season
    # Use ambient temperature from reference line (not line's own gas temperature)
    heating = df[df['month'].isin([1, 2, 3, 10, 11, 12])].copy()
    if len(heating) > 168 and ambient_temp is not None:
        heating = heating.sort_values('datetime')
        # Merge ambient temperature by datetime
        heating = heating.merge(
            ambient_temp.rename('ambient_temp'),
            left_on='datetime', right_index=True, how='left')
        heating = heating.dropna(subset=['ambient_temp'])
        if len(heating) > 168:
            heating['rolling_corr'] = heating['ambient_temp'].rolling(
                168, min_periods=100).corr(heating['pop_vol'])

            # Normal: strong negative correlation; anomaly: corr > -0.3
            weak_corr = heating[heating['rolling_corr'] > -0.3].dropna(subset=['rolling_corr'])
            # Filter to analysis period
            weak_corr = weak_corr[hourly_mask.reindex(weak_corr.index, fill_value=True)]
            if len(weak_corr) > 0:
                groups = (weak_corr['datetime'].diff() > pd.Timedelta(hours=24)).cumsum()
                for _, grp in weak_corr.groupby(groups):
                    if len(grp) >= 24:  # at least 1 day of weak correlation
                        start_dt = grp['datetime'].iloc[0]
                        end_dt = grp['datetime'].iloc[-1]
                        mean_corr = grp['rolling_corr'].mean()
                        anomalies.append(make_anomaly(
                            line_id, grs_name, 'Consumption_Shifts', 'WEAK_TEMP_CORRELATION',
                            start_dt, end_dt,
                            'volume', mean_corr, -0.5, 'MEDIUM',
                            f'Слабая корреляция с уличной T = {mean_corr:.2f} '
                            f'в отопительный сезон ({len(grp)}ч)',
                            'Возможно аномальное потребление, не связанное с температурой'))

    return anomalies


# ============================================================================
# SCORING
# ============================================================================

def score_anomalies(anomalies):
    """
    Scoring: severity_score = base_score * duration_mult
    base_score: CRITICAL=90, HIGH=70, MEDIUM=40, LOW=15
    duration_mult: 1 + log10(hours) / 3, cap 1.5
    No overlap multiplier — it inflated scores excessively.
    Reclassify thresholds: CRITICAL >= 90, HIGH >= 65, MEDIUM >= 35
    """
    if not anomalies:
        return anomalies

    for a in anomalies:
        base = SEVERITY_SCORES.get(a['severity_label'], 40)
        hours = max(a['duration_hours'], 1.0)
        duration_mult = min(1 + np.log10(hours) / 3, 1.5)
        a['severity_score'] = min(round(base * duration_mult, 1), 100)

    # Reclassify by score
    for a in anomalies:
        if a['severity_score'] >= 90:
            a['severity_label'] = 'CRITICAL'
        elif a['severity_score'] >= 65:
            a['severity_label'] = 'HIGH'
        elif a['severity_score'] >= 35:
            a['severity_label'] = 'MEDIUM'
        else:
            a['severity_label'] = 'LOW'

    return anomalies


# ============================================================================
# ОТЧЁТ
# ============================================================================

def generate_text_report(anomalies_df, lineid_to_grs, start_date, end_date,
                         reference_days=0, active_detectors=None, inactive_detectors=None):
    """Генерация текстового заключения."""
    lines = []
    lines.append('=' * 80)
    lines.append('ЗАКЛЮЧЕНИЕ ПО РЕЗУЛЬТАТАМ АНАЛИЗА АНОМАЛИЙ')
    lines.append(f'Период: {start_date} — {end_date}')
    lines.append('=' * 80)

    # Секция конфигурации анализа
    analysis_days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1
    lines.append('')
    lines.append('КОНФИГУРАЦИЯ АНАЛИЗА')
    lines.append(f'Период анализа: {start_date} — {end_date} ({analysis_days} дн.)')
    if reference_days > 0:
        lines.append(f'Референсный период: {reference_days} дн. (для базовых линий)')
    else:
        lines.append('Референсный период: не используется')
    if active_detectors is not None:
        total_detectors = len(active_detectors) + (len(inactive_detectors) if inactive_detectors else 0)
        lines.append(f'Активные детекторы: {len(active_detectors)}/{total_detectors}')
        if inactive_detectors:
            inactive_list = ', '.join(f'{d} ({r})' for d, r in inactive_detectors.items())
            lines.append(f'Неактивные: {inactive_list}')
    lines.append('')

    total = len(anomalies_df)
    n_crit = len(anomalies_df[anomalies_df['severity_label'] == 'CRITICAL'])
    n_high = len(anomalies_df[anomalies_df['severity_label'] == 'HIGH'])
    n_med = len(anomalies_df[anomalies_df['severity_label'] == 'MEDIUM'])
    n_low = len(anomalies_df[anomalies_df['severity_label'] == 'LOW'])

    if n_crit > 5:
        status = 'КРИТИЧНО — требуется немедленное вмешательство'
    elif n_crit > 0 or n_high > 10:
        status = 'ВНИМАНИЕ — обнаружены серьёзные аномалии'
    else:
        status = 'НОРМА — серьёзных проблем не обнаружено'

    lines.append(f'\nОбщая оценка: {status}')
    lines.append(f'Всего аномалий: {total} (CRITICAL: {n_crit}, HIGH: {n_high}, MEDIUM: {n_med}, LOW: {n_low})')

    by_cat = anomalies_df['category'].value_counts()
    lines.append('\nПо категориям:')
    for cat, cnt in by_cat.items():
        lines.append(f'  {cat}: {cnt}')

    # Top-5 anomalies
    lines.append('\n' + '-' * 80)
    lines.append('ТОП-5 САМЫХ СЕРЬЁЗНЫХ АНОМАЛИЙ')
    lines.append('-' * 80)
    top5 = anomalies_df.nlargest(5, 'severity_score')
    for i, (_, row) in enumerate(top5.iterrows(), 1):
        lines.append(f'\n{i}. [{row["severity_label"]}] Score={row["severity_score"]:.0f} | '
                      f'Линия {row["line_id"]} ({row["grs_name"]})')
        lines.append(f'   {row["description"]}')
        lines.append(f'   Период: {row["start_datetime"]} — {row["end_datetime"]}')
        lines.append(f'   Рекомендация: {row["recommended_action"]}')

    # Per-line summary
    lines.append('\n' + '-' * 80)
    lines.append('ПО ЛИНИЯМ')
    lines.append('-' * 80)
    for lid in sorted(anomalies_df['line_id'].unique()):
        grs = lineid_to_grs.get(lid, str(lid))
        line_anom = anomalies_df[anomalies_df['line_id'] == lid]
        n = len(line_anom)
        top_cat = line_anom['category'].value_counts().index[0] if n > 0 else '-'
        max_score = line_anom['severity_score'].max() if n > 0 else 0
        top_issue = line_anom.nlargest(1, 'severity_score')
        desc = top_issue['description'].iloc[0] if len(top_issue) > 0 else '-'
        rec = top_issue['recommended_action'].iloc[0] if len(top_issue) > 0 else '-'
        lines.append(f'\nЛиния {lid} ({grs}): {n} аномалий, макс. score={max_score:.0f}, '
                      f'основная категория: {top_cat}')
        lines.append(f'  Главная проблема: {desc}')
        lines.append(f'  Рекомендация: {rec}')

    # Cross-line observations
    cross = anomalies_df[anomalies_df['category'] == 'Cross_Line']
    if len(cross) > 0:
        lines.append('\n' + '-' * 80)
        lines.append('КРОСС-ЛИНЕЙНЫЕ ВЫВОДЫ')
        lines.append('-' * 80)
        cross_lines = cross['line_id'].value_counts()
        for lid, cnt in cross_lines.head(5).items():
            grs = lineid_to_grs.get(lid, str(lid))
            lines.append(f'  Линия {lid} ({grs}): {cnt} кросс-линейных отклонений')

    # Recommendations
    lines.append('\n' + '-' * 80)
    lines.append('РЕКОМЕНДАЦИИ ПО ДЕЙСТВИЯМ')
    lines.append('-' * 80)
    recommendations = set()
    for _, row in anomalies_df[anomalies_df['severity_score'] >= 60].iterrows():
        recommendations.add(row['recommended_action'])
    for i, rec in enumerate(sorted(recommendations), 1):
        lines.append(f'  {i}. {rec}')

    lines.append('\n' + '=' * 80)

    return '\n'.join(lines)


def export_excel(anomalies_df, hourly_data, lineid_to_grs, output_path):
    """Экспорт результатов в Excel с листами по категориям."""
    print('\n[*] Экспорт в Excel...')
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        # Summary — top anomalies
        summary = anomalies_df.sort_values('severity_score', ascending=False).head(100)
        summary.to_excel(writer, sheet_name='Summary', index=False)

        # By_Line
        by_line = anomalies_df.sort_values(['line_id', 'severity_score'], ascending=[True, False])
        by_line.to_excel(writer, sheet_name='By_Line', index=False)

        # Per-category sheets
        for cat, sheet_name in [
            ('Temperature', 'Temperature'),
            ('Pressure', 'Pressure'),
            ('Sensor_Issues', 'Sensor_Issues'),
            ('Consumption_Shifts', 'Consumption_Shifts'),
            ('Cross_Line', 'Cross_Line'),
            ('Data_Quality', 'Data_Quality'),
        ]:
            cat_df = anomalies_df[anomalies_df['category'] == cat].sort_values(
                'severity_score', ascending=False)
            if len(cat_df) == 0:
                cat_df = pd.DataFrame(columns=anomalies_df.columns)
            cat_df.to_excel(writer, sheet_name=sheet_name, index=False)

        # Statistics sheet — baseline stats used
        stats_data = []
        for lid in sorted(hourly_data['line_id'].unique()):
            grs = lineid_to_grs.get(lid, str(lid))
            ld = hourly_data[hourly_data['line_id'] == lid]
            stats_data.append({
                'line_id': lid,
                'grs_name': grs,
                'n_records': len(ld),
                'temp_mean': ld['temperature'].mean(),
                'temp_std': ld['temperature'].std(),
                'temp_min': ld['temperature'].min(),
                'temp_max': ld['temperature'].max(),
                'pressure_mean': ld['pressure'].mean() if 'pressure' in ld.columns else None,
                'pressure_std': ld['pressure'].std() if 'pressure' in ld.columns else None,
                'volume_mean': ld['volume'].mean(),
                'volume_std': ld['volume'].std(),
                'pop_vol_mean': ld['pop_vol'].mean(),
                'pop_vol_std': ld['pop_vol'].std(),
                'density_mean': ld['density'].mean() if 'density' in ld.columns else None,
                'zero_pct': (ld['volume'] == 0).mean() * 100,
            })
        pd.DataFrame(stats_data).to_excel(writer, sheet_name='Statistics', index=False)

    print(f'[OK] {output_path}')


# ============================================================================
# ВИЗУАЛИЗАЦИЯ
# ============================================================================

def plot_timeline_heatmap(anomalies_df, lineid_to_grs, output_path, analysis_days=None):
    """Тепловая карта: линия x неделя/день, цвет = max severity."""
    if len(anomalies_df) == 0:
        return

    df = anomalies_df.copy()

    # Adaptive grouping: days for short periods, weeks for long
    if analysis_days is not None and analysis_days <= 7:
        df['time_group'] = pd.to_datetime(df['start_datetime']).dt.strftime('%m-%d')
        xlabel = 'День'
        title_suffix = 'линия x день'
    else:
        df['time_group'] = pd.to_datetime(df['start_datetime']).dt.isocalendar().week.astype(int)
        xlabel = 'Неделя года'
        title_suffix = 'линия x неделя'

    pivot = df.groupby(['line_id', 'time_group'])['severity_score'].max().reset_index()
    pivot['grs'] = pivot['line_id'].map(lineid_to_grs).fillna(pivot['line_id'].astype(str))
    heatmap_data = pivot.pivot(index='grs', columns='time_group', values='severity_score').fillna(0)

    fig, ax = plt.subplots(figsize=(20, max(6, len(heatmap_data) * 0.6)))
    sns.heatmap(heatmap_data, cmap='YlOrRd', vmin=0, vmax=100, annot=False,
                ax=ax, cbar_kws={'label': 'Max Severity Score'})
    ax.set_title(f'Тепловая карта аномалий: {title_suffix}', fontsize=14, fontweight='bold')
    ax.set_xlabel(xlabel)
    ax.set_ylabel('Линия (ГРС)')
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'[OK] {output_path.name}')


def plot_sensor_health(line_df, line_id, grs_name, line_anomalies, output_path):
    """Таймлайн T/P/V с маркерами аномалий для одной линии."""
    df = line_df.sort_values('datetime')

    fig, axes = plt.subplots(3, 1, figsize=(18, 10), sharex=True)

    # Temperature
    axes[0].plot(df['datetime'], df['temperature'], 'b-', linewidth=0.5, alpha=0.7)
    temp_anom = [a for a in line_anomalies if a['affected_field'] == 'temperature']
    for a in temp_anom:
        axes[0].axvspan(a['start_datetime'], a['end_datetime'],
                        alpha=0.3, color='red' if a['severity_label'] in ('CRITICAL', 'HIGH') else 'orange')
    axes[0].set_ylabel('Температура (C)')
    axes[0].set_title(f'Линия {line_id} ({grs_name}) — Мониторинг датчиков', fontsize=12, fontweight='bold')
    axes[0].grid(True, alpha=0.3)

    # Pressure
    if 'pressure' in df.columns and line_id not in EXCLUDE_PRESSURE_LINES:
        axes[1].plot(df['datetime'], df['pressure'], 'g-', linewidth=0.5, alpha=0.7)
        p_anom = [a for a in line_anomalies if a['affected_field'] == 'pressure']
        for a in p_anom:
            axes[1].axvspan(a['start_datetime'], a['end_datetime'],
                            alpha=0.3, color='red' if a['severity_label'] in ('CRITICAL', 'HIGH') else 'orange')
    axes[1].set_ylabel('Давление (кг/см²)')
    axes[1].grid(True, alpha=0.3)

    # Volume
    axes[2].plot(df['datetime'], df['pop_vol'], 'purple', linewidth=0.5, alpha=0.7)
    v_anom = [a for a in line_anomalies if a['affected_field'] == 'volume']
    for a in v_anom:
        axes[2].axvspan(a['start_datetime'], a['end_datetime'],
                        alpha=0.3, color='red' if a['severity_label'] in ('CRITICAL', 'HIGH') else 'orange')
    axes[2].set_ylabel('Объём (м³)')
    axes[2].set_xlabel('Дата')
    axes[2].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()


def plot_cusum(line_df, line_id, grs_name, output_path, analysis_start=None, analysis_end=None):
    """CUSUM chart для сдвигов потребления. При наличии reference — подсвечивает analysis period."""
    df = line_df.sort_values('datetime')
    daily = df.groupby('contract_date').agg(
        daily_vol=('pop_vol', 'sum'),
        mean_temp=('temperature', 'mean'),
    ).reset_index().sort_values('contract_date').dropna()

    if len(daily) < 30 or daily['mean_temp'].std() < 0.01:
        return

    slope, intercept, _, _, _ = stats.linregress(daily['mean_temp'], daily['daily_vol'])
    daily['expected'] = intercept + slope * daily['mean_temp']
    daily['residual'] = daily['daily_vol'] - daily['expected']
    sigma_res = daily['residual'].std()
    if sigma_res == 0:
        return

    daily['cusum'] = daily['residual'].cumsum()
    threshold = 4 * sigma_res * np.sqrt(np.arange(1, len(daily) + 1))

    fig, axes = plt.subplots(2, 1, figsize=(16, 8), sharex=True)

    axes[0].plot(daily['contract_date'], daily['daily_vol'], 'b-', linewidth=0.8, label='Факт')
    axes[0].plot(daily['contract_date'], daily['expected'], 'r--', linewidth=0.8, label='Ожидаемое (по T)')
    axes[0].set_ylabel('Суточный объём (м³)')
    axes[0].set_title(f'CUSUM — Линия {line_id} ({grs_name})', fontsize=12, fontweight='bold')
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(daily['contract_date'], daily['cusum'], 'b-', linewidth=1.0, label='CUSUM')
    axes[1].fill_between(daily['contract_date'], threshold, -threshold, alpha=0.15, color='red', label='±4σ порог')
    axes[1].axhline(0, color='gray', linestyle='--', linewidth=0.5)
    axes[1].set_ylabel('CUSUM')
    axes[1].set_xlabel('Дата')
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    # Highlight analysis period if reference is used
    if analysis_start and analysis_end:
        a_start = pd.Timestamp(analysis_start)
        a_end = pd.Timestamp(analysis_end)
        for ax in axes:
            ax.axvspan(a_start, a_end, alpha=0.08, color='blue', zorder=0)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()


def plot_cross_line_deviation(hourly_data, lineid_to_grs, output_path,
                              analysis_start=None, analysis_end=None):
    """Нормализованные расходы всех линий."""
    daily = hourly_data.groupby(['contract_date', 'line_id']).agg(
        daily_vol=('pop_vol', 'sum'),
        mean_temp=('temperature', 'mean'),
    ).reset_index()

    daily['temp_bin'] = pd.cut(daily['mean_temp'], bins=10, labels=False)
    bin_median = daily.groupby('temp_bin')['daily_vol'].median().rename('median_vol')
    daily = daily.merge(bin_median, on='temp_bin', how='left')
    daily['norm_vol'] = daily['daily_vol'] / daily['median_vol'].clip(lower=1)

    line_ids = sorted(daily['line_id'].unique())
    n_lines = len(line_ids)
    if n_lines == 0:
        return

    fig, ax = plt.subplots(figsize=(18, 8))
    for lid in line_ids:
        ld = daily[daily['line_id'] == lid].sort_values('contract_date')
        grs = lineid_to_grs.get(lid, str(lid))
        # Smooth with rolling 7-day
        ld_smooth = ld.set_index('contract_date')['norm_vol'].rolling(7, min_periods=3).mean()
        ax.plot(ld_smooth.index, ld_smooth.values, linewidth=1.0, label=f'{lid} ({grs})', alpha=0.8)

    ax.axhline(1.0, color='black', linestyle='--', linewidth=0.8, label='Медиана')
    ax.axhline(2.0, color='red', linestyle=':', linewidth=0.8, label='Верх. порог (2.0x)')
    ax.axhline(0.3, color='red', linestyle=':', linewidth=0.8, label='Ниж. порог (0.3x)')
    ax.set_ylabel('Нормализованный расход (x медианы)')
    ax.set_xlabel('Дата')
    ax.set_title('Кросс-линейное сравнение: нормализованный расход', fontsize=13, fontweight='bold')
    ax.legend(fontsize=8, loc='upper right')
    ax.grid(True, alpha=0.3)

    # Highlight analysis period if reference is used
    if analysis_start and analysis_end:
        ax.axvspan(pd.Timestamp(analysis_start), pd.Timestamp(analysis_end),
                   alpha=0.08, color='blue', zorder=0)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'[OK] {output_path.name}')


# ============================================================================
# MAIN
# ============================================================================

def run_anomaly_analysis(start_date=None, end_date=None, line_ids=None, no_reference=False):
    """
    Главная функция анализа аномалий.

    Args:
        start_date: начало периода (str 'YYYY-MM-DD')
        end_date: конец периода (str 'YYYY-MM-DD')
        line_ids: список ID линий для анализа
        no_reference: если True, не загружать reference period (старое поведение)
    """
    start_date = start_date or ANALYSIS_START
    end_date = end_date or ANALYSIS_END
    line_ids = line_ids or TARGET_LINE_IDS

    # Compute reference period
    if no_reference:
        reference_start = None
        reference_days = 0
        active_detectors = None  # None = все активны (старое поведение)
        inactive_detectors = {}
    else:
        reference_start, reference_days, active_detectors, inactive_detectors = \
            compute_reference_period(start_date, end_date)

    analysis_days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1

    print('=' * 80)
    print('КОМПЛЕКСНЫЙ АНАЛИЗ АНОМАЛИЙ ГАЗОРАСПРЕДЕЛИТЕЛЬНЫХ СТАНЦИЙ')
    print(f'Период анализа: {start_date} — {end_date} ({analysis_days} дн.)')
    if reference_days > 0:
        print(f'Референсный период: {reference_days} дн. до {start_date} (для базовых линий)')
    print(f'Линии: {line_ids}')
    if active_detectors is not None:
        print(f'Активных детекторов: {len(active_detectors)}/19')
        if inactive_detectors:
            for det, reason in inactive_detectors.items():
                print(f'  [SKIP] {det}: {reason}')
    print('=' * 80)

    engine = create_engine(DB_URL)

    # 1. Load data (with reference period if needed)
    hourly, lineid_to_grs, grs_to_lineid, virtual_lines = load_data(
        engine, start_date, end_date, line_ids, reference_start=reference_start)

    # 2. Prepare data
    hourly = prepare_data(hourly, grs_to_lineid, virtual_lines)

    # Detect household meters (max_dp <= threshold) and exclude from analysis
    household_lines = get_household_lines(engine, line_ids)
    if household_lines:
        names = [f'{lid} ({lineid_to_grs.get(lid, "")})' for lid in sorted(household_lines)]
        print(f'\n[*] Бытовые счётчики (max_dp <= {HOUSEHOLD_MAX_DP_THRESHOLD}), исключены: {", ".join(names)}')

    # Pre-compute system-wide 7d/30d ratio for BASELINE_SHIFT normalization
    print('\n[*] Расчёт системного тренда потребления...')
    system_daily = hourly.groupby('contract_date').agg(
        sys_vol=('pop_vol', 'sum')).reset_index().sort_values('contract_date')
    system_daily['sys_ma_7d'] = system_daily['sys_vol'].rolling(7, min_periods=5).mean()
    system_daily['sys_ma_30d'] = system_daily['sys_vol'].rolling(30, min_periods=20).mean()
    system_daily['sys_ratio'] = system_daily['sys_ma_7d'] / system_daily['sys_ma_30d'].clip(lower=1)
    system_ma_ratio = system_daily.set_index('contract_date')['sys_ratio']
    print(f'  Системный тренд: {len(system_daily)} дней, ratio диапазон '
          f'[{system_ma_ratio.min():.2f}, {system_ma_ratio.max():.2f}]')

    # Extract ambient temperature from reference line (line 19)
    ambient_temp = None
    if AMBIENT_TEMP_LINE_ID in hourly['line_id'].values:
        ambient_df = hourly[hourly['line_id'] == AMBIENT_TEMP_LINE_ID][['datetime', 'temperature']].copy()
        ambient_df = ambient_df.dropna(subset=['temperature'])
        if len(ambient_df) > 0:
            ambient_temp = ambient_df.set_index('datetime')['temperature']
            ambient_temp = ambient_temp[~ambient_temp.index.duplicated(keep='first')]
            print(f'\n[*] Температура воздуха (линия {AMBIENT_TEMP_LINE_ID}): '
                  f'{len(ambient_temp)} записей, диапазон [{ambient_temp.min():.1f}, {ambient_temp.max():.1f}]°C')
    if ambient_temp is None:
        print(f'\n[!] Температура воздуха (линия {AMBIENT_TEMP_LINE_ID}) не найдена, '
              f'WEAK_TEMP_CORRELATION будет пропущен')

    # 3-8. Run all detectors
    all_anomalies = []

    for line_id in sorted(hourly['line_id'].unique()):
        if line_id not in line_ids:
            continue
        if line_id in household_lines:
            continue

        grs_name = lineid_to_grs.get(line_id, str(line_id))
        line_df = hourly[hourly['line_id'] == line_id].copy()
        line_df = line_df.sort_values('datetime').reset_index(drop=True)

        # Create analysis_mask for this line's data
        if reference_start and not no_reference:
            analysis_mask = line_df['is_analysis_period']
        else:
            analysis_mask = None

        n_analysis = line_df['is_analysis_period'].sum() if 'is_analysis_period' in line_df.columns else len(line_df)
        print(f'\n[*] Линия {line_id} ({grs_name}): {len(line_df):,} записей (анализ: {n_analysis:,})')

        # 3. Temperature anomalies
        temp_anom = detect_temperature_anomalies(line_df, line_id, grs_name, analysis_mask=analysis_mask)
        print(f'  Температура: {len(temp_anom)} аномалий')
        all_anomalies.extend(temp_anom)

        # 4. Stuck sensors
        stuck_anom = detect_stuck_sensors(line_df, line_id, grs_name, analysis_mask=analysis_mask)
        print(f'  Зависание датчиков: {len(stuck_anom)} аномалий')
        all_anomalies.extend(stuck_anom)

        # 5. Pressure anomalies
        pressure_anom = detect_pressure_anomalies(line_df, line_id, grs_name, analysis_mask=analysis_mask)
        print(f'  Давление: {len(pressure_anom)} аномалий')
        all_anomalies.extend(pressure_anom)

        # 6. Data quality
        dq_anom = detect_data_quality(line_df, line_id, grs_name, analysis_mask=analysis_mask)
        print(f'  Качество данных: {len(dq_anom)} аномалий')
        all_anomalies.extend(dq_anom)

        # 8. Consumption shifts (with system trend normalization)
        shift_anom = detect_consumption_shifts(line_df, line_id, grs_name,
                                               analysis_mask=analysis_mask,
                                               system_ma_ratio=system_ma_ratio,
                                               ambient_temp=ambient_temp)
        print(f'  Сдвиг потребления: {len(shift_anom)} аномалий')
        all_anomalies.extend(shift_anom)

    # 7. Cross-line deviations (needs all lines, exclude household)
    print('\n[*] Кросс-линейные отклонения...')
    hourly_no_household = hourly[~hourly['line_id'].isin(household_lines)]
    cross_anom = detect_cross_line_deviations(
        hourly_no_household, lineid_to_grs,
        analysis_start=start_date if not no_reference else None,
        analysis_end=end_date if not no_reference else None,
        active_detectors=active_detectors)
    print(f'  Кросс-линейных: {len(cross_anom)} аномалий')
    all_anomalies.extend(cross_anom)

    # 9. Score all anomalies
    print('\n[*] Scoring аномалий...')
    all_anomalies = score_anomalies(all_anomalies)
    print(f'  Всего аномалий: {len(all_anomalies)}')

    if len(all_anomalies) == 0:
        print('\n[OK] Аномалий не обнаружено!')
        return pd.DataFrame()

    anomalies_df = pd.DataFrame(all_anomalies)
    anomalies_df = anomalies_df.sort_values('severity_score', ascending=False).reset_index(drop=True)

    # 10. Console report
    print('\n' + '=' * 80)
    print('РЕЗУЛЬТАТЫ АНАЛИЗА')
    print('=' * 80)
    print(f'\nВсего аномалий: {len(anomalies_df)}')
    print(f'  CRITICAL: {len(anomalies_df[anomalies_df["severity_label"] == "CRITICAL"])}')
    print(f'  HIGH: {len(anomalies_df[anomalies_df["severity_label"] == "HIGH"])}')
    print(f'  MEDIUM: {len(anomalies_df[anomalies_df["severity_label"] == "MEDIUM"])}')
    print(f'  LOW: {len(anomalies_df[anomalies_df["severity_label"] == "LOW"])}')

    print(f'\nПо категориям:')
    for cat, cnt in anomalies_df['category'].value_counts().items():
        print(f'  {cat}: {cnt}')

    print(f'\nТоп-10 аномалий:')
    for i, (_, row) in enumerate(anomalies_df.head(10).iterrows(), 1):
        print(f'  {i}. [{row["severity_label"]}] Score={row["severity_score"]:.0f} | '
              f'{row["line_id"]} ({row["grs_name"]}) | {row["description"][:80]}')

    # Text report
    report_text = generate_text_report(
        anomalies_df, lineid_to_grs, start_date, end_date,
        reference_days=reference_days, active_detectors=active_detectors,
        inactive_detectors=inactive_detectors)
    print('\n' + report_text)

    report_path = DATA_DIR / 'anomaly_analysis_report.txt'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)
    print(f'\n[OK] Отчёт сохранён: {report_path}')

    # Excel export — only analysis period data for statistics
    hourly_for_export = hourly[hourly['is_analysis_period']] if 'is_analysis_period' in hourly.columns else hourly
    excel_path = DATA_DIR / 'anomaly_analysis_results.xlsx'
    export_excel(anomalies_df, hourly_for_export, lineid_to_grs, excel_path)

    # 11. Visualizations
    print('\n[*] Визуализация...')

    # Timeline heatmap — adaptive grouping
    plot_timeline_heatmap(anomalies_df, lineid_to_grs, OUTPUT_DIR / 'timeline_heatmap.png',
                          analysis_days=analysis_days)

    # Sensor health per line — only analysis period
    hourly_viz = hourly[hourly['is_analysis_period']] if ('is_analysis_period' in hourly.columns and reference_start) else hourly
    for line_id in sorted(hourly['line_id'].unique()):
        if line_id not in line_ids:
            continue
        grs_name = lineid_to_grs.get(line_id, str(line_id))
        line_df = hourly_viz[hourly_viz['line_id'] == line_id]
        line_anom_list = [a for a in all_anomalies if a['line_id'] == line_id]
        plot_sensor_health(line_df, line_id, grs_name, line_anom_list,
                           OUTPUT_DIR / f'sensor_health_line{line_id}.png')

    # CUSUM per line — show all data, highlight analysis period
    for line_id in sorted(hourly['line_id'].unique()):
        if line_id not in line_ids:
            continue
        grs_name = lineid_to_grs.get(line_id, str(line_id))
        line_df = hourly[hourly['line_id'] == line_id]
        plot_cusum(line_df, line_id, grs_name, OUTPUT_DIR / f'cusum_line{line_id}.png',
                   analysis_start=start_date if reference_start else None,
                   analysis_end=end_date if reference_start else None)

    # Cross-line deviation — show all, highlight analysis
    plot_cross_line_deviation(hourly, lineid_to_grs, OUTPUT_DIR / 'cross_line_deviation.png',
                              analysis_start=start_date if reference_start else None,
                              analysis_end=end_date if reference_start else None)

    print(f'\n[OK] Графики сохранены в {OUTPUT_DIR}')

    print(f'\n{"=" * 80}')
    print('АНАЛИЗ ЗАВЕРШЁН')
    print(f'{"=" * 80}')
    print(f'  Отчёт: {report_path}')
    print(f'  Excel: {excel_path}')
    print(f'  Графики: {OUTPUT_DIR}')

    return anomalies_df


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Anomaly analysis for gas distribution data')
    parser.add_argument('--start', type=str, default=None, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default=None, help='End date (YYYY-MM-DD)')
    parser.add_argument('--days', type=int, default=30,
                        help='Number of days to analyze from latest data (default: 30). '
                             'Ignored if --start/--end are provided.')
    parser.add_argument('--no-reference', action='store_true',
                        help='Disable reference period (use old behavior)')
    args = parser.parse_args()

    if args.start and args.end:
        # Explicit date range
        start_date = args.start
        end_date = args.end
    elif args.start or args.end:
        # Partial — use defaults for missing
        start_date = args.start or ANALYSIS_START
        end_date = args.end or ANALYSIS_END
    else:
        # Auto-detect: query MAX(period) from DB, analyze last N days
        print(f'[*] Авто-определение периода анализа (последние {args.days} дней)...')
        engine = create_engine(DB_URL)
        with engine.connect() as conn:
            result = conn.execute(text('SELECT MAX(period) FROM hourly_archive'))
            max_period = result.scalar()
        if max_period is None:
            print('[!] Нет данных в hourly_archive, используем значения по умолчанию')
            start_date = ANALYSIS_START
            end_date = ANALYSIS_END
        else:
            end_dt = pd.Timestamp(max_period)
            start_dt = end_dt - pd.Timedelta(days=args.days - 1)
            start_date = start_dt.strftime('%Y-%m-%d')
            end_date = end_dt.strftime('%Y-%m-%d')
            print(f'  Последняя запись: {max_period}')
            print(f'  Период анализа: {start_date} — {end_date}')

    run_anomaly_analysis(start_date=start_date, end_date=end_date, no_reference=args.no_reference)
