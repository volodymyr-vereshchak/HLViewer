"""Synthetic 1С exports and weather for the GRS consumption model tests.

Four stations whose remainder (inflow minus non-household) follows the
temperature, a year and a half of days, and one station that over-reads by
60 % for three weeks of the second winter — the thing the detector is for.
"""
import io
import math
import random
import zipfile
from datetime import date, timedelta

import openpyxl

FIRST = date(2025, 1, 1)
LAST = date(2026, 6, 30)
STATIONS = {"ГРС Альфа": 1.0, "ГРС Бета": 0.6, "ГРС Гамма": 2.5, "ГРС Дельта": 1.4}
FAULTY = "ГРС Дельта"
FAULT = (date(2026, 2, 1), date(2026, 2, 21))
#: A village that empties: from this day on, 8 % fewer households a month.
EMPTYING = "ГРС Бета"
EMPTYING_FROM = date(2025, 11, 1)
INDUSTRY = "Промисловість"
#: Non-household consumption of every station, m³ a day.
INDUSTRY_VOLUME = 500.0


def days(first: date = FIRST, last: date = LAST) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def weather(first: date = FIRST, last: date = LAST) -> list[dict]:
    """Daily weather with a season in it and some day-to-day wander."""
    rng = random.Random(7)
    rows, wander = [], 0.0
    for day in days(first, last):
        season = math.cos(2 * math.pi * (day.timetuple().tm_yday - 15) / 365)
        wander = 0.7 * wander + rng.gauss(0, 2.0)
        t = 9.0 - 13.0 * season + wander
        rows.append({
            "day": day, "t_mean": round(t, 2), "t_min": round(t - 4, 2), "t_max": round(t + 4, 2),
            "precipitation": 0.0, "wind_max": round(4 + rng.random() * 3, 2),
            "radiation": round(12 - 9 * season + rng.random(), 2),
        })
    return rows


def remainder(rows: list[dict], emptying: bool = False) -> dict[str, dict[date, float]]:
    """What households of each station burn, m³ a day. With `emptying`,
    EMPTYING loses households month by month from EMPTYING_FROM."""
    rng = random.Random(11)
    out: dict[str, dict[date, float]] = {name: {} for name in STATIONS}
    for row in rows:
        base = 2.0 + 0.9 * max(15.5 - row["t_mean"], 0.0)
        for name, scale in STATIONS.items():
            volume = 1000 * scale * base * (1 + rng.gauss(0, 0.03))
            if name == FAULTY and FAULT[0] <= row["day"] <= FAULT[1]:
                volume *= 1.6
            if emptying and name == EMPTYING and row["day"] >= EMPTYING_FROM:
                volume *= 0.92 ** ((row["day"] - EMPTYING_FROM).days / 30)
            out[name][row["day"]] = round(volume, 2)
    return out


def _book(title: str, first_day_column: int, period: list[date]):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.cell(1, 2, title)
    sheet.cell(2, 2, f"Період: {period[0]:%d.%m.%Y} - {period[-1]:%d.%m.%Y}")
    for i, day in enumerate(period):
        sheet.cell(5, first_day_column + i, f"{day:%d.%m.%Y}")
    return book, sheet


def _bytes(book) -> bytes:
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def inflow_xlsx(values: dict[str, dict[date, float]], period: list[date]) -> bytes:
    """«Фактичний баланс газу по ГРС»: a row per GRS, a column per day."""
    book, sheet = _book("Фактичний баланс газу по ГРС (подобовий)", 3, period)
    sheet.cell(5, 2, "ГРС")
    row = 6
    for name, by_day in values.items():
        sheet.cell(row, 2, name)
        for i, day in enumerate(period):
            if by_day.get(day):
                sheet.cell(row, 3 + i, by_day[day])
        row += 1
    sheet.cell(row, 2, "Підсумок")
    for i, day in enumerate(period):
        sheet.cell(row, 3 + i, sum(v.get(day, 0) for v in values.values()))
    return _bytes(book)


def consumption_xlsx(values: dict[str, dict[str, dict[date, float]]], period: list[date]) -> bytes:
    """«Фактичне споживання газу подобове» by GRS and category:
    values[grs][category][day]."""
    book, sheet = _book("Фактичне споживання газу подобове", 3, period)
    sheet.cell(5, 2, "ГРС / Категорія")
    row = 6
    for name, categories in values.items():
        sheet.cell(row, 2, name)
        for i, day in enumerate(period):
            total = sum(v.get(day, 0) for v in categories.values())
            if total:
                sheet.cell(row, 3 + i, total)
        row += 1
        for category, by_day in categories.items():
            sheet.cell(row, 2, category)
            sheet.row_dimensions[row].outline_level = 1
            for i, day in enumerate(period):
                if by_day.get(day):
                    sheet.cell(row, 3 + i, by_day[day])
            row += 1
    sheet.cell(row, 2, "Підсумок")
    return _bytes(book)


def object_xlsx(objects: list[tuple[str, str, str, dict[date, float]]], period: list[date]) -> bytes:
    """The same report by organisation › category › object:
    objects = [(category, object name, GRS, by day)]."""
    book, sheet = _book("Фактичне споживання газу подобове", 5, period)
    sheet.cell(5, 2, "Організація / Категорія / Обʼєкт")
    sheet.cell(5, 4, "ГРС")
    sheet.cell(6, 2, "ТОВ ГРМУ")
    for i, day in enumerate(period):
        sheet.cell(6, 5 + i, sum(o[3].get(day, 0) for o in objects))
    sheet.cell(7, 2, "Філія")
    sheet.row_dimensions[7].outline_level = 1
    row, category = 8, None
    for name, title, grs, by_day in sorted(objects, key=lambda o: o[0]):
        if name != category:
            category = name
            sheet.cell(row, 2, category)
            sheet.row_dimensions[row].outline_level = 2
            row += 1
        sheet.cell(row, 2, title)
        sheet.cell(row, 4, grs)
        sheet.row_dimensions[row].outline_level = 3
        for i, day in enumerate(period):
            if by_day.get(day):
                sheet.cell(row, 5 + i, by_day[day])
        row += 1
    return _bytes(book)


def as_1c_writes_it(content: bytes) -> bytes:
    """Rename the shared strings part the way 1С does (a capital S), which
    openpyxl cannot open."""
    source = zipfile.ZipFile(io.BytesIO(content))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr("xl/SharedStrings.xml" if name == "xl/sharedStrings.xml" else name,
                            source.read(name))
    return out.getvalue()


def reports(first: date = FIRST, last: date = LAST) -> tuple[bytes, bytes]:
    """(inflow, consumption) exports of the synthetic branch over a period."""
    period = days(first, last)
    households = remainder(weather(FIRST, LAST))
    inflow = {name: {d: households[name][d] + INDUSTRY_VOLUME for d in period} for name in STATIONS}
    consumption = {name: {INDUSTRY: {d: INDUSTRY_VOLUME for d in period}} for name in STATIONS}
    return inflow_xlsx(inflow, period), consumption_xlsx(consumption, period)
