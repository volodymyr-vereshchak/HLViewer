"""Folding DPD's alarm rows into the enterprise accidents report.

Every case below is a shape taken from live DPD data (branch 9, a week in
September 2026), because the arithmetic is only obvious once the shapes are:
a continuous alarm is sliced into buckets, the occurrence counter is zero
while it merely continues, and a handful of rows carry a sentinel end date
whose duration dwarfs every real row put together.
"""

from datetime import datetime

from backend.services.enterprise_events import (
    _series, _within, aggregate, event_names,
)


def row(start, end="", duration=0, retry=0):
    return {"start": start, "end": end, "duration": duration, "retry": retry}


def test_buckets_of_one_alarm_sum_rather_than_count():
    """An hourly-sliced alarm is one appearance, not one per bucket.

    This is device 190742's shape: 175 rows of 3599s across a week, all with
    retry=0 because the alarm never stopped to be raised again.
    """
    rows = [
        row("2026-09-04 00:00:00", "2026-09-04 00:59:00", 3599),
        row("2026-09-04 01:00:00", "2026-09-04 01:59:00", 3599),
        row("2026-09-04 02:00:00", "2026-09-04 02:59:00", 3599),
    ]
    s = _series(rows)
    assert s["duration"] == 3 * 3599
    # Not 3, and — the point of the fallback — not 0 either.
    assert s["appearances"] == 1
    assert s["first"] == "2026-09-04 00:00:00"
    assert s["last"] == "2026-09-04 02:59:00"


def test_retry_is_taken_as_the_occurrence_count():
    """Device 5765: daily buckets, a few dozen raises inside each."""
    rows = [
        row("2026-08-29 07:10:00", "2026-08-30 06:49:00", 960, 53),
        row("2026-08-30 07:07:00", "2026-08-31 06:59:00", 840, 49),
    ]
    s = _series(rows)
    assert s["appearances"] == 102
    # duration is the time IN alarm, not the span of the buckets holding it.
    assert s["duration"] == 1800


def test_sentinel_end_rows_are_dropped_not_clamped():
    """`end` = 2000-01-01 makes duration ~593 days and swamps everything.

    Twelve such rows outweighed 934 real ones by 200x in the sample, so they
    are removed from both totals and counted separately.
    """
    rows = [
        row("2026-08-29 08:57:21", "2000-01-01 00:00:00", 51228094),
        row("2026-08-29 09:00:00", "2026-08-29 09:10:00", 600, 1),
    ]
    s = _series(rows)
    assert s["dropped"] == 1
    assert s["duration"] == 600
    assert s["appearances"] == 1
    assert s["first"] == "2026-08-29 09:00:00"


def test_open_row_is_bounded_by_its_start():
    """A row without an end is the "could not decode" shape: start + duration
    would put the last-seen time in the future for a still-running alarm."""
    s = _series([row("2026-09-04 00:25:09", "", 167, 1)])
    assert s["last"] == "2026-09-04 00:25:09"
    assert s["duration"] == 167


def test_all_rows_dropped_yields_nothing():
    assert _series([row("2026-08-29 08:57:21", "2000-01-01 00:00:00", 5122809)]) is None


def test_groups_sum_over_objects_and_sort_by_pain():
    per_device = [
        {"type": "industry.acc.rgk.15", "code": 15, "first": "2026-09-01 00:00:00",
         "last": "2026-09-02 00:00:00", "duration": 3600, "appearances": 1,
         "enterprise_id": 1, "enterprise_name": "A", "line_id": None,
         "serNum": 1, "chNum": 0},
        {"type": "industry.acc.rgk.15", "code": 15, "first": "2026-08-30 00:00:00",
         "last": "2026-09-03 00:00:00", "duration": 7200, "appearances": 2,
         "enterprise_id": 2, "enterprise_name": "B", "line_id": None,
         "serNum": 2, "chNum": 0},
        {"type": "industry.acc.radmirtech.2", "code": 2, "first": "2026-09-01 00:00:00",
         "last": "2026-09-01 01:00:00", "duration": 60, "appearances": 9,
         "enterprise_id": 3, "enterprise_name": "C", "line_id": None,
         "serNum": 3, "chNum": 0},
    ]
    groups = aggregate(per_device)
    assert [g["type"] for g in groups] == [
        "industry.acc.rgk.15", "industry.acc.radmirtech.2",
    ]
    top = groups[0]
    assert top["duration"] == 10800
    assert top["appearances"] == 3
    assert top["devices"] == 2
    # The window is min/max over objects; it is NOT the duration.
    assert top["first"] == "2026-08-30 00:00:00"
    assert top["last"] == "2026-09-03 00:00:00"
    # Objects are ordered by their own outage, worst first.
    assert [o["enterprise_name"] for o in top["objects"]] == ["B", "A"]


def test_unknown_key_falls_through_untranslated():
    groups = aggregate([
        {"type": "industry.acc.nosuchvendor.99", "code": 99,
         "first": "2026-09-01 00:00:00", "last": "2026-09-01 01:00:00",
         "duration": 60, "appearances": 1, "enterprise_id": 1,
         "enterprise_name": "A", "line_id": None, "serNum": 1, "chNum": 0},
    ])
    assert groups[0]["translated"] is False
    # Showing the raw key beats showing nothing: it says what to add.
    assert groups[0]["name"] == "industry.acc.nosuchvendor.99"


def test_dictionary_covers_the_keys_live_data_produced():
    names = event_names()
    for key in (
        "industry.acc.error",
        "industry.acc.rgk.15",
        "industry.acc.radmirtech.2",
        "industry.acc.grempis.221",
        "industry.interv.error",
    ):
        assert names.get(key), key


def test_replaced_corrector_keeps_only_its_own_days():
    """One enterprise, two correctors: 01–03 and 03–05 of the month.

    Both devices are polled over the whole request window (one call each),
    then each keeps only the rows that started while IT was installed — so
    neither lends the enterprise the other's alarms.
    """
    first = (datetime(2026, 9, 1), datetime(2026, 9, 2, 23, 59, 59))
    second = (datetime(2026, 9, 3), datetime(2026, 9, 5, 23, 59, 59))

    assert _within("2026-09-01 10:00:00", first) is True
    assert _within("2026-09-04 10:00:00", first) is False
    assert _within("2026-09-04 10:00:00", second) is True
    assert _within("2026-09-01 10:00:00", second) is False

    # An alarm is attributed by its START, so one that began under the old
    # corrector stays with it even if its bucket runs into the new window.
    assert _within("2026-09-02 23:00:00", first) is True
    assert _within("2026-09-02 23:00:00", second) is False


def test_row_without_a_usable_start_is_not_attributed():
    span = (datetime(2026, 9, 1), datetime(2026, 9, 5))
    assert _within("", span) is False
    assert _within(None, span) is False
    assert _within("not a date", span) is False
