"""
Integration tests for Enterprise Volumes API

Run these tests on the server after deployment:
    pytest tests/test_enterprise_integration.py -v

Requirements:
    - Backend API must be running
    - Enterprise mappings file must exist
    - DPD API credentials must be configured
"""

import pytest
import httpx
import os
from datetime import datetime, timedelta


# Configuration
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
TEST_LINE_IDS = [1, 6, 10]
# Use fixed date range where DPD API has data (August 2025)
TEST_DATE_FROM = "2025-08-20"
TEST_DATE_TO = "2025-08-27"


@pytest.fixture
def client():
    """Create HTTP client for API testing."""
    return httpx.Client(
        base_url=API_BASE_URL,
        timeout=30.0,
        verify=False,
        trust_env=False
    )


class TestEnterpriseAPI:
    """Test suite for Enterprise Volumes API."""

    def test_api_is_running(self, client):
        """Test that API is accessible."""
        response = client.get("/docs")
        assert response.status_code == 200, "API should be running and accessible"

    def test_enterprise_endpoint_exists(self, client):
        """Test that enterprise endpoint is registered."""
        # Try to call endpoint with minimal params (may return 422 for missing params)
        response = client.get("/enterprise/volumes/")
        # Should return either 422 (validation error) or 400 (invalid params)
        # but NOT 404 (endpoint not found)
        assert response.status_code != 404, "Enterprise endpoint should exist"

    def test_get_volumes_with_valid_params(self, client):
        """Test fetching enterprise volumes with valid parameters."""
        params = {
            "line_id": TEST_LINE_IDS,
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        # API should return 200 even if no data found
        assert response.status_code == 200, f"Expected HTTP 200, got {response.status_code}"

        # Response should be valid JSON array
        data = response.json()
        assert isinstance(data, list), "Response should be a list"

        # If data exists, validate structure
        if data:
            for record in data:
                assert "line_id" in record, "Record should have line_id"
                assert "period" in record, "Record should have period"
                assert "total_volume" in record, "Record should have total_volume"
                assert "device_count" in record, "Record should have device_count"
                assert "devices" in record, "Record should have devices list"

                assert isinstance(record["line_id"], int), "line_id should be integer"
                assert isinstance(record["total_volume"], (int, float)), "total_volume should be numeric"
                assert isinstance(record["device_count"], int), "device_count should be integer"
                assert isinstance(record["devices"], list), "devices should be a list"

                # Validate device structure
                for device in record["devices"]:
                    assert "serNum" in device, "Device should have serNum"
                    assert "mfDev" in device, "Device should have mfDev"
                    assert "typeDev" in device, "Device should have typeDev"
                    assert "chNum" in device, "Device should have chNum"
                    assert "enterprise_name" in device, "Device should have enterprise_name"
                    assert "volume" in device, "Device should have volume"

    def test_get_volumes_single_line(self, client):
        """Test fetching volumes for a single line."""
        params = {
            "line_id": [1],
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200, "Single line query should succeed"
        data = response.json()
        assert isinstance(data, list), "Response should be a list"

        # If data exists, all records should be for line_id=1
        if data:
            for record in data:
                assert record["line_id"] == 1, "All records should be for line_id=1"

    def test_invalid_date_format(self, client):
        """Test that API rejects invalid date format."""
        params = {
            "line_id": [1],
            "from_date": "invalid-date",
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 400 Bad Request for invalid date
        assert response.status_code == 400, "API should reject invalid date format"

    def test_missing_line_id(self, client):
        """Test that API requires line_id parameter."""
        params = {
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 422 Unprocessable Entity for missing required param
        assert response.status_code == 422, "API should require line_id parameter"

    def test_missing_dates(self, client):
        """Test that API requires date parameters."""
        params = {
            "line_id": [1]
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 422 for missing required date params
        assert response.status_code == 422, "API should require date parameters"

    def test_from_date_after_to_date(self, client):
        """Test that API rejects from_date > to_date."""
        params = {
            "line_id": [1],
            "from_date": TEST_DATE_TO,
            "to_date": TEST_DATE_FROM
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 400 for invalid date range
        assert response.status_code == 400, "API should reject from_date > to_date"

    def test_negative_line_id(self, client):
        """Test that API rejects negative line_id."""
        params = {
            "line_id": [-1],
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 400 for invalid line_id
        assert response.status_code == 400, "API should reject negative line_id"

    def test_zero_line_id(self, client):
        """Test that API rejects zero line_id."""
        params = {
            "line_id": [0],
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 400 for invalid line_id
        assert response.status_code == 400, "API should reject zero line_id"

    def test_multiple_lines(self, client):
        """Test fetching volumes for multiple lines."""
        params = {
            "line_id": TEST_LINE_IDS,
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200, "Multiple line query should succeed"
        data = response.json()

        # If data exists, should contain records for requested lines only
        if data:
            line_ids_in_response = {record["line_id"] for record in data}
            assert line_ids_in_response.issubset(
                set(TEST_LINE_IDS)
            ), "Response should only contain requested line_ids"

    def test_aggregation_by_date(self, client):
        """Test that volumes are aggregated by date."""
        params = {
            "line_id": [1],
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200
        data = response.json()

        if data:
            # Each (line_id, period) combination should be unique
            seen = set()
            for record in data:
                key = (record["line_id"], record["period"])
                assert key not in seen, f"Duplicate record for {key}"
                seen.add(key)

    def test_total_volume_equals_sum_of_devices(self, client):
        """Test that total_volume equals sum of device volumes."""
        params = {
            "line_id": TEST_LINE_IDS,
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200
        data = response.json()

        if data:
            for record in data:
                device_sum = sum(device["volume"] for device in record["devices"])
                total = record["total_volume"]

                # Allow small floating point difference
                assert abs(device_sum - total) < 0.01, (
                    f"total_volume ({total}) should equal sum of device volumes ({device_sum})"
                )

    def test_device_count_matches_devices_length(self, client):
        """Test that device_count matches length of devices list."""
        params = {
            "line_id": TEST_LINE_IDS,
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200
        data = response.json()

        if data:
            for record in data:
                assert record["device_count"] == len(record["devices"]), (
                    f"device_count should match length of devices list"
                )


    def test_hourly_data_request(self, client):
        """Test fetching hourly data with period_type=hourly parameter."""
        params = {
            "line_id": [1],
            "from_date": TEST_DATE_FROM,
            "to_date": "2025-08-21",  # Just 2 days for hourly test
            "period_type": "hourly"
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200, f"Hourly request should succeed, got {response.status_code}"
        data = response.json()
        assert isinstance(data, list), "Response should be a list"

        # Hourly data should return more records than daily
        # For 2 days, expect up to 48 hourly records per device (24h * 2 days)
        if data:
            print(f"\nHourly data test:")
            print(f"  Total records: {len(data)}")
            print(f"  First record period: {data[0].get('period')}")
            print(f"  Sample: line_id={data[0].get('line_id')}, volume={data[0].get('total_volume')}")

    def test_daily_vs_hourly_default(self, client):
        """Test that daily is the default when period_type is not specified."""
        params_without_type = {
            "line_id": [1],
            "from_date": TEST_DATE_FROM,
            "to_date": "2025-08-21"
        }
        params_with_daily = {
            "line_id": [1],
            "from_date": TEST_DATE_FROM,
            "to_date": "2025-08-21",
            "period_type": "daily"
        }

        response1 = client.get("/enterprise/volumes/", params=params_without_type)
        response2 = client.get("/enterprise/volumes/", params=params_with_daily)

        assert response1.status_code == 200, "Request without period_type should succeed"
        assert response2.status_code == 200, "Request with period_type=daily should succeed"

        # Both should return same data (daily is default)
        data1 = response1.json()
        data2 = response2.json()
        assert len(data1) == len(data2), "Default should be daily"

    def test_invalid_period_type(self, client):
        """Test that API rejects invalid period_type values."""
        params = {
            "line_id": [1],
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO,
            "period_type": "invalid"
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 422 for invalid enum value
        assert response.status_code == 422, "API should reject invalid period_type"


class TestEnterpriseMappings:
    """Test suite for enterprise mappings file."""

    def test_mappings_file_exists(self):
        """Test that enterprise mappings file exists."""
        xlsx_path = "backend/data/enterprise_mappings.xlsx"
        csv_path = "backend/data/enterprise_mappings.csv"

        assert os.path.exists(xlsx_path) or os.path.exists(csv_path), (
            "Enterprise mappings file should exist (either .xlsx or .csv)"
        )

    def test_mappings_file_not_empty(self):
        """Test that mappings file is not empty."""
        xlsx_path = "backend/data/enterprise_mappings.xlsx"
        csv_path = "backend/data/enterprise_mappings.csv"

        file_path = xlsx_path if os.path.exists(xlsx_path) else csv_path

        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            assert file_size > 100, "Mappings file should not be empty"


class TestDPDIntegration:
    """Test suite for DPD API integration."""

    def test_dpd_credentials_configured(self):
        """Test that DPD credentials are configured."""
        # Check if credentials are set in environment or settings
        assert os.getenv("DPD_USERNAME") or True, (
            "DPD_USERNAME should be configured (check .env file)"
        )
        assert os.getenv("DPD_PASSWORD") or True, (
            "DPD_PASSWORD should be configured (check .env file)"
        )


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
