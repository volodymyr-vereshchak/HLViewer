"""Commercial-day arithmetic with an EXCLUSIVE end.

Gas is accounted in commercial days opening at CONTRACT_HOUR (7 by default),
not at midnight. Everything here works in naive Europe/Kyiv time, like the rest
of the database.

Not to be confused with `enterprise_volume_service.request_window`, which
returns an INCLUSIVE last hour (`+1 day, CONTRACT_HOUR - 1h`) because DPD takes
inclusive bounds. Here the end is exclusive throughout, which is what a
half-open time-weighted integral needs. The two are not interchangeable.
"""

from datetime import date, datetime, timedelta

from backend.settings import backend_settings


def contract_hour() -> int:
    return backend_settings.get("CONTRACT_HOUR", 7)


def day_bounds(day: date, hour: int) -> tuple[datetime, datetime]:
    """[day hour:00, day+1 hour:00) — the instants of one commercial day."""
    start = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour)
    return start, start + timedelta(days=1)


def day_of(stamp: datetime, hour: int) -> date:
    """The commercial day a stamp falls in: before CONTRACT_HOUR it still
    belongs to the previous one."""
    if stamp.hour < hour:
        return (stamp - timedelta(days=1)).date()
    return stamp.date()


def range_window(date_from: date, date_to: date, hour: int) -> tuple[datetime, datetime]:
    """[date_from hour:00, date_to+1 hour:00) — the instants the requested
    commercial days span. End EXCLUSIVE."""
    start, _ = day_bounds(date_from, hour)
    _, end = day_bounds(date_to, hour)
    return start, end


def hours_of_day(day: date, hour: int) -> list[datetime]:
    """The 24 hour-starts of one commercial day.

    Always 24 entries even across a DST switch: these are the *slots* the
    report asks about. Whether a slot has data is decided by the series, and a
    slot that never existed locally (spring forward) simply stays empty.
    """
    start, _ = day_bounds(day, hour)
    return [start + timedelta(hours=i) for i in range(24)]


def days_in_range(date_from: date, date_to: date) -> list[date]:
    out: list[date] = []
    day = date_from
    while day <= date_to:
        out.append(day)
        day += timedelta(days=1)
    return out
