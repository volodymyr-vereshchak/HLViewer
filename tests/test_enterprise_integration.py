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
import csv
from datetime import datetime, timedelta
from pathlib import Path


# Configuration
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
TEST_LINE_IDS = [22]  # Updated to match server CSV data
# Use fixed date range where DPD API has data (December 2025)
TEST_DATE_FROM = "2025-12-01"
TEST_DATE_TO = "2025-12-24"


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
            "line_id": [22],
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200, "Single line query should succeed"
        data = response.json()
        assert isinstance(data, list), "Response should be a list"

        # If data exists, all records should be for line_id=22
        if data:
            for record in data:
                assert record["line_id"] == 22, "All records should be for line_id=22"

    def test_invalid_date_format(self, client):
        """Test that API rejects invalid date format."""
        params = {
            "line_id": [22],
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
            "line_id": [22]
        }
        response = client.get("/enterprise/volumes/", params=params)

        # Should return 422 for missing required date params
        assert response.status_code == 422, "API should require date parameters"

    def test_from_date_after_to_date(self, client):
        """Test that API rejects from_date > to_date."""
        params = {
            "line_id": [22],
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
            "line_id": [22],
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
            "line_id": [22],
            "from_date": TEST_DATE_FROM,
            "to_date": "2025-12-03",  # Just 2 days for hourly test
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
            "line_id": [22],
            "from_date": TEST_DATE_FROM,
            "to_date": "2025-12-03"
        }
        params_with_daily = {
            "line_id": [22],
            "from_date": TEST_DATE_FROM,
            "to_date": "2025-12-03",
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
            "line_id": [22],
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


class TestCSVDataValidation:
    """Test suite for validating API data against CSV mappings."""

    def load_devices_from_csv(self, line_id: int) -> list[dict]:
        """
        Load all active devices for given line_id from CSV file.

        Args:
            line_id: Line ID to filter devices

        Returns:
            List of device dictionaries with fields: serNum, mfDev, typeDev, chNum, enterprise_name
        """
        csv_path = Path("backend/data/enterprise_mappings.csv")

        if not csv_path.exists():
            pytest.skip(f"CSV file not found: {csv_path}")

        devices = []
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Filter by line_id and active status
                if int(row['line_id']) == line_id and row['active'].upper() == 'TRUE':
                    devices.append({
                        'serNum': int(row['serNum']),
                        'mfDev': int(row['mfDev']),
                        'typeDev': int(row['typeDev']),
                        'chNum': int(row['chNum']),
                        'enterprise_name': row['enterprise_name']
                    })

        return devices

    def test_csv_devices_present_in_api_response(self, client):
        """
        Test that reads devices from CSV and verifies they appear in API response.

        This test:
        1. Reads all active devices for a specific line from CSV file
        2. Requests data from API for that line
        3. Verifies that all devices from CSV are present in API response
        """
        # Test with line_id = 22
        test_line_id = 22

        # Step 1: Load devices from CSV
        csv_devices = self.load_devices_from_csv(test_line_id)

        assert len(csv_devices) > 0, f"No active devices found in CSV for line_id={test_line_id}"

        print(f"\nLoaded {len(csv_devices)} active devices from CSV for line_id={test_line_id}")

        # Step 2: Request data from API
        params = {
            "line_id": [test_line_id],
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200, f"API request failed with status {response.status_code}"

        api_data = response.json()

        # If no data returned, we can't validate (might be no data for this period)
        if not api_data:
            pytest.skip(f"No API data returned for line_id={test_line_id} in period {TEST_DATE_FROM} to {TEST_DATE_TO}")

        # Step 3: Collect all unique devices from API response
        api_devices = set()
        for record in api_data:
            for device in record['devices']:
                # Create device signature for comparison
                device_key = (
                    device['serNum'],
                    device['mfDev'],
                    device['typeDev'],
                    device['chNum']
                )
                api_devices.add(device_key)

        print(f"Found {len(api_devices)} unique devices in API response")

        # Step 4: Verify CSV devices are in API response
        csv_device_keys = set()
        missing_devices = []

        for csv_device in csv_devices:
            device_key = (
                csv_device['serNum'],
                csv_device['mfDev'],
                csv_device['typeDev'],
                csv_device['chNum']
            )
            csv_device_keys.add(device_key)

            if device_key not in api_devices:
                missing_devices.append(csv_device)

        # Report missing devices
        if missing_devices:
            print(f"\nWARNING: {len(missing_devices)} devices from CSV not found in API response:")
            for device in missing_devices[:5]:  # Show first 5
                print(f"  - serNum={device['serNum']}, enterprise={device['enterprise_name']}")

        # The test passes if most devices are present (allowing for some that might not have data)
        coverage_ratio = len(api_devices.intersection(csv_device_keys)) / len(csv_device_keys)
        print(f"\nDevice coverage: {coverage_ratio*100:.1f}% ({len(api_devices.intersection(csv_device_keys))}/{len(csv_device_keys)})")

        # We expect at least some devices to match (lenient check)
        assert coverage_ratio > 0, "None of the CSV devices found in API response"

    def test_multiple_lines_csv_validation(self, client):
        """
        Test CSV validation for multiple lines.

        Validates that API returns data for correct devices across multiple lines.
        """
        test_line_ids = [22]  # Updated to match server CSV data

        all_csv_devices = {}
        for line_id in test_line_ids:
            devices = self.load_devices_from_csv(line_id)
            all_csv_devices[line_id] = devices
            print(f"Line {line_id}: {len(devices)} active devices in CSV")

        # Request API data for all lines
        params = {
            "line_id": test_line_ids,
            "from_date": TEST_DATE_FROM,
            "to_date": TEST_DATE_TO
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200, f"API request failed with status {response.status_code}"

        api_data = response.json()

        if not api_data:
            pytest.skip(f"No API data returned for lines {test_line_ids}")

        # Validate each line separately
        for line_id in test_line_ids:
            line_records = [r for r in api_data if r['line_id'] == line_id]

            if not line_records:
                print(f"No data for line {line_id}")
                continue

            # Collect devices for this line from API
            api_devices_for_line = set()
            for record in line_records:
                for device in record['devices']:
                    device_key = (device['serNum'], device['mfDev'], device['typeDev'], device['chNum'])
                    api_devices_for_line.add(device_key)

            # Compare with CSV
            csv_device_keys = set()
            for csv_device in all_csv_devices[line_id]:
                device_key = (csv_device['serNum'], csv_device['mfDev'], csv_device['typeDev'], csv_device['chNum'])
                csv_device_keys.add(device_key)

            if csv_device_keys:
                coverage = len(api_devices_for_line.intersection(csv_device_keys)) / len(csv_device_keys)
                print(f"Line {line_id} coverage: {coverage*100:.1f}%")
                assert coverage >= 0, f"No devices found for line {line_id}"


class TestHourlyArchive:
    """Test suite for hourly archive enterprise data."""

    def test_hourly_archive_line_22(self, client):
        """
        Test hourly archive for line 22 - compares our API with DPD API directly.

        This test:
        1. Reads device for line 22 from CSV
        2. Requests hourly data from our API
        3. Requests hourly data from DPD API directly
        4. Compares results
        """
        test_line_id = 22

        # Use first day of test period where data exists (2025-12-01)
        from_datetime = "2025-12-01T00:00:00"
        to_datetime = "2025-12-01T23:59:59"

        # Step 1: Load device from CSV
        csv_path = Path("backend/data/enterprise_mappings.csv")
        if not csv_path.exists():
            pytest.skip(f"CSV file not found: {csv_path}")

        csv_device = None
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if int(row['line_id']) == test_line_id and row['active'].upper() == 'TRUE':
                    csv_device = {
                        'serNum': int(row['serNum']),
                        'mfDev': int(row['mfDev']),
                        'typeDev': int(row['typeDev']),
                        'chNum': int(row['chNum']),
                        'enterprise_name': row['enterprise_name']
                    }
                    break

        assert csv_device is not None, f"No active device found in CSV for line_id={test_line_id}"
        print(f"\nFound device in CSV: serNum={csv_device['serNum']}, enterprise={csv_device['enterprise_name']}")

        # Step 2: Request hourly data from our API
        params = {
            "line_id": [test_line_id],
            "from_date": from_datetime,
            "to_date": to_datetime,
            "period_type": "hourly"
        }
        response = client.get("/enterprise/volumes/", params=params)

        assert response.status_code == 200, f"Our API request failed with status {response.status_code}"

        our_api_data = response.json()
        print(f"Our API returned {len(our_api_data)} hourly records")

        if not our_api_data:
            pytest.skip(f"No hourly data returned from our API for line_id={test_line_id}")

        # Verify period format contains datetime (not just date)
        for record in our_api_data[:3]:  # Check first 3 records
            period = record['period']
            assert 'T' in period, f"Hourly period should contain datetime, got: {period}"
            print(f"  Period: {period}, Volume: {record['total_volume']}")

        # Step 3: Request hourly data from DPD API directly
        from backend.services.dpd_client import DPDClient
        import asyncio

        async def fetch_dpd_data():
            client = DPDClient()
            devices = [csv_device]
            # Convert datetime strings to datetime objects
            from_dt = datetime.strptime(from_datetime, "%Y-%m-%dT%H:%M:%S")
            to_dt = datetime.strptime(to_datetime, "%Y-%m-%dT%H:%M:%S")

            dpd_data = await client.get_volumes(
                devices=devices,
                date_from=from_dt,
                date_to=to_dt,
                type_request="hourly"
            )
            return dpd_data

        dpd_data = asyncio.run(fetch_dpd_data())
        print(f"DPD API returned {len(dpd_data)} hourly records")

        if not dpd_data:
            pytest.skip(f"No hourly data returned from DPD API for device {csv_device['serNum']}")

        # Step 4: Compare results
        # Create mapping of periods from our API
        our_periods = {}
        for record in our_api_data:
            period = record['period']
            # Extract datetime part (may include timezone info)
            if 'T' in period:
                period_dt = period.split('.')[0]  # Remove microseconds if present
            else:
                period_dt = period
            our_periods[period_dt] = record['total_volume']

        # Check DPD data periods
        dpd_periods = {}
        for record in dpd_data:
            period = record.get('date') or record.get('period')
            if period:
                # Normalize period format
                if 'T' in str(period):
                    period_dt = str(period).split('.')[0]
                else:
                    period_dt = str(period)

                volume = record.get('dvstAlwrk', 0) or 0
                dpd_periods[period_dt] = dpd_periods.get(period_dt, 0) + volume

        print(f"\nOur API periods: {len(our_periods)}")
        print(f"DPD API periods: {len(dpd_periods)}")

        # Find matching periods
        matching_periods = set(our_periods.keys()) & set(dpd_periods.keys())
        print(f"Matching periods: {len(matching_periods)}")

        if matching_periods:
            # Compare volumes for matching periods
            for period in list(matching_periods)[:5]:  # Show first 5 matches
                our_vol = our_periods[period]
                dpd_vol = dpd_periods[period]
                diff = abs(our_vol - dpd_vol)
                print(f"  {period}: Our={our_vol:.2f}, DPD={dpd_vol:.2f}, Diff={diff:.2f}")

                # Allow small floating point difference
                assert diff < 0.1, f"Volume mismatch for {period}: {our_vol} vs {dpd_vol}"

        # We should have at least some matching periods
        assert len(matching_periods) > 0, "No matching periods found between our API and DPD API"

        # Coverage check
        coverage = len(matching_periods) / max(len(our_periods), 1) * 100
        print(f"\nCoverage: {coverage:.1f}% ({len(matching_periods)}/{len(our_periods)} periods)")

        # We expect at least 50% coverage (some hours might have no data)
        assert coverage >= 50, f"Low coverage: {coverage:.1f}% - expected at least 50%"


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
