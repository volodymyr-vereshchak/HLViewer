"""Reading the install date out of an uploaded workbook.

`_parse_installed_from` sets the moment a corrector took over a metering
point, and that moment is what every later read is filtered against — get it
wrong by a few hours and a day of gas moves from one corrector to another.
The module (691 lines, and the only way enterprises are created in bulk) had
no test at all.

Two behaviours carry the weight and neither is obvious from the signature: an
empty date means "since forever", and an empty hour means the start of the
commercial day rather than midnight.
"""

from datetime import date, datetime

import pytest

from backend.db.models.enterprise_model import EPOCH_INSTALLED_FROM
from backend.services.enterprise_excel import _parse_installed_from
from backend.settings import backend_settings

CONTRACT_HOUR = backend_settings.get("CONTRACT_HOUR", 7)


def ok(date_cell, hour_cell=None) -> datetime:
    parsed, error = _parse_installed_from(date_cell, hour_cell)
    assert error is None, error
    return parsed


class TestEmptyDate:
    """No date means the point's whole archive belongs to this corrector —
    how every row behaved before the history existed, which is what keeps
    older workbooks importing unchanged."""

    @pytest.mark.parametrize("cell", [None, "", "   "])
    def test_means_since_forever(self, cell):
        assert ok(cell) == EPOCH_INSTALLED_FROM

    def test_an_hour_alone_does_not_invent_a_date(self):
        assert ok(None, 9) == EPOCH_INSTALLED_FROM


class TestDefaultHour:
    def test_empty_hour_is_the_commercial_day_start_not_midnight(self):
        """At 00:00 the hours from midnight to CONTRACT_HOUR — which belong to
        the previous commercial day, and so to whoever stood here before —
        would be handed to the new corrector."""
        parsed = ok("02.05.2026")
        assert parsed == datetime(2026, 5, 2, CONTRACT_HOUR)
        assert parsed.hour != 0

    def test_explicit_hour_wins(self):
        assert ok("02.05.2026", 14) == datetime(2026, 5, 2, 14)

    def test_hour_zero_is_honoured_not_treated_as_empty(self):
        """0 is falsy in Python and a plausible answer here; the parser must
        tell it apart from a blank cell."""
        assert ok("02.05.2026", 0) == datetime(2026, 5, 2, 0)

    def test_time_carried_by_the_date_cell_is_used(self):
        assert ok(datetime(2026, 5, 2, 14, 30)) == datetime(2026, 5, 2, 14)

    def test_explicit_hour_overrides_the_date_cell_time(self):
        assert ok(datetime(2026, 5, 2, 14, 30), 9) == datetime(2026, 5, 2, 9)


class TestDateShapes:
    @pytest.mark.parametrize("cell,expected", [
        ("02.05.2026", datetime(2026, 5, 2, CONTRACT_HOUR)),
        ("2026-05-02", datetime(2026, 5, 2, CONTRACT_HOUR)),
        ("02/05/2026", datetime(2026, 5, 2, CONTRACT_HOUR)),
        ("02.05.26", datetime(2026, 5, 2, CONTRACT_HOUR)),
    ])
    def test_accepted_text_formats(self, cell, expected):
        assert ok(cell) == expected

    def test_openpyxl_datetime_cell(self):
        assert ok(datetime(2026, 5, 2)) == datetime(2026, 5, 2, CONTRACT_HOUR)

    def test_openpyxl_date_cell(self):
        assert ok(date(2026, 5, 2)) == datetime(2026, 5, 2, CONTRACT_HOUR)

    def test_a_time_suffix_on_a_text_cell_is_ignored(self):
        assert ok("02.05.2026 13:45") == datetime(2026, 5, 2, CONTRACT_HOUR)

    def test_surrounding_whitespace_is_tolerated(self):
        assert ok("  02.05.2026  ") == datetime(2026, 5, 2, CONTRACT_HOUR)


class TestRejected:
    """A bad cell returns an error for the row rather than a wrong date —
    silently defaulting would move gas between correctors."""

    @pytest.mark.parametrize("cell", ["не дата", "31.02.2026", "2026/05/02", "42"])
    def test_unparseable_date(self, cell):
        parsed, error = _parse_installed_from(cell, None)
        assert parsed is None
        assert "дата встановлення" in error

    @pytest.mark.parametrize("hour", ["дев'ята", "abc"])
    def test_unparseable_hour(self, hour):
        parsed, error = _parse_installed_from("02.05.2026", hour)
        assert parsed is None
        assert "година встановлення" in error

    @pytest.mark.parametrize("hour", [-1, 24, 99])
    def test_hour_out_of_range(self, hour):
        parsed, error = _parse_installed_from("02.05.2026", hour)
        assert parsed is None
        assert "0–23" in error


class TestPrecision:
    def test_truncated_to_the_hour(self):
        """DPD's hourly records land on the hour, so a handover cannot be
        lined up any finer and must not pretend to be."""
        parsed = ok(datetime(2026, 5, 2, 14, 37, 51, 123456))
        assert (parsed.minute, parsed.second, parsed.microsecond) == (0, 0, 0)

    def test_float_hour_from_a_numeric_cell(self):
        """openpyxl hands back numbers as floats."""
        assert ok("02.05.2026", 9.0) == datetime(2026, 5, 2, 9)
