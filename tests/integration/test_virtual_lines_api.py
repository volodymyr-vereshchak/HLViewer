"""
Integration tests for Virtual Lines API endpoints.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, date
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.db.models.hourly_archive_model import HourlyArchiveList
from backend.db.models.daily_archive_model import DailyArchiveList
from backend.db.models.line_model import LineList


# Mock virtual lines configuration
MOCK_VIRTUAL_LINES_CONFIG = {
    "1001": {
        "name": "Test Ring North",
        "physical_line_ids": [1, 6, 22],
        "active": True,
        "description": "Northern test ring"
    },
    "1002": {
        "name": "Test Ring South",
        "physical_line_ids": [4, 10],
        "active": True,
        "description": "Southern test ring"
    }
}


class TestVirtualLinesEndpoints:
    """Tests for /virtual_lines/ endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    @patch("backend.services.virtual_lines_config.get_active_virtual_lines")
    @patch("backend.services.virtual_lines_config.get_physical_lines_in_rings")
    @patch("backend.db.dao.line_dao.LineDao.get_all_items")
    def test_get_visible_lines(
        self,
        mock_get_all_lines,
        mock_get_physical_in_rings,
        mock_get_active_virtual,
        client
    ):
        """Test GET /virtual_lines/visible endpoint."""
        # Setup mocks
        mock_get_active_virtual.return_value = MOCK_VIRTUAL_LINES_CONFIG
        mock_get_physical_in_rings.return_value = {1, 6, 22, 4, 10}

        # Mock physical lines from database
        mock_get_all_lines.return_value = [
            LineList(id=1, gas_volume_calc_id=1, line=1, meter=True, name="Line 1"),
            LineList(id=6, gas_volume_calc_id=1, line=6, meter=True, name="Line 6"),
            LineList(id=22, gas_volume_calc_id=1, line=22, meter=True, name="Line 22"),
            LineList(id=4, gas_volume_calc_id=1, line=4, meter=True, name="Line 4"),
            LineList(id=10, gas_volume_calc_id=1, line=10, meter=True, name="Line 10"),
            LineList(id=25, gas_volume_calc_id=1, line=25, meter=True, name="Line 25"),  # Not in ring
        ]

        # Make request
        response = client.get("/virtual_lines/visible")

        # Verify response
        assert response.status_code == 200
        data = response.json()

        # Should have 3 items: 2 virtual + 1 physical (line 25 not in ring)
        assert len(data) == 3

        # Check virtual line 1001
        vline1001 = next((item for item in data if item["id"] == 1001), None)
        assert vline1001 is not None
        assert vline1001["name"] == "Test Ring North"
        assert vline1001["is_virtual"] is True
        assert set(vline1001["physical_line_ids"]) == {1, 6, 22}

        # Check virtual line 1002
        vline1002 = next((item for item in data if item["id"] == 1002), None)
        assert vline1002 is not None
        assert vline1002["name"] == "Test Ring South"
        assert vline1002["is_virtual"] is True
        assert set(vline1002["physical_line_ids"]) == {4, 10}

        # Check physical line 25 (not in any ring)
        pline25 = next((item for item in data if item["id"] == 25), None)
        assert pline25 is not None
        assert pline25["name"] == "Line 25"
        assert pline25["is_virtual"] is False
        assert pline25["physical_line_ids"] is None

    @patch("backend.services.virtual_lines_config.validate_config")
    def test_validate_configuration_success(self, mock_validate, client):
        """Test GET /virtual_lines/validate endpoint with valid config."""
        # Setup mock
        mock_validate.return_value = {
            "valid": True,
            "virtual_lines_count": 2,
            "active_count": 2,
            "hidden_physical_lines": [1, 6, 22, 4, 10],
            "errors": [],
            "warnings": []
        }

        # Make request
        response = client.get("/virtual_lines/validate")

        # Verify response
        assert response.status_code == 200
        data = response.json()

        assert data["valid"] is True
        assert data["virtual_lines_count"] == 2
        assert data["active_count"] == 2
        assert set(data["hidden_physical_lines"]) == {1, 6, 22, 4, 10}
        assert len(data["errors"]) == 0

    @patch("backend.services.virtual_lines_config.validate_config")
    def test_validate_configuration_with_errors(self, mock_validate, client):
        """Test GET /virtual_lines/validate endpoint with invalid config."""
        # Setup mock
        mock_validate.return_value = {
            "valid": False,
            "virtual_lines_count": 2,
            "active_count": 2,
            "hidden_physical_lines": [1, 6, 22, 4, 10],
            "errors": ["Physical line 6 is used in multiple active virtual lines"],
            "warnings": []
        }

        # Make request
        response = client.get("/virtual_lines/validate")

        # Verify response
        assert response.status_code == 200
        data = response.json()

        assert data["valid"] is False
        assert len(data["errors"]) == 1
        assert "Physical line 6" in data["errors"][0]


class TestHourlyVirtualEndpoints:
    """Tests for /hourly_virtual/ endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    @patch("backend.services.virtual_lines_config.get_active_virtual_lines")
    @patch("backend.db.dao.hourly_archive_dao.HourlyArchiveDao.get_range")
    def test_get_hourly_archives_no_virtual_lines(
        self,
        mock_get_range,
        mock_get_active_virtual,
        client
    ):
        """Test GET /hourly_virtual/ with only physical lines."""
        # Setup mocks
        mock_get_active_virtual.return_value = MOCK_VIRTUAL_LINES_CONFIG

        period = datetime(2025, 12, 1, 0, 0)
        mock_get_range.return_value = [
            HourlyArchiveList(
                id=1, line_id=25, period=period,
                volume=100.0, w_volume_dp=95.0,
                pressure=5.0, temperature=20.0, density=0.8
            )
        ]

        # Make request
        response = client.get(
            "/hourly_virtual/",
            params={
                "from_date": "2025-12-01T00:00:00",
                "to_date": "2025-12-01T23:59:59",
                "line_id": [25]
            }
        )

        # Verify response
        assert response.status_code == 200
        data = response.json()

        assert len(data) == 1
        assert data[0]["line_id"] == 25
        assert data[0]["volume"] == 100.0

    @patch("backend.services.virtual_lines_config.get_active_virtual_lines")
    @patch("backend.services.virtual_lines_config.resolve_virtual_to_physical")
    @patch("backend.db.dao.hourly_archive_dao.HourlyArchiveDao.get_range")
    def test_get_hourly_archives_with_virtual_lines(
        self,
        mock_get_range,
        mock_resolve,
        mock_get_active_virtual,
        client
    ):
        """Test GET /hourly_virtual/ with virtual lines."""
        # Setup mocks
        mock_get_active_virtual.return_value = MOCK_VIRTUAL_LINES_CONFIG
        mock_resolve.return_value = [1, 6, 22]  # Virtual line 1001 -> physical [1, 6, 22]

        period = datetime(2025, 12, 1, 0, 0)
        mock_get_range.return_value = [
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
            HourlyArchiveList(
                id=3, line_id=22, period=period,
                volume=150.0, w_volume_dp=145.0,
                pressure=5.2, temperature=21.0, density=0.81
            )
        ]

        # Make request
        response = client.get(
            "/hourly_virtual/",
            params={
                "from_date": "2025-12-01T00:00:00",
                "to_date": "2025-12-01T23:59:59",
                "line_id": [1001]
            }
        )

        # Verify response
        assert response.status_code == 200
        data = response.json()

        assert len(data) == 1
        assert data[0]["line_id"] == 1001

        # Check aggregation
        assert data[0]["volume"] == 450.0  # 100 + 200 + 150
        assert data[0]["w_volume_dp"] == 430.0  # 95 + 190 + 145

        # Check weighted average (pressure: (5.0*100 + 5.5*200 + 5.2*150) / 450 = 5.289)
        assert abs(data[0]["pressure"] - 5.289) < 0.01

    @patch("backend.services.virtual_lines_config.get_active_virtual_lines")
    @patch("backend.services.virtual_lines_config.resolve_virtual_to_physical")
    @patch("backend.db.dao.hourly_archive_dao.HourlyArchiveDao.get_range")
    def test_get_hourly_archives_mixed_virtual_and_physical(
        self,
        mock_get_range,
        mock_resolve,
        mock_get_active_virtual,
        client
    ):
        """Test GET /hourly_virtual/ with mix of virtual and physical lines."""
        # Setup mocks
        mock_get_active_virtual.return_value = MOCK_VIRTUAL_LINES_CONFIG
        mock_resolve.return_value = [1, 6, 22, 25]  # Virtual 1001 + physical 25

        period = datetime(2025, 12, 1, 0, 0)
        mock_get_range.return_value = [
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
            HourlyArchiveList(
                id=3, line_id=22, period=period,
                volume=150.0, w_volume_dp=145.0,
                pressure=5.2, temperature=21.0, density=0.81
            ),
            HourlyArchiveList(
                id=4, line_id=25, period=period,
                volume=300.0, w_volume_dp=290.0,
                pressure=6.0, temperature=23.0, density=0.85
            )
        ]

        # Make request
        response = client.get(
            "/hourly_virtual/",
            params={
                "from_date": "2025-12-01T00:00:00",
                "to_date": "2025-12-01T23:59:59",
                "line_id": [1001, 25]
            }
        )

        # Verify response
        assert response.status_code == 200
        data = response.json()

        # Should have 2 records: 1 aggregated virtual + 1 physical
        assert len(data) == 2

        # Find virtual line 1001
        vline1001 = next((item for item in data if item["line_id"] == 1001), None)
        assert vline1001 is not None
        assert vline1001["volume"] == 450.0  # 100 + 200 + 150

        # Find physical line 25
        pline25 = next((item for item in data if item["line_id"] == 25), None)
        assert pline25 is not None
        assert pline25["volume"] == 300.0


class TestDailyVirtualEndpoints:
    """Tests for /daily_virtual/ endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    @patch("backend.services.virtual_lines_config.get_active_virtual_lines")
    @patch("backend.services.virtual_lines_config.resolve_virtual_to_physical")
    @patch("backend.db.dao.daily_archive_dao.DailyArchiveDao.get_range")
    def test_get_daily_archives_with_virtual_lines(
        self,
        mock_get_range,
        mock_resolve,
        mock_get_active_virtual,
        client
    ):
        """Test GET /daily_virtual/ with virtual lines."""
        # Setup mocks
        mock_get_active_virtual.return_value = MOCK_VIRTUAL_LINES_CONFIG
        mock_resolve.return_value = [1, 6, 22]

        period = date(2025, 12, 1)
        mock_get_range.return_value = [
            DailyArchiveList(
                id=1, line_id=1, period=period,
                volume=2400.0, w_volume_dp=2300.0,
                pressure=5.0, temperature=20.0, density=0.8
            ),
            DailyArchiveList(
                id=2, line_id=6, period=period,
                volume=4800.0, w_volume_dp=4600.0,
                pressure=5.5, temperature=22.0, density=0.82
            ),
            DailyArchiveList(
                id=3, line_id=22, period=period,
                volume=3600.0, w_volume_dp=3450.0,
                pressure=5.2, temperature=21.0, density=0.81
            )
        ]

        # Make request
        response = client.get(
            "/daily_virtual/",
            params={
                "from_date": "2025-12-01T00:00:00",
                "to_date": "2025-12-01T23:59:59",
                "line_id": [1001]
            }
        )

        # Verify response
        assert response.status_code == 200
        data = response.json()

        assert len(data) == 1
        assert data[0]["line_id"] == 1001
        assert data[0]["volume"] == 10800.0  # 2400 + 4800 + 3600
        assert data[0]["w_volume_dp"] == 10350.0  # 2300 + 4600 + 3450
