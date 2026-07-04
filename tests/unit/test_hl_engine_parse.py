"""Unit tests for the pure parsing pieces of backend/hl_engine/hl_engine.py:
filename → address/line extraction and binary record reading (no DB)."""

import math
import struct

from backend.db.models import DailyArchiveCreate, HourlyArchiveCreate
from backend.hl_engine.data_classes.day_dataclass import DayStruct
from backend.hl_engine.data_classes.hour_dataclass import HourStruct
from backend.hl_engine.hl_engine import Hostlib, _read_file_records_sync

HOURLY_FIELDS = set(HourlyArchiveCreate.model_fields)
DAILY_FIELDS = set(DailyArchiveCreate.model_fields)


def _pack_hour(month, day, year, hour, minutes, volume=1000.5, density=0.7) -> bytes:
    return struct.pack(
        "=5B6f", month, day, year, hour, minutes, volume, 0.0, 0.1, 5.2, 20.5, density
    )


def _pack_day(month, day, year, volume=24000.0) -> bytes:
    return struct.pack("=3B6f", month, day, year, volume, 0.0, 2.4, 5.2, 20.5, 0.7)


class TestGetParamsFromFileName:
    def test_extracts_address_and_line(self):
        params = Hostlib.get_params_from_file_name("S012R3R.NOV")
        assert params == {"address": 12, "line": 3}

    def test_full_path_uses_basename(self):
        params = Hostlib.get_params_from_file_name(r"D:\archives\deep\S250R1D.24C")
        assert params == {"address": 250, "line": 1}


class TestReadFileRecordsSync:
    def test_hourly_records(self, tmp_path):
        path = tmp_path / "S012R1R.bin"
        path.write_bytes(_pack_hour(12, 25, 24, 14, 30) + _pack_hour(12, 25, 24, 15, 30))
        records = _read_file_records_sync(
            str(path), HourStruct, date_flag=False, line_id=7, model_fields=HOURLY_FIELDS
        )
        assert len(records) == 2
        first = records[0]
        assert first["line_id"] == 7
        assert first["period"].year == 2024  # year byte 24 → 2024
        assert first["period"].hour == 14
        assert first["period"].minute == 30
        assert abs(first["volume"] - 1000.5) < 1e-3
        # non-model fields (month/day/unknown/...) are stripped
        assert "month" not in first
        assert "unknown" not in first

    def test_daily_records_use_date(self, tmp_path):
        path = tmp_path / "S012R1D.bin"
        path.write_bytes(_pack_day(12, 25, 24))
        records = _read_file_records_sync(
            str(path), DayStruct, date_flag=True, line_id=1, model_fields=DAILY_FIELDS
        )
        assert len(records) == 1
        period = records[0]["period"]
        assert (period.year, period.month, period.day) == (2024, 12, 25)
        assert not hasattr(period, "hour")  # date, not datetime

    def test_invalid_date_skipped(self, tmp_path):
        path = tmp_path / "S012R1R.bin"
        # month=13 → ValueError inside → record dropped, valid one kept
        path.write_bytes(_pack_hour(13, 25, 24, 14, 30) + _pack_hour(1, 2, 25, 3, 0))
        records = _read_file_records_sync(
            str(path), HourStruct, date_flag=False, line_id=1, model_fields=HOURLY_FIELDS
        )
        assert len(records) == 1
        assert records[0]["period"].month == 1

    def test_nan_replaced_with_zero(self, tmp_path):
        path = tmp_path / "S012R1R.bin"
        path.write_bytes(_pack_hour(12, 25, 24, 14, 30, density=math.nan))
        records = _read_file_records_sync(
            str(path), HourStruct, date_flag=False, line_id=1, model_fields=HOURLY_FIELDS
        )
        assert records[0]["density"] == 0.0

    def test_empty_file(self, tmp_path):
        path = tmp_path / "S012R1R.bin"
        path.write_bytes(b"")
        assert (
            _read_file_records_sync(
                str(path), HourStruct, date_flag=False, line_id=1, model_fields=HOURLY_FIELDS
            )
            == []
        )
