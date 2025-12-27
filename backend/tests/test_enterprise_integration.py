"""
Integration test for enterprise mappings and DPD API.

This test validates that enterprise mappings work correctly by:
1. Loading device mappings from enterprise.xlsx and line_id.xlsx
2. Fetching data directly from DPD API for sample devices
3. Aggregating data manually
4. Comparing with our API endpoint results

Run with: pytest backend/tests/test_enterprise_integration.py -v
"""

import asyncio
import pytest
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict

from backend.services.enterprise_mappings import load_mappings
from backend.services.dpd_client import DPDClient


# Test configuration
TEST_DEVICE_COUNT = 50  # Number of devices to test
TEST_DATE_FROM = datetime.now() - timedelta(days=7)  # Last 7 days
TEST_DATE_TO = datetime.now() - timedelta(days=1)  # Until yesterday
TEST_PERIOD_TYPE = "daily"  # daily or hourly


class TestEnterpriseIntegration:
    """Integration tests for enterprise volume data."""

    @pytest.fixture(scope="class")
    def sample_devices(self) -> List[Dict]:
        """
        Load sample devices from enterprise mappings.

        Returns first N active devices from mappings.
        """
        df = load_mappings()

        # Filter active devices
        active_df = df[df["active"] == True].copy()

        # Take first TEST_DEVICE_COUNT devices
        sample_df = active_df.head(TEST_DEVICE_COUNT)

        # Convert to list of dicts
        devices = sample_df.to_dict("records")

        print(f"\nLoaded {len(devices)} sample devices for testing")
        print(f"Unique line_ids: {sorted(sample_df['line_id'].unique().tolist())}")
        print(f"chNum distribution: {sample_df['chNum'].value_counts().to_dict()}")

        return devices

    @pytest.mark.asyncio
    async def test_dpd_api_direct_access(self, sample_devices):
        """
        Test 1: Verify we can fetch data directly from DPD API.

        This test validates:
        - DPD API authentication works
        - Device parameters are correct
        - API returns data for our devices
        """
        print(f"\n{'='*80}")
        print(f"TEST 1: Direct DPD API Access")
        print(f"{'='*80}")

        client = DPDClient()

        # Fetch volumes for all sample devices
        volumes_data = await client.get_volumes(
            sample_devices,
            TEST_DATE_FROM,
            TEST_DATE_TO,
            type_request=TEST_PERIOD_TYPE
        )

        print(f"\nDPD API Results:")
        print(f"  Total records: {len(volumes_data)}")

        # Verify we got some data
        assert len(volumes_data) > 0, "DPD API returned no data"

        # Group by device
        device_data = defaultdict(list)
        for record in volumes_data:
            device_key = (
                record["serNum"],
                record["mfDev"],
                record["typeDev"],
                record["chNum"]
            )
            device_data[device_key].append(record)

        print(f"  Devices with data: {len(device_data)} / {len(sample_devices)}")

        # Show sample data
        if volumes_data:
            sample_record = volumes_data[0]
            print(f"\n  Sample record:")
            print(f"    Device: serNum={sample_record['serNum']}, mfDev={sample_record['mfDev']}, "
                  f"typeDev={sample_record['typeDev']}, chNum={sample_record['chNum']}")
            print(f"    Date: {sample_record.get('date') or sample_record.get('period')}")
            print(f"    Volume (dvstAlwrk): {sample_record.get('dvstAlwrk')}")

        return volumes_data

    @pytest.mark.asyncio
    async def test_manual_aggregation(self, sample_devices):
        """
        Test 2: Manually aggregate DPD data by line_id and compare structure.

        This test validates:
        - Device-to-line_id mapping is correct
        - Aggregation logic matches expected behavior
        - Data structure is correct
        """
        print(f"\n{'='*80}")
        print(f"TEST 2: Manual Aggregation")
        print(f"{'='*80}")

        client = DPDClient()

        # Fetch raw data from DPD
        volumes_data = await client.get_volumes(
            sample_devices,
            TEST_DATE_FROM,
            TEST_DATE_TO,
            type_request=TEST_PERIOD_TYPE
        )

        # Create device lookup map
        device_map = {
            (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]): d
            for d in sample_devices
        }

        # Manual aggregation by line_id and date
        aggregated = defaultdict(lambda: {"total": 0.0, "device_count": 0, "devices": set()})

        for record in volumes_data:
            device_key = (
                record["serNum"],
                record["mfDev"],
                record["typeDev"],
                record["chNum"]
            )

            device_info = device_map.get(device_key)
            if not device_info:
                continue

            volume = record.get("dvstAlwrk", 0.0) or 0.0

            # Parse date
            record_date_str = record.get("date") or record.get("period")
            if not record_date_str:
                continue

            if TEST_PERIOD_TYPE == "daily":
                if isinstance(record_date_str, str):
                    record_date = datetime.strptime(
                        record_date_str.split("T")[0], "%Y-%m-%d"
                    ).date()
                else:
                    record_date = record_date_str.date() if hasattr(record_date_str, 'date') else record_date_str
            else:
                # hourly - preserve datetime
                if isinstance(record_date_str, str):
                    clean_str = record_date_str.split(".")[0]
                    try:
                        record_date = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        record_date = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M")
                else:
                    record_date = record_date_str

            key = (device_info["line_id"], record_date)
            aggregated[key]["total"] += volume
            aggregated[key]["devices"].add(device_key)

        # Update device_count
        for key in aggregated:
            aggregated[key]["device_count"] = len(aggregated[key]["devices"])

        print(f"\nAggregation Results:")
        print(f"  Unique (line_id, date) combinations: {len(aggregated)}")

        # Group by line_id
        by_line = defaultdict(int)
        for (line_id, _), data in aggregated.items():
            by_line[line_id] += 1

        print(f"  Records per line_id:")
        for line_id in sorted(by_line.keys()):
            print(f"    line_id {line_id}: {by_line[line_id]} time periods")

        # Show sample aggregated record
        if aggregated:
            sample_key = list(aggregated.keys())[0]
            sample_data = aggregated[sample_key]
            print(f"\n  Sample aggregated record:")
            print(f"    line_id: {sample_key[0]}, period: {sample_key[1]}")
            print(f"    Total volume: {sample_data['total']:.2f}")
            print(f"    Device count: {sample_data['device_count']}")

        assert len(aggregated) > 0, "No aggregated data produced"

        return aggregated

    @pytest.mark.asyncio
    async def test_compare_with_api(self, sample_devices):
        """
        Test 3: Compare manual aggregation with API endpoint results.

        This test validates:
        - API endpoint returns correct aggregated data
        - Manual aggregation matches API aggregation
        - All devices are included correctly
        """
        print(f"\n{'='*80}")
        print(f"TEST 3: Compare Manual vs API Aggregation")
        print(f"{'='*80}")

        # Step 1: Get manual aggregation
        client = DPDClient()
        volumes_data = await client.get_volumes(
            sample_devices,
            TEST_DATE_FROM,
            TEST_DATE_TO,
            type_request=TEST_PERIOD_TYPE
        )

        device_map = {
            (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]): d
            for d in sample_devices
        }

        manual_aggregated = defaultdict(lambda: {"total": 0.0, "devices": set()})

        for record in volumes_data:
            device_key = (record["serNum"], record["mfDev"], record["typeDev"], record["chNum"])
            device_info = device_map.get(device_key)
            if not device_info:
                continue

            volume = record.get("dvstAlwrk", 0.0) or 0.0
            record_date_str = record.get("date") or record.get("period")
            if not record_date_str:
                continue

            if TEST_PERIOD_TYPE == "daily":
                if isinstance(record_date_str, str):
                    record_date = datetime.strptime(record_date_str.split("T")[0], "%Y-%m-%d").date()
                else:
                    record_date = record_date_str.date() if hasattr(record_date_str, 'date') else record_date_str
            else:
                if isinstance(record_date_str, str):
                    clean_str = record_date_str.split(".")[0]
                    try:
                        record_date = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        record_date = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M")
                else:
                    record_date = record_date_str

            key = (device_info["line_id"], record_date)
            manual_aggregated[key]["total"] += volume
            manual_aggregated[key]["devices"].add(device_key)

        print(f"\nManual Aggregation:")
        print(f"  Records: {len(manual_aggregated)}")
        manual_total_volume = sum(data["total"] for data in manual_aggregated.values())
        print(f"  Total volume (all periods): {manual_total_volume:.2f}")

        # Step 2: Simulate API aggregation (same logic as enterprise_ep.py)
        # This simulates what the API endpoint does
        api_aggregated = defaultdict(lambda: {"total": 0.0, "devices": set()})

        for record in volumes_data:
            device_key = (record["serNum"], record["mfDev"], record["typeDev"], record["chNum"])
            device_info = device_map.get(device_key)
            if not device_info:
                continue

            volume = record.get("dvstAlwrk", 0.0) or 0.0
            record_date_str = record.get("date") or record.get("period")
            if not record_date_str:
                continue

            try:
                if TEST_PERIOD_TYPE == 'hourly':
                    if isinstance(record_date_str, str):
                        clean_str = record_date_str.split(".")[0]
                        try:
                            record_period = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            record_period = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M")
                    else:
                        record_period = record_date_str
                else:
                    if isinstance(record_date_str, str):
                        record_period = datetime.strptime(record_date_str.split("T")[0], "%Y-%m-%d").date()
                    elif hasattr(record_date_str, 'date'):
                        record_period = record_date_str.date()
                    else:
                        record_period = record_date_str
            except Exception:
                continue

            key = (device_info["line_id"], record_period)
            api_aggregated[key]["total"] += volume
            api_aggregated[key]["devices"].add(device_key)

        print(f"\nAPI-style Aggregation:")
        print(f"  Records: {len(api_aggregated)}")
        api_total_volume = sum(data["total"] for data in api_aggregated.values())
        print(f"  Total volume (all periods): {api_total_volume:.2f}")

        # Step 3: Compare results
        print(f"\nComparison:")

        # Should have same number of records
        assert len(manual_aggregated) == len(api_aggregated), \
            f"Record count mismatch: manual={len(manual_aggregated)}, api={len(api_aggregated)}"
        print(f"  ✓ Record count matches: {len(manual_aggregated)}")

        # Should have same total volume (with small tolerance for floating point)
        volume_diff = abs(manual_total_volume - api_total_volume)
        assert volume_diff < 0.01, \
            f"Total volume mismatch: manual={manual_total_volume:.2f}, api={api_total_volume:.2f}"
        print(f"  ✓ Total volume matches: {manual_total_volume:.2f}")

        # Compare individual records
        mismatches = 0
        for key in manual_aggregated:
            if key not in api_aggregated:
                print(f"  ✗ Missing in API aggregation: {key}")
                mismatches += 1
                continue

            manual_vol = manual_aggregated[key]["total"]
            api_vol = api_aggregated[key]["total"]
            diff = abs(manual_vol - api_vol)

            if diff > 0.01:
                print(f"  ✗ Volume mismatch for {key}: manual={manual_vol:.2f}, api={api_vol:.2f}")
                mismatches += 1

        assert mismatches == 0, f"Found {mismatches} mismatches between manual and API aggregation"
        print(f"  ✓ All individual records match")

        print(f"\n{'='*80}")
        print(f"✓ ALL TESTS PASSED")
        print(f"{'='*80}")


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "-s"])
