"""
Тестовый скрипт для получения температурных данных по городу Запорожье
через библиотеку Meteostat.

Станция: 34601 (Zaporizhzhia, UA)
"""

from datetime import datetime, timedelta

from meteostat import daily, hourly, stations, Point

# ID метеостанции Запорожья (WMO 34601)
STATION_ID = "34601"

# --- Информация о станции ---
print("=" * 60)
print("Метеостанция Запорожья")
print("=" * 60)

loc = Point(47.8388, 35.1396)
nearby = stations.nearby(loc, radius=50000, limit=5)
print(nearby[["name", "latitude", "longitude", "elevation", "distance"]].to_string())

# --- Суточные данные за последний месяц ---
end = datetime.now()
start = end - timedelta(days=30)

print()
print("=" * 60)
print(f"Суточные температуры ({start.date()} — {end.date()})")
print("=" * 60)

daily_ts = daily(STATION_ID, start, end)

if daily_ts.empty:
    print("Нет суточных данных за указанный период.")
else:
    df = daily_ts.fetch()
    temp_cols = ["temp", "tmin", "tmax"]
    available = [c for c in temp_cols if c in df.columns]
    print(df[available].to_string())
    print()
    print("Статистика:")
    print(df[available].describe().round(1).to_string())

# --- Почасовые данные за последние 2 суток ---
hourly_end = datetime.now()
hourly_start = hourly_end - timedelta(days=2)

print()
print("=" * 60)
print(f"Почасовые температуры ({hourly_start.date()} — {hourly_end.date()})")
print("=" * 60)

hourly_ts = hourly(STATION_ID, hourly_start, hourly_end)

if hourly_ts.empty:
    print("Нет почасовых данных за указанный период.")
else:
    df = hourly_ts.fetch()
    temp_col = "temp" if "temp" in df.columns else None
    if temp_col:
        print(df[[temp_col]].to_string())
    else:
        print(df.to_string())
