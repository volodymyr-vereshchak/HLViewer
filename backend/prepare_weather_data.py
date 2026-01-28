"""
Підготовка файлу погодних умов з Meteostat для аналізу споживання газу.

Станція: 34601 (Запоріжжя, UA)
Дані: суточні температури, вологість, тиск, вітер, хмарність (2025-01 — 2026-01).
Вихідний файл: backend/data/weather_2025.csv
"""

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
from meteostat import daily, hourly

STATION_ID = "34601"
START = datetime(2024, 12, 31)  # Extended to get complete first contractual day
END = datetime(2026, 2, 1)      # Extended to get complete last contractual day

DATA_DIR = Path(__file__).parent / "data"

print(f"Завантаження даних зі станції {STATION_ID} ({START.date()} — {END.date()})...")

ts = hourly(STATION_ID, START, END)

if ts.empty:
    raise RuntimeError(f"Немає даних для станції {STATION_ID}")

df = ts.fetch()

# Конвертація часу з UTC до Europe/Kiev (контрактна доба)
df.index = df.index.tz_localize('UTC').tz_convert('Europe/Kiev')
df.index = df.index.tz_localize(None)  # Remove tz info for easier processing

# Перейменування колонок для сумісності з analysis_population_gas.py
df.index.name = "date"
df = df.rename(columns={
    "temp": "temperature",       # середня t, °C
    "tmin": "temperature_min",   # мінімальна t, °C
    "tmax": "temperature_max",   # максимальна t, °C
    "rhum": "humidity",          # відносна вологість, %
    "prcp": "precipitation",     # опади, мм
    "wspd": "wind_speed",        # швидкість вітру, км/год
    "pres": "pressure",          # тиск на рівні станції, гПа
    "cldc": "cloud_cover",       # хмарність, окта (0-8)
})

# Залишаємо тільки корисні колонки (snwd, wpgt, tsun мають багато NA)
keep_cols = [
    "temperature", "temperature_min", "temperature_max",
    "humidity", "precipitation", "wind_speed", "pressure", "cloud_cover",
]
df = df[[c for c in keep_cols if c in df.columns]]

# Заповнення поодиноких пропусків інтерполяцією
na_before = df.isna().sum()
df = df.interpolate(method="linear", limit=3)
na_after = df.isna().sum()

out_path = DATA_DIR / "weather_2025_h.csv"
df.to_csv(out_path)

print(f"Збережено: {out_path}")
print(f"Записів: {len(df)}")
print(f"Діапазон дат: {df.index.min().date()} .. {df.index.max().date()}")
print(f"\nПропуски до/після інтерполяції:")
for col in df.columns:
    print(f"  {col}: {na_before[col]} -> {na_after[col]}")
print(f"\nСтатистика температури:")
print(f"  min={df['temperature'].min():.1f}°C, max={df['temperature'].max():.1f}°C, "
      f"mean={df['temperature'].mean():.1f}°C")


# ============================================================================
# Агрегація погодинних даних до добових з урахуванням контрактної доби 7:00-7:00
# ============================================================================

def aggregate_hourly_to_contractual_daily(hourly_df):
    """
    Aggregate hourly weather to daily respecting 7 AM boundary.

    For contractual day D: aggregate (D-1) 07:00 through D 06:59

    Example: For gas consumption date 2025-01-02, aggregate hourly data
    from 2025-01-01 07:00:00 to 2025-01-02 06:59:59 (24 hours).
    """
    # Create a copy to avoid modifying original
    df_work = hourly_df.copy()

    # Shift index by 7 hours backward to align 7 AM to midnight
    df_work.index = df_work.index - pd.Timedelta(hours=7)

    # Basic aggregations
    daily = df_work.resample('D').agg({
        'temperature': 'mean',
        'humidity': 'mean',
        'wind_speed': 'mean',
        'pressure': 'mean',
        'cloud_cover': 'mean',
        'precipitation': 'sum'
    })

    # Calculate min/max temperatures
    daily['temperature_min'] = df_work['temperature'].resample('D').min()
    daily['temperature_max'] = df_work['temperature'].resample('D').max()
    daily['temp_range'] = daily['temperature_max'] - daily['temperature_min']

    # Advanced hourly features
    daily['temp_hourly_std'] = df_work['temperature'].resample('D').std()

    # Heating degree hours (sum of max(0, 18 - T_hourly) for all 24 hours)
    df_work['hdh_18c'] = np.maximum(0, 18 - df_work['temperature'])
    daily['hdh_18c'] = df_work['hdh_18c'].resample('D').sum()

    df_work['hdh_15c'] = np.maximum(0, 15 - df_work['temperature'])
    daily['hdh_15c'] = df_work['hdh_15c'].resample('D').sum()

    # Hours below freezing
    df_work['below_0c'] = (df_work['temperature'] < 0).astype(int)
    daily['hours_below_0c'] = df_work['below_0c'].resample('D').sum()

    # Wind chill metrics
    df_work['wind_chill'] = df_work['temperature'] - 0.1 * df_work['wind_speed']
    daily['wind_chill_mean'] = df_work['wind_chill'].resample('D').mean()
    daily['wind_chill_min'] = df_work['wind_chill'].resample('D').min()

    # Shift index back forward 7 hours to restore contractual day date
    daily.index = daily.index + pd.Timedelta(hours=7)
    daily.index.name = 'date'

    return daily


print("\n" + "="*80)
print("Створення добової агрегації з контрактною добою (7:00-7:00)")
print("="*80)

hourly_df = pd.read_csv(out_path, parse_dates=['date'], index_col='date')
daily_contractual = aggregate_hourly_to_contractual_daily(hourly_df)

contractual_path = DATA_DIR / "weather_2025_daily_contractual.csv"
daily_contractual.to_csv(contractual_path)

print(f"\nСтворено файл з добовою агрегацією (7:00-7:00): {contractual_path}")
print(f"Записів: {len(daily_contractual)}")
print(f"Діапазон дат: {daily_contractual.index.min().date()} .. {daily_contractual.index.max().date()}")
print(f"\nКолонки в агрегованому файлі:")
for col in daily_contractual.columns:
    print(f"  - {col}")
print(f"\nСтатистика нових фіч:")
print(f"  temp_hourly_std: mean={daily_contractual['temp_hourly_std'].mean():.2f}, "
      f"max={daily_contractual['temp_hourly_std'].max():.2f}")
print(f"  hdh_18c: mean={daily_contractual['hdh_18c'].mean():.1f}, "
      f"max={daily_contractual['hdh_18c'].max():.1f}")
print(f"  hours_below_0c: mean={daily_contractual['hours_below_0c'].mean():.1f}, "
      f"max={daily_contractual['hours_below_0c'].max():.0f}")
print(f"  wind_chill_min: mean={daily_contractual['wind_chill_min'].mean():.1f}°C, "
      f"min={daily_contractual['wind_chill_min'].min():.1f}°C")
