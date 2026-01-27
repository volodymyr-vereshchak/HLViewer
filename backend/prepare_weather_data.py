"""
Підготовка файлу погодних умов з Meteostat для аналізу споживання газу.

Станція: 34601 (Запоріжжя, UA)
Дані: суточні температури, вологість, тиск, вітер, хмарність (2025-01 — 2026-01).
Вихідний файл: backend/data/weather_2025.csv
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
from meteostat import daily

STATION_ID = "34601"
START = datetime(2025, 1, 1)
END = datetime(2026, 1, 31)

DATA_DIR = Path(__file__).parent / "data"

print(f"Завантаження даних зі станції {STATION_ID} ({START.date()} — {END.date()})...")

ts = daily(STATION_ID, START, END)

if ts.empty:
    raise RuntimeError(f"Немає даних для станції {STATION_ID}")

df = ts.fetch()

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

out_path = DATA_DIR / "weather_2025.csv"
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
