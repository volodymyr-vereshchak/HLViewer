"""Parsing and slot arithmetic of the admin-configurable DPD refresh schedule."""

from datetime import datetime

from backend.hl_engine.scheduler_runner import _last_due_refresh
from backend.services.dpd_archive_refresh import parse_refresh_times


class TestParseRefreshTimes:
    def test_normalizes_sorts_and_dedupes(self):
        assert parse_refresh_times(["16:00", "9:5", "16:00"]) == ["09:05", "16:00"]

    def test_accepts_comma_separated_string(self):
        assert parse_refresh_times("10:00,16:00") == ["10:00", "16:00"]

    def test_drops_unparseable_entries(self):
        assert parse_refresh_times(["25:00", "10:70", "abc", "12", ""]) == []
        assert parse_refresh_times(None) == []


class TestLastDueRefresh:
    TIMES = ["08:00", "12:00", "20:00"]

    def test_picks_the_latest_passed_slot(self):
        now = datetime(2026, 7, 30, 13, 5)
        assert _last_due_refresh(now, self.TIMES) == datetime(2026, 7, 30, 12, 0)

    def test_before_first_slot_falls_back_to_yesterday(self):
        now = datetime(2026, 7, 30, 3, 0)
        assert _last_due_refresh(now, self.TIMES) == datetime(2026, 7, 29, 20, 0)

    def test_empty_schedule_has_no_due_moment(self):
        assert _last_due_refresh(datetime(2026, 7, 30, 13, 5), []) is None
