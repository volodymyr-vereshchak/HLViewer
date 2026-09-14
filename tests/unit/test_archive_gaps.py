"""What a poll should ask for — the arithmetic, without a database."""
from datetime import date, datetime, timedelta

from backend.services.archive_gaps import (
    Window, as_ranges, missing_days, missing_hours,
)


def test_a_hole_in_the_middle_is_found():
    """The case this exists for: DPD fetched a month over somebody else's
    internet and lost an afternoon. Polling "everything after the newest row"
    would never come back for it."""
    window = Window(datetime(2026, 9, 1, 0), datetime(2026, 9, 1, 5))
    present = [datetime(2026, 9, 1, h) for h in (0, 1, 4, 5)]
    assert missing_hours(window, present) == [
        datetime(2026, 9, 1, 2), datetime(2026, 9, 1, 3),
    ]


def test_a_reading_filed_mid_hour_still_counts_as_that_hour():
    """Records come back stamped 09:00:32. Comparing exact moments would
    report every hour as missing and re-read the whole ring."""
    window = Window(datetime(2026, 9, 1, 9), datetime(2026, 9, 1, 9))
    assert missing_hours(window, [datetime(2026, 9, 1, 9, 0, 32)]) == []


def test_an_empty_archive_asks_for_the_whole_window():
    window = Window(datetime(2026, 9, 1, 0), datetime(2026, 9, 1, 3))
    assert len(missing_hours(window, [])) == 4


def test_nothing_is_asked_for_outside_the_window():
    """A gap older than the corrector's ring is gone from the meter too."""
    window = Window(datetime(2026, 9, 10, 0), datetime(2026, 9, 10, 2))
    gaps = missing_hours(window, [])
    assert min(gaps) == datetime(2026, 9, 10, 0)
    assert max(gaps) == datetime(2026, 9, 10, 2)


def test_a_part_day_at_the_edge_is_not_a_missing_day():
    """The day a window opens halfway through was never going to be complete.

    Asking for it every poll would be a gap that never closes and a line in
    the log that never stops appearing.
    """
    window = Window(datetime(2026, 9, 1, 13), datetime(2026, 9, 4, 9))
    assert missing_days(window, []) == [date(2026, 9, 2), date(2026, 9, 3)]


def test_consecutive_holes_are_one_range():
    hours = [datetime(2026, 9, 1, h) for h in (2, 3, 4, 9)]
    ranges = as_ranges(hours)
    assert [(r.start.hour, r.end.hour) for r in ranges] == [(2, 4), (9, 9)]


def test_a_window_counts_its_own_hours():
    assert Window(datetime(2026, 9, 1, 0), datetime(2026, 9, 1, 0)).hours() == 1
    assert Window(datetime(2026, 9, 1, 0), datetime(2026, 9, 2, 0)).hours() == 25
    # A window that ends before it starts is not a window.
    assert Window(datetime(2026, 9, 2), datetime(2026, 9, 1)).hours() == 0


def test_a_dpd_poll_starts_where_the_archive_ends():
    from backend.services.archive_gaps import dpd_window

    window = dpd_window(
        newest_stored=datetime(2026, 9, 10, 6),
        installed_from=datetime(2026, 1, 1),
        today=date(2026, 9, 12),
    )
    assert window.start == date(2026, 9, 10)
    assert window.reason == "archive"


def test_a_dpd_poll_ends_tomorrow_because_the_day_is_a_gas_day():
    """The current gas day's hours are filed under a date that has not
    arrived yet. Ending at today leaves them behind on every poll."""
    from backend.services.archive_gaps import dpd_window

    window = dpd_window(None, datetime(2026, 9, 1), today=date(2026, 9, 12))
    assert window.end == date(2026, 9, 13)


def test_an_empty_archive_starts_at_the_installation():
    from backend.services.archive_gaps import dpd_window

    window = dpd_window(None, datetime(2026, 3, 15, 7), today=date(2026, 9, 12))
    assert window.start == date(2026, 3, 15)
    assert window.reason == "installed"


def test_with_neither_it_asks_about_today_rather_than_about_history():
    from backend.services.archive_gaps import dpd_window

    window = dpd_window(None, None, today=date(2026, 9, 12))
    assert (window.start, window.reason) == (date(2026, 9, 12), "unknown")
