"""
Unit tests for virtual_lines_aggregator module.
"""

import pytest
from datetime import datetime, date
from unittest.mock import patch, MagicMock

from backend.services import virtual_lines_aggregator
from backend.db.models.hourly_archive_model import HourlyArchiveList
from backend.db.models.daily_archive_model import DailyArchiveList


# Mock virtual lines configuration
MOCK_VIRTUAL_LINES = {
    "1001": {
        "name": "Ring North",
        "physical_line_ids": [1, 6, 22],
        "active": True
    },
    "1002": {
        "name": "Ring South",
        "physical_line_ids": [4, 10],
        "active": True
    }
}


class TestAggregateToVirtualLines:
    """Tests for aggregate_to_virtual_lines function."""

    @patch("backend.services.virtual_lines_aggregator.get_active_virtual_lines")
    def test_no_virtual_lines_requested(self, mock_get_active):
        """Test when no virtual lines are requested (only physical)."""
        mock_get_active.return_value = MOCK_VIRTUAL_LINES

        # Create test archives
        archives = [
            HourlyArchiveList(
                id=1,
                line_id=1,
                period=datetime(2025, 12, 1, 0, 0),
                volume=100.0,
                w_volume_dp=95.0,
                pressure=5.0,
                temperature=20.0,
                density=0.8
            ),
            HourlyArchiveList(
                id=2,
                line_id=2,
                period=datetime(2025, 12, 1, 0, 0),
                volume=200.0,
                w_volume_dp=190.0,
                pressure=5.5,
                temperature=22.0,
                density=0.82
            )
        ]

        result = virtual_lines_aggregator.aggregate_to_virtual_lines(
            archives,
            requested_line_ids=[1, 2]  # Only physical lines
        )

        # Should return archives as-is (as dicts)
        assert len(result) == 2
        assert result[0]["line_id"] == 1
        assert result[1]["line_id"] == 2

    @patch("backend.services.virtual_lines_aggregator.get_active_virtual_lines")
    def test_aggregate_single_virtual_line(self, mock_get_active):
        """Test aggregating a single virtual line."""
        mock_get_active.return_value = MOCK_VIRTUAL_LINES

        # Create test archives for physical lines 1, 6, 22 (all in virtual line 1001)
        period = datetime(2025, 12, 1, 0, 0)
        archives = [
            HourlyArchiveList(
                id=1,
                line_id=1,
                period=period,
                volume=100.0,
                w_volume_dp=95.0,
                pressure=5.0,
                temperature=20.0,
                density=0.8
            ),
            HourlyArchiveList(
                id=2,
                line_id=6,
                period=period,
                volume=200.0,
                w_volume_dp=190.0,
                pressure=5.5,
                temperature=22.0,
                density=0.82
            ),
            HourlyArchiveList(
                id=3,
                line_id=22,
                period=period,
                volume=150.0,
                w_volume_dp=145.0,
                pressure=5.2,
                temperature=21.0,
                density=0.81
            )
        ]

        result = virtual_lines_aggregator.aggregate_to_virtual_lines(
            archives,
            requested_line_ids=[1001]
        )

        # Should return one aggregated record
        assert len(result) == 1
        assert result[0]["line_id"] == 1001
        assert result[0]["period"] == period

        # Check volume sum
        assert result[0]["volume"] == 450.0  # 100 + 200 + 150
        assert result[0]["w_volume_dp"] == 430.0  # 95 + 190 + 145

        # Check weighted averages
        # pressure: (5.0*100 + 5.5*200 + 5.2*150) / 450 = (500 + 1100 + 780) / 450 = 5.289
        assert abs(result[0]["pressure"] - 5.289) < 0.01

        # temperature: (20*100 + 22*200 + 21*150) / 450 = (2000 + 4400 + 3150) / 450 = 21.222
        assert abs(result[0]["temperature"] - 21.222) < 0.01

        # density: (0.8*100 + 0.82*200 + 0.81*150) / 450 = (80 + 164 + 121.5) / 450 = 0.8122
        assert abs(result[0]["density"] - 0.8122) < 0.01

    @patch("backend.services.virtual_lines_aggregator.get_active_virtual_lines")
    def test_aggregate_multiple_periods(self, mock_get_active):
        """Test aggregating multiple periods for same virtual line."""
        mock_get_active.return_value = MOCK_VIRTUAL_LINES

        period1 = datetime(2025, 12, 1, 0, 0)
        period2 = datetime(2025, 12, 1, 1, 0)

        archives = [
            # Period 1
            HourlyArchiveList(
                id=1, line_id=1, period=period1,
                volume=100.0, w_volume_dp=95.0,
                pressure=5.0, temperature=20.0, density=0.8
            ),
            HourlyArchiveList(
                id=2, line_id=6, period=period1,
                volume=200.0, w_volume_dp=190.0,
                pressure=5.5, temperature=22.0, density=0.82
            ),
            # Period 2
            HourlyArchiveList(
                id=3, line_id=1, period=period2,
                volume=110.0, w_volume_dp=105.0,
                pressure=5.1, temperature=20.5, density=0.81
            ),
            HourlyArchiveList(
                id=4, line_id=6, period=period2,
                volume=210.0, w_volume_dp=200.0,
                pressure=5.6, temperature=22.5, density=0.83
            )
        ]

        result = virtual_lines_aggregator.aggregate_to_virtual_lines(
            archives,
            requested_line_ids=[1001]
        )

        # Should return two aggregated records (one per period)
        assert len(result) == 2

        # Find period1 result
        period1_result = [r for r in result if r["period"] == period1][0]
        assert period1_result["volume"] == 300.0  # 100 + 200

        # Find period2 result
        period2_result = [r for r in result if r["period"] == period2][0]
        assert period2_result["volume"] == 320.0  # 110 + 210

    @patch("backend.services.virtual_lines_aggregator.get_active_virtual_lines")
    def test_aggregate_multiple_virtual_lines(self, mock_get_active):
        """Test aggregating multiple virtual lines."""
        mock_get_active.return_value = MOCK_VIRTUAL_LINES

        period = datetime(2025, 12, 1, 0, 0)

        archives = [
            # Virtual line 1001 (lines 1, 6)
            HourlyArchiveList(
                id=1, line_id=1, period=period,
                volume=100.0, w_volume_dp=95.0,
                pressure=5.0, temperature=20.0, density=0.8
            ),
            HourlyArchiveList(
                id=2, line_id=6, period=period,
                volume=200.0, w_volume_dp=190.0,
                pressure=5.5, temperature=22.0, density=0.82
            ),
            # Virtual line 1002 (lines 4, 10)
            HourlyArchiveList(
                id=3, line_id=4, period=period,
                volume=150.0, w_volume_dp=145.0,
                pressure=5.2, temperature=21.0, density=0.81
            ),
            HourlyArchiveList(
                id=4, line_id=10, period=period,
                volume=180.0, w_volume_dp=175.0,
                pressure=5.3, temperature=21.5, density=0.815
            )
        ]

        result = virtual_lines_aggregator.aggregate_to_virtual_lines(
            archives,
            requested_line_ids=[1001, 1002]
        )

        # Should return two aggregated records
        assert len(result) == 2

        # Find virtual line 1001
        vline1001 = [r for r in result if r["line_id"] == 1001][0]
        assert vline1001["volume"] == 300.0  # 100 + 200

        # Find virtual line 1002
        vline1002 = [r for r in result if r["line_id"] == 1002][0]
        assert vline1002["volume"] == 330.0  # 150 + 180

    @patch("backend.services.virtual_lines_aggregator.get_active_virtual_lines")
    def test_mixed_virtual_and_physical_lines(self, mock_get_active):
        """Test aggregating mix of virtual and physical lines."""
        mock_get_active.return_value = MOCK_VIRTUAL_LINES

        period = datetime(2025, 12, 1, 0, 0)

        archives = [
            # Virtual line 1001 (lines 1, 6)
            HourlyArchiveList(
                id=1, line_id=1, period=period,
                volume=100.0, w_volume_dp=95.0,
                pressure=5.0, temperature=20.0, density=0.8
            ),
            HourlyArchiveList(
                id=2, line_id=6, period=period,
                volume=200.0, w_volume_dp=190.0,
                pressure=5.5, temperature=22.0, density=0.82
            ),
            # Physical line 25 (not in any ring)
            HourlyArchiveList(
                id=3, line_id=25, period=period,
                volume=300.0, w_volume_dp=290.0,
                pressure=6.0, temperature=23.0, density=0.85
            )
        ]

        result = virtual_lines_aggregator.aggregate_to_virtual_lines(
            archives,
            requested_line_ids=[1001, 25]
        )

        # Should return two records: one aggregated (1001) and one physical (25)
        assert len(result) == 2

        # Find virtual line 1001
        vline1001 = [r for r in result if r["line_id"] == 1001][0]
        assert vline1001["volume"] == 300.0  # 100 + 200

        # Find physical line 25
        pline25 = [r for r in result if r["line_id"] == 25][0]
        assert pline25["volume"] == 300.0  # No aggregation

    @patch("backend.services.virtual_lines_aggregator.get_active_virtual_lines")
    def test_aggregate_with_zero_volume(self, mock_get_active):
        """Test aggregating when total volume is zero."""
        mock_get_active.return_value = MOCK_VIRTUAL_LINES

        period = datetime(2025, 12, 1, 0, 0)

        archives = [
            HourlyArchiveList(
                id=1, line_id=1, period=period,
                volume=0.0, w_volume_dp=0.0,
                pressure=5.0, temperature=20.0, density=0.8
            ),
            HourlyArchiveList(
                id=2, line_id=6, period=period,
                volume=0.0, w_volume_dp=0.0,
                pressure=5.5, temperature=22.0, density=0.82
            )
        ]

        result = virtual_lines_aggregator.aggregate_to_virtual_lines(
            archives,
            requested_line_ids=[1001]
        )

        # Should return one record with zero values
        assert len(result) == 1
        assert result[0]["volume"] == 0.0
        assert result[0]["pressure"] == 0.0
        assert result[0]["temperature"] == 0.0
        assert result[0]["density"] == 0.0

    @patch("backend.services.virtual_lines_aggregator.get_active_virtual_lines")
    def test_aggregate_daily_archives(self, mock_get_active):
        """Test aggregating daily archives (not just hourly)."""
        mock_get_active.return_value = MOCK_VIRTUAL_LINES

        period = date(2025, 12, 1)

        archives = [
            DailyArchiveList(
                id=1, line_id=1, period=period,
                volume=2400.0, w_volume_dp=2300.0,
                pressure=5.0, temperature=20.0, density=0.8
            ),
            DailyArchiveList(
                id=2, line_id=6, period=period,
                volume=4800.0, w_volume_dp=4600.0,
                pressure=5.5, temperature=22.0, density=0.82
            )
        ]

        result = virtual_lines_aggregator.aggregate_to_virtual_lines(
            archives,
            requested_line_ids=[1001]
        )

        # Should work the same for daily archives
        assert len(result) == 1
        assert result[0]["volume"] == 7200.0  # 2400 + 4800
        assert result[0]["w_volume_dp"] == 6900.0  # 2300 + 4600


class TestArchiveToDict:
    """Tests for _archive_to_dict helper function."""

    def test_hourly_archive_to_dict(self):
        """Test converting hourly archive to dict."""
        archive = HourlyArchiveList(
            id=1,
            line_id=5,
            period=datetime(2025, 12, 1, 0, 0),
            volume=100.0,
            w_volume_dp=95.0,
            pressure=5.0,
            temperature=20.0,
            density=0.8
        )

        result = virtual_lines_aggregator._archive_to_dict(archive)

        assert result["line_id"] == 5
        assert result["period"] == datetime(2025, 12, 1, 0, 0)
        assert result["volume"] == 100.0
        assert result["pressure"] == 5.0

    def test_daily_archive_to_dict(self):
        """Test converting daily archive to dict."""
        archive = DailyArchiveList(
            id=1,
            line_id=5,
            period=date(2025, 12, 1),
            volume=2400.0,
            w_volume_dp=2300.0,
            pressure=5.0,
            temperature=20.0,
            density=0.8
        )

        result = virtual_lines_aggregator._archive_to_dict(archive)

        assert result["line_id"] == 5
        assert result["period"] == date(2025, 12, 1)
        assert result["volume"] == 2400.0
