"""When a poll is due, said in cron.

The hours used to be a list — "08:00", "20:00" — which reads well for a site
polled twice a day and badly for everything else: asking for an hourly poll
meant ticking twenty-four boxes, and asking for "every four hours" meant
counting them out by hand. A cron expression says both in five characters and
is a thing operators already recognise from every other scheduler.

Five fields, as everywhere else: minute, hour, day of month, month, day of
week. `*`, lists, ranges and steps; `7` and `0` are both Sunday. Seconds are
not a field — a poll is a phone call that takes minutes.

What the poller needs is not the usual question. A scheduler asks "when is the
next one"; this asks **"which slot has already passed"**, because a device is
due when the last slot that arrived is later than its last successful poll.
That is what survives an agent being switched off: nothing is queued, so
nothing is lost — the arithmetic simply still holds an hour later.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

#: How far back a search for the last slot goes. Three days covers a cron that
#: only fires on a weekday or a day of the month that has just passed; beyond
#: that the answer stops mattering — a device unpolled for three days is
#: overdue on any reading.
LOOKBACK_HOURS = 24 * 3

#: The fields, in order, with the range each one accepts.
FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)

#: Names people write instead of numbers, in both alphabets they write them in.
NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

_PART = re.compile(r"^(\*|\d+|[a-z]{3})(?:-(\d+|[a-z]{3}))?(?:/(\d+))?$")


class CronError(ValueError):
    """An expression an operator has to fix, with a message that says how."""


def parse(expression: str) -> Dict[str, Set[int]]:
    """The expression as the set of values each field allows.

    Refused rather than guessed at: a cron nobody checked is a schedule that
    silently never fires, which looks exactly like a modem that never answers.
    """
    if not expression or not str(expression).strip():
        raise CronError("Порожній вираз розкладу")
    fields = str(expression).strip().lower().split()
    if len(fields) != len(FIELDS):
        raise CronError(
            "Розклад має п'ять полів: хвилина, година, день, місяць, день тижня "
            f"(отримано {len(fields)})"
        )

    allowed: Dict[str, Set[int]] = {}
    for value, (name, low, high) in zip(fields, FIELDS):
        allowed[name] = _field(value, name, low, high)
    # Sunday is written both ways, and both have to mean the same day.
    if 7 in allowed["weekday"]:
        allowed["weekday"] = (allowed["weekday"] - {7}) | {0}
    return allowed


def _field(value: str, name: str, low: int, high: int) -> Set[int]:
    found: Set[int] = set()
    for part in value.split(","):
        match = _PART.match(part)
        if not match:
            raise CronError(f"Не розумію «{part}» у полі «{name}»")
        start, end, step = match.groups()
        step = int(step) if step else 1
        if step < 1:
            raise CronError(f"Крок має бути додатним: «{part}»")
        if start == "*":
            first, last = low, high
        else:
            first = _number(start, name, low, high)
            last = _number(end, name, low, high) if end else (
                high if step > 1 else first
            )
        if last < first:
            raise CronError(f"Діапазон навпаки: «{part}» у полі «{name}»")
        found.update(range(first, last + 1, step))
    if not found:
        raise CronError(f"Поле «{name}» нічого не дозволяє")
    return found


def _number(token: str, name: str, low: int, high: int) -> int:
    value = NAMES.get(token, None)
    if value is None:
        if not token.isdigit():
            raise CronError(f"Не розумію «{token}» у полі «{name}»")
        value = int(token)
    if not low <= value <= high:
        raise CronError(f"«{token}» поза межами поля «{name}» ({low}–{high})")
    return value


def matches(allowed: Dict[str, Set[int]], when: datetime) -> bool:
    """Whether a minute is one the expression names.

    Day of month and day of week are OR, not AND, when both are restricted —
    the rule every cron follows and the one that surprises everybody: "1 * *
    mon" means the first of the month *and* every Monday.
    """
    if when.minute not in allowed["minute"] or when.hour not in allowed["hour"]:
        return False
    if when.month not in allowed["month"]:
        return False

    day_any = allowed["day"] == set(range(1, 32))
    weekday_any = allowed["weekday"] == set(range(0, 7))
    # Python counts Monday as 0; cron counts Sunday.
    weekday = (when.weekday() + 1) % 7
    day_hit = when.day in allowed["day"]
    weekday_hit = weekday in allowed["weekday"]
    if day_any and weekday_any:
        return True
    if day_any:
        return weekday_hit
    if weekday_any:
        return day_hit
    return day_hit or weekday_hit


def last_fire(now: datetime, expression: str) -> Optional[datetime]:
    """The most recent moment the expression named, or None within the window.

    Walked back an hour at a time rather than a minute at a time: an hour has
    at most sixty candidates and the field already says which of them count,
    so three days of history cost seventy-two steps instead of four thousand.
    """
    try:
        allowed = parse(expression)
    except CronError:
        return None

    cursor = now.replace(second=0, microsecond=0)
    for step in range(LOOKBACK_HOURS + 1):
        hour = (cursor - timedelta(hours=step)).replace(minute=0)
        ceiling = cursor.minute if step == 0 else 59
        for minute in sorted((m for m in allowed["minute"] if m <= ceiling),
                             reverse=True):
            candidate = hour.replace(minute=minute)
            if matches(allowed, candidate):
                return candidate
    return None


def describe(expression: str) -> str:
    """The expression in words, for the line under the field.

    Only the shapes a poll actually uses are spelled out; anything else is
    named as what it is rather than described wrongly.
    """
    try:
        allowed = parse(expression)
    except CronError as error:
        return str(error)

    minutes = sorted(allowed["minute"])
    hours = sorted(allowed["hour"])
    every_day = (allowed["day"] == set(range(1, 32))
                 and allowed["month"] == set(range(1, 13))
                 and allowed["weekday"] == set(range(0, 7)))
    if not every_day:
        return "за власним розкладом"

    if len(minutes) == 1 and len(hours) == 24:
        at = "щогодини" if minutes[0] == 0 else f"щогодини о :{minutes[0]:02d}"
        return at
    if len(minutes) == 1 and len(hours) > 1:
        # "Every N hours" only when the hours actually tile the day from
        # midnight: 8 and 20 are twelve apart but they are two fixed hours,
        # and calling that "every 12 hours" would promise a slot at 08:00
        # tomorrow to somebody who reads it at 21:00.
        step = hours[1] - hours[0]
        if step and hours == list(range(0, 24, step)):
            return f"кожні {step} год"
        return "о " + ", ".join(f"{h:02d}:{minutes[0]:02d}" for h in hours)
    if len(minutes) == 1 and len(hours) == 1:
        return f"щодня о {hours[0]:02d}:{minutes[0]:02d}"
    return "за власним розкладом"


def from_times(times: Optional[List[str]]) -> Optional[str]:
    """The old list of "HH:MM" as the cron that means the same thing.

    Kept for the migration and for anything still holding a list: the fleet's
    schedules were whole hours, and those convert exactly.
    """
    hours: Set[int] = set()
    minutes: Set[int] = set()
    for value in times or []:
        try:
            hour, minute = (int(part) for part in str(value).split(":"))
        except (ValueError, TypeError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            hours.add(hour)
            minutes.add(minute)
    if not hours:
        return None
    return (",".join(str(m) for m in sorted(minutes)) + " "
            + ",".join(str(h) for h in sorted(hours)) + " * * *")
