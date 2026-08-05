"""Device-history windows: the rules that decide which corrector a record
belongs to. Pure functions, no DB."""

from datetime import datetime

import pytest

from backend.services.device_history import (
    HistoryError,
    attribution_stamp,
    clip,
    covers,
    find_device_clashes,
    resolve_windows,
    validate_point,
)


def dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 3, day, hour)


def entry(day: int, hour: int = 0, removed=None, device=1) -> dict:
    return {"installed_from": dt(day, hour), "removed_at": removed,
            "device": device}


class TestResolveWindows:
    def test_single_entry_is_open_ended(self):
        e = entry(1)
        assert resolve_windows([e]) == [(e, dt(1), None)]

    def test_chains_to_the_next_install(self):
        first, second = entry(1), entry(10)
        windows = resolve_windows([second, first])  # unordered input
        assert windows == [(first, dt(1), dt(10)), (second, dt(10), None)]

    def test_removed_at_leaves_a_gap(self):
        """Taken off on the 5th, replaced on the 10th: those five days belong
        to nobody. The old corrector is already measuring another point."""
        first, second = entry(1, removed=dt(5)), entry(10)
        windows = resolve_windows([first, second])
        assert windows[0] == (first, dt(1), dt(5))
        assert windows[1] == (second, dt(10), None)

    def test_removed_at_after_the_next_install_is_ignored(self):
        """A removal later than the replacement would overlap the next window;
        the chain wins so the two can never both be in force."""
        first, second = entry(1, removed=dt(20)), entry(10)
        assert resolve_windows([first, second])[0][2] == dt(10)

    def test_trailing_removed_at_closes_the_history(self):
        e = entry(1, removed=dt(5))
        assert resolve_windows([e]) == [(e, dt(1), dt(5))]

    def test_reads_orm_style_objects(self):
        class Row:
            def __init__(self, d):
                self.installed_from = dt(d)
                self.removed_at = None

        rows = [Row(10), Row(1)]
        assert [w[1] for w in resolve_windows(rows)] == [dt(1), dt(10)]


class TestAttributionStamp:
    def test_hourly_uses_the_exact_stamp(self):
        assert attribution_stamp(dt(5, 14), "hourly") == dt(5, 14)

    def test_daily_shifts_to_the_commercial_day_start(self):
        """A day belongs to the device in force when the commercial day
        opened, not at midnight."""
        assert attribution_stamp(dt(5), "daily") == dt(5, 7)


class TestCovers:
    def test_inclusive_start_exclusive_end(self):
        assert covers(dt(1), dt(5), dt(1)) is True
        assert covers(dt(1), dt(5), dt(5)) is False
        assert covers(dt(1), None, dt(99 % 28 + 1)) is True

    def test_before_the_window(self):
        assert covers(dt(5), None, dt(1)) is False


class TestClip:
    def test_open_window_clips_to_the_request(self):
        assert clip(dt(1), None, dt(5), dt(10)) == (dt(5), dt(10))

    def test_window_narrower_than_the_request(self):
        start, end = clip(dt(5), dt(8), dt(1), dt(20))
        assert start == dt(5)
        # win_to is exclusive, so the clipped end stops just short of it.
        assert end < dt(8)

    def test_no_overlap_returns_none(self):
        assert clip(dt(1), dt(5), dt(10), dt(20)) is None
        assert clip(dt(10), None, dt(1), dt(5)) is None

    def test_window_ending_exactly_at_the_request_start_contributes_nothing(self):
        assert clip(dt(1), dt(5), dt(5), dt(9)) is None


class TestValidatePoint:
    def test_chained_history_is_valid(self):
        validate_point([entry(1), entry(10)])

    def test_gap_history_is_valid(self):
        validate_point([entry(1, removed=dt(5)), entry(10)])

    def test_same_install_moment_overlaps(self):
        with pytest.raises(HistoryError):
            validate_point([entry(1), entry(1)])


class TestFindDeviceClashes:
    def test_moved_corrector_without_overlap_is_fine(self):
        """#7 leaves point A on the 5th and turns up at B on the 10th."""
        a = [(entry(1, removed=dt(5), device=7), dt(1), dt(5))]
        b = [(entry(10, device=7), dt(10), None)]
        assert find_device_clashes({"A": a, "B": b}, lambda e: e["device"]) == []

    def test_same_device_at_two_points_at_once_is_a_clash(self):
        """Without an explicit removal at A, #7 is still open-ended there when
        it is installed at B — its gas would be counted twice."""
        a = [(entry(1, device=7), dt(1), None)]
        b = [(entry(10, device=7), dt(10), None)]
        clashes = find_device_clashes({"A": a, "B": b}, lambda e: e["device"])
        assert clashes == [(7, "A", "B")]

    def test_different_devices_never_clash(self):
        a = [(entry(1, device=7), dt(1), None)]
        b = [(entry(1, device=8), dt(1), None)]
        assert find_device_clashes({"A": a, "B": b}, lambda e: e["device"]) == []

    def test_successive_entries_within_one_point_are_not_a_clash(self):
        """The same corrector removed and re-installed at the SAME point."""
        windows = resolve_windows(
            [entry(1, removed=dt(5), device=7), entry(10, device=7)]
        )
        assert find_device_clashes({"A": windows}, lambda e: e["device"]) == []
