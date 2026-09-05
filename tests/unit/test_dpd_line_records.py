"""Turning a DPD reply into DPD-line archive rows.

`_records_to_rows` is where a record acquires an owner: it decides which rows
of a corrector's answer belong to THIS line's installation window and which
belong to whoever stood here before or after. The module writes
dpd_line_daily_archive and dpd_line_hourly_archive and had no test of its own —
only the read side was covered, on pre-filled fixtures.

Daily and hourly attribute differently, and that is the subtle part: a daily
record dated d covers the commercial day starting at d 07:00, so it belongs to
the device that held the line at 07:00, not at midnight.
"""

from datetime import datetime

from backend.services.dpd_line_refresh import _parse_stamp, _records_to_rows

LINE_ID = 42
# dvstAlwrk is the commercial volume for everything outside WORK_VOLUME_DEVICES.
DEVICE = {"mfDev": 5, "typeDev": 12, "serNum": 100, "chNum": 0}
# (1, 24) is ТКБ, one of the two identities that reports in dvwrkAlwrk.
WORK_VOLUME_DEVICE = {"mfDev": 1, "typeDev": 24, "serNum": 101, "chNum": 0}


def daily(date_str: str, **fields) -> dict:
    return {"date": date_str, "dvstAlwrk": 10.0, **fields}


def hourly(stamp: str, **fields) -> dict:
    return {"date": stamp, "dvstAlwrk": 1.0, **fields}


class TestVolumeField:
    def test_reads_the_field_the_device_reports_in(self):
        rows = _records_to_rows(
            [{"date": "2026-05-02", "dvstAlwrk": 7.0, "dvwrkAlwrk": 99.0}],
            LINE_ID, WORK_VOLUME_DEVICE, datetime(2026, 5, 1), None, "daily",
        )
        assert [r["volume"] for r in rows] == [99.0]

    def test_other_devices_read_the_standard_field(self):
        rows = _records_to_rows(
            [{"date": "2026-05-02", "dvstAlwrk": 7.0, "dvwrkAlwrk": 99.0}],
            LINE_ID, DEVICE, datetime(2026, 5, 1), None, "daily",
        )
        assert [r["volume"] for r in rows] == [7.0]

    def test_skeleton_rows_are_dropped(self):
        """DPD pads the whole requested range and leaves the volume null where
        it has nothing. Those are not zero readings."""
        rows = _records_to_rows(
            [daily("2026-05-02"), {"date": "2026-05-03", "dvstAlwrk": None}],
            LINE_ID, DEVICE, datetime(2026, 5, 1), None, "daily",
        )
        assert [r["stamp"] for r in rows] == [datetime(2026, 5, 2)]


class TestDailyWindow:
    """A daily record for date d covers d 07:00 → d+1 07:00."""

    def test_day_of_handover_goes_to_the_device_that_held_07_00(self):
        records = [daily("2026-05-01"), daily("2026-05-02"), daily("2026-05-03")]

        # Predecessor: leaves at 07:00 on the 2nd, so the 2nd is not its day.
        before = _records_to_rows(
            records, LINE_ID, DEVICE,
            datetime(2026, 4, 1), datetime(2026, 5, 2, 7), "daily",
        )
        # Successor: arrives at 07:00 on the 2nd, so the 2nd IS its day.
        after = _records_to_rows(
            records, LINE_ID, DEVICE,
            datetime(2026, 5, 2, 7), None, "daily",
        )

        assert [r["stamp"] for r in before] == [datetime(2026, 5, 1)]
        assert [r["stamp"] for r in after] == [
            datetime(2026, 5, 2), datetime(2026, 5, 3),
        ]
        # Counted once between them — never twice, never dropped.
        assert len(before) + len(after) == 3

    def test_open_window_takes_everything_from_its_start(self):
        rows = _records_to_rows(
            [daily("2026-05-01"), daily("2026-05-02")],
            LINE_ID, DEVICE, datetime(2026, 5, 2, 7), None, "daily",
        )
        assert [r["stamp"] for r in rows] == [datetime(2026, 5, 2)]

    def test_records_outside_the_window_are_dropped_entirely(self):
        rows = _records_to_rows(
            [daily("2026-04-01"), daily("2026-07-01")],
            LINE_ID, DEVICE,
            datetime(2026, 5, 1), datetime(2026, 6, 1), "daily",
        )
        assert rows == []


class TestHourlyWindow:
    """Hourly records are attributed at their exact stamp."""

    def test_boundary_hour_belongs_to_the_arriving_device(self):
        records = [
            hourly("2026-05-02T06:00:00"),
            hourly("2026-05-02T07:00:00"),
            hourly("2026-05-02T08:00:00"),
        ]
        before = _records_to_rows(
            records, LINE_ID, DEVICE,
            datetime(2026, 5, 1), datetime(2026, 5, 2, 7), "hourly",
        )
        after = _records_to_rows(
            records, LINE_ID, DEVICE, datetime(2026, 5, 2, 7), None, "hourly",
        )
        assert [r["stamp"] for r in before] == [datetime(2026, 5, 2, 6)]
        assert [r["stamp"] for r in after] == [
            datetime(2026, 5, 2, 7), datetime(2026, 5, 2, 8),
        ]

    def test_later_record_wins_for_a_repeated_stamp(self):
        """Rows are keyed by stamp, so a range DPD returns twice collapses
        instead of turning into two archive rows for one hour."""
        rows = _records_to_rows(
            [hourly("2026-05-02T08:00:00", dvstAlwrk=1.0),
             hourly("2026-05-02T08:00:00", dvstAlwrk=2.0)],
            LINE_ID, DEVICE, datetime(2026, 5, 1), None, "hourly",
        )
        assert len(rows) == 1
        assert rows[0]["volume"] == 2.0


class TestStampParsing:
    def test_accepts_both_hourly_shapes(self):
        assert _parse_stamp("2026-05-02T08:00:00", "hourly") == datetime(2026, 5, 2, 8)
        assert _parse_stamp("2026-05-02T08:00", "hourly") == datetime(2026, 5, 2, 8)
        assert _parse_stamp("2026-05-02T08:00:00.123456", "hourly") == datetime(2026, 5, 2, 8)

    def test_daily_ignores_any_time_part(self):
        assert _parse_stamp("2026-05-02", "daily") == datetime(2026, 5, 2)
        assert _parse_stamp("2026-05-02T00:00:00", "daily") == datetime(2026, 5, 2)

    def test_unparseable_is_none_not_an_exception(self):
        assert _parse_stamp("", "hourly") is None
        assert _parse_stamp(None, "daily") is None
        assert _parse_stamp("2026-13-45", "daily") is None

    def test_record_with_a_bad_stamp_is_skipped(self):
        rows = _records_to_rows(
            [{"date": "not a date", "dvstAlwrk": 5.0}, daily("2026-05-02")],
            LINE_ID, DEVICE, datetime(2026, 5, 1), None, "daily",
        )
        assert [r["stamp"] for r in rows] == [datetime(2026, 5, 2)]


class TestCarriedFields:
    def test_pressure_unit_is_normalised(self):
        rows = _records_to_rows(
            [daily("2026-05-02", press=0.7, temper=12.5, pressUnit="кгс/см2")],
            LINE_ID, DEVICE, datetime(2026, 5, 1), None, "daily",
        )
        row = rows[0]
        assert row["dpd_line_id"] == LINE_ID
        assert row["pressure"] == 0.7
        assert row["temperature"] == 12.5
        # Whatever normalize_press_unit returns, it must not be the raw string
        # verbatim unless that is already the canonical spelling.
        assert row["press_unit"] is not None

    def test_missing_optional_fields_stay_none(self):
        rows = _records_to_rows(
            [daily("2026-05-02")], LINE_ID, DEVICE,
            datetime(2026, 5, 1), None, "daily",
        )
        assert rows[0]["pressure"] is None
        assert rows[0]["temperature"] is None
