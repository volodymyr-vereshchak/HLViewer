"""
Diagnostic tests for DPD API - debug why no data is returned

Run with: pytest tests/test_dpd_diagnostic.py -v -s
"""

import pytest
import asyncio
from datetime import datetime
from backend.services.dpd_client import DPDClient


class TestDPDDiagnostic:
    """Diagnostic tests to debug DPD API responses."""

    @pytest.mark.asyncio
    async def test_single_device_requests(self):
        """Test each device individually to see which ones return data."""
        client = DPDClient()

        # All 6 devices from enterprise_mappings.csv
        all_devices = [
            {"serNum": 21390, "mfDev": 1, "typeDev": 15, "chNum": 0, "name": "Агрокомплекс-2012 ТОВ"},
            {"serNum": 8480, "mfDev": 1, "typeDev": 5, "chNum": 0, "name": "Агрошляхбуд ТОВ"},
            {"serNum": 8189, "mfDev": 1, "typeDev": 5, "chNum": 0, "name": "Автосистем Дп ТОВ"},
            {"serNum": 7559, "mfDev": 1, "typeDev": 5, "chNum": 0, "name": "Асканія Бевериджиз ТОВ"},
            {"serNum": 5989, "mfDev": 1, "typeDev": 5, "chNum": 0, "name": "Асканія Бевериджиз ТОВ"},
            {"serNum": 6437, "mfDev": 1, "typeDev": 5, "chNum": 0, "name": "Баском ПП"}
        ]

        # Test August 2025
        date_from = datetime(2025, 8, 20)
        date_to = datetime(2025, 8, 27)

        print("\n" + "=" * 80)
        print(f"Testing period: {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}")
        print("=" * 80)

        total_records = 0

        for device in all_devices:
            name = device.pop("name")  # Remove name before sending to API

            print(f"\n[{device['serNum']}] {name}")
            print(f"  Request: serNum={device['serNum']}, mfDev={device['mfDev']}, "
                  f"typeDev={device['typeDev']}, chNum={device['chNum']}")

            try:
                result = await client.get_volumes([device], date_from, date_to)

                print(f"  Response: {len(result)} records")

                if result:
                    total_records += len(result)
                    # Show first record as sample
                    sample = result[0]
                    print(f"  Sample record:")
                    print(f"    Date: {sample.get('date')}")
                    print(f"    dvstAlwrk: {sample.get('dvstAlwrk')}")
                    print(f"    dvwrkAlwrk: {sample.get('dvwrkAlwrk')}")
                else:
                    print(f"  ⚠️ No data for this device in this period")

            except Exception as e:
                print(f"  ❌ Error: {e}")

        print("\n" + "=" * 80)
        print(f"TOTAL RECORDS: {total_records}")
        print("=" * 80)

    @pytest.mark.asyncio
    async def test_different_date_ranges(self):
        """Test different date ranges to find when data exists."""
        client = DPDClient()

        # Use one known device
        test_device = [{"serNum": 8189, "mfDev": 1, "typeDev": 5, "chNum": 0}]

        date_ranges = [
            ("2025-08-01", "2025-08-07", "Early August 2025"),
            ("2025-08-20", "2025-08-27", "Late August 2025"),
            ("2025-07-01", "2025-07-07", "July 2025"),
            ("2025-06-01", "2025-06-07", "June 2025"),
            ("2024-12-01", "2024-12-07", "December 2024"),
            ("2024-11-01", "2024-11-07", "November 2024"),
        ]

        print("\n" + "=" * 80)
        print(f"Testing device serNum=8189 (Автосистем Дп ТОВ)")
        print("=" * 80)

        for date_from_str, date_to_str, description in date_ranges:
            date_from = datetime.strptime(date_from_str, "%Y-%m-%d")
            date_to = datetime.strptime(date_to_str, "%Y-%m-%d")

            print(f"\n{description}: {date_from_str} to {date_to_str}")

            try:
                result = await client.get_volumes(test_device, date_from, date_to)

                if result:
                    print(f"  ✓ Found {len(result)} records")
                    # Show date range of returned data
                    dates = [r.get('date') for r in result]
                    print(f"  Date range: {min(dates)} to {max(dates)}")
                else:
                    print(f"  ⚠️ No data")

            except Exception as e:
                print(f"  ❌ Error: {e}")

    @pytest.mark.asyncio
    async def test_raw_api_response(self):
        """Test to see the raw API response structure from indications endpoint."""
        client = DPDClient()

        await client._authenticate()

        test_device = {"serNum": 8189, "mfDev": 1, "typeDev": 5, "chNum": 0}
        date_from = datetime(2025, 8, 20)
        date_to = datetime(2025, 8, 27)

        endpoint = f"{client.base_url}indications"
        params = {
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to.strftime("%Y-%m-%d"),
            "serNUM": test_device["serNum"],
            "mfDEV": test_device["mfDev"],
            "typeDEV": test_device["typeDev"],
            "chNUM": test_device["chNum"],
            "typeRequest": "daily"
        }

        print("\n" + "=" * 80)
        print("RAW DPD API REQUEST (indications endpoint):")
        print("=" * 80)
        print(f"URL: {endpoint}")
        print(f"Method: GET")
        print(f"Params: {params}")

        import httpx

        headers = {"Authorization": f"Bearer {client.access_token}"}

        async with httpx.AsyncClient(verify=False, timeout=30.0, trust_env=False) as http_client:
            response = await http_client.get(endpoint, headers=headers, params=params)

            print(f"\nRESPONSE:")
            print(f"Status: {response.status_code}")
            print(f"Headers: {dict(response.headers)}")

            if response.status_code == 200:
                data = response.json()
                print(f"\nResponse structure:")
                print(f"  Type: {type(data)}")
                print(f"  Keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")

                if isinstance(data, dict):
                    # indications endpoint returns {"table": {"data": [...]}}
                    table_data = data.get("table", {}).get("data", [])
                    print(f"  table.data length: {len(table_data)}")

                    if table_data:
                        print(f"\n  First record:")
                        print(f"    {table_data[0]}")

                        # Check for None values in dvstAlwrk
                        none_count = sum(1 for r in table_data if r.get("dvstAlwrk") is None)
                        print(f"\n  Records with dvstAlwrk=None: {none_count}/{len(table_data)}")
                    else:
                        print(f"\n  table.data is empty!")
                        print(f"\n  Full response:")
                        print(f"    {data}")
                else:
                    print(f"  Response is not a dict!")
                    print(f"  {data}")
            else:
                print(f"Error: {response.text}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
