"""
Standalone tests for DPD API Client

Run these tests to verify DPD API connectivity without backend API:
    pytest tests/test_dpd_client.py -v
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from backend.services.dpd_client import DPDClient


class TestDPDClientStandalone:
    """Standalone tests for DPD API client."""

    def test_dpd_client_initialization(self):
        """Test that DPDClient can be initialized."""
        try:
            client = DPDClient()
            assert client is not None, "DPDClient should initialize"
            assert client.base_url is not None, "base_url should be set"
            assert client.auth_url is not None, "auth_url should be set"
            print(f"✓ DPDClient initialized successfully")
            print(f"  Base URL: {client.base_url}")
            print(f"  Auth URL: {client.auth_url}")
        except Exception as e:
            pytest.fail(f"DPDClient initialization failed: {e}")

    def test_dpd_client_lazy_authentication(self):
        """Test that DPDClient uses lazy authentication (no tokens until first call)."""
        client = DPDClient()

        # Tokens should be None after init (lazy authentication)
        assert client.access_token is None, "access_token should be None after init (lazy auth)"
        assert client.refresh_token is None, "refresh_token should be None after init (lazy auth)"
        assert client._authenticated is False, "_authenticated flag should be False"

        print(f"✓ Lazy authentication verified")
        print(f"  Tokens not acquired until first API call (as expected)")

    @pytest.mark.asyncio
    async def test_dpd_authentication(self):
        """Test DPD API authentication."""
        client = DPDClient()

        # Re-authenticate to test the method
        await client._authenticate()

        assert client.access_token is not None, "access_token should be set"
        assert client.refresh_token is not None, "refresh_token should be set"

        print(f"✓ Authentication successful")

    @pytest.mark.asyncio
    async def test_dpd_get_volumes_empty_devices(self):
        """Test get_volumes with empty device list."""
        client = DPDClient()

        date_from = datetime.now() - timedelta(days=7)
        date_to = datetime.now()

        result = await client.get_volumes([], date_from, date_to)

        assert result == [], "Should return empty list for empty devices"
        print(f"✓ Empty devices handled correctly")

    @pytest.mark.asyncio
    async def test_dpd_get_volumes_with_test_device(self):
        """Test get_volumes with a test device."""
        client = DPDClient()

        # Use test device from enterprise_mappings.xlsx
        test_devices = [
            {
                "serNum": 8189,
                "mfDev": 1,
                "typeDev": 5,
                "chNum": 0
            }
        ]

        # Request last 60 days to increase chances of finding data
        date_from = datetime.now() - timedelta(days=60)
        date_to = datetime.now()

        try:
            result = await client.get_volumes(test_devices, date_from, date_to)

            print(f"✓ get_volumes executed successfully")
            print(f"  Devices requested: {len(test_devices)}")
            print(f"  Records returned: {len(result)}")

            if result:
                print(f"  Sample record keys: {list(result[0].keys())}")
                if "dvstAlwrk" in result[0]:
                    print(f"  Sample volume: {result[0]['dvstAlwrk']}")

            assert isinstance(result, list), "Result should be a list"

        except Exception as e:
            pytest.fail(f"get_volumes failed: {e}")

    @pytest.mark.asyncio
    async def test_dpd_get_volumes_multiple_devices(self):
        """Test get_volumes with multiple test devices."""
        client = DPDClient()

        # Use multiple REAL devices from all.csv
        test_devices = [
            {"serNum": 8189, "mfDev": 1, "typeDev": 5, "chNum": 0},   # Автосистем Дп ТОВ
            {"serNum": 21390, "mfDev": 1, "typeDev": 15, "chNum": 0}, # Агрокомплекс-2012 ТОВ
            {"serNum": 8480, "mfDev": 1, "typeDev": 5, "chNum": 0}    # Агрошляхбуд ТОВ
        ]

        # Request last 60 days to increase chances of finding data
        date_from = datetime.now() - timedelta(days=60)
        date_to = datetime.now()

        try:
            result = await client.get_volumes(test_devices, date_from, date_to)

            print(f"✓ Multiple devices query executed")
            print(f"  Devices requested: {len(test_devices)}")
            print(f"  Records returned: {len(result)}")

            assert isinstance(result, list), "Result should be a list"

            # Check that results contain expected fields
            if result:
                required_fields = ["serNum", "mfDev", "typeDev", "chNum", "date"]
                for field in required_fields:
                    assert field in result[0], f"Result should contain {field}"

                print(f"  ✓ All required fields present")

        except Exception as e:
            pytest.fail(f"Multiple devices query failed: {e}")


class TestDPDClientErrorHandling:
    """Test error handling in DPD client."""

    @pytest.mark.asyncio
    async def test_dpd_token_refresh(self):
        """Test token refresh mechanism."""
        client = DPDClient()

        old_access_token = client.access_token

        # Test refresh
        await client._refresh_tokens()

        print(f"✓ Token refresh executed")
        print(f"  Old token length: {len(old_access_token) if old_access_token else 0}")
        print(f"  New token length: {len(client.access_token) if client.access_token else 0}")

        assert client.access_token is not None, "New access token should be set"

    @pytest.mark.asyncio
    async def test_dpd_invalid_date_range(self):
        """Test handling of invalid date range."""
        client = DPDClient()

        # Future dates - may have no data
        date_from = datetime.now() + timedelta(days=365)
        date_to = datetime.now() + timedelta(days=366)

        test_devices = [
            {"serNum": 8189, "mfDev": 1, "typeDev": 5, "chNum": 0}
        ]

        result = await client.get_volumes(test_devices, date_from, date_to)

        # Should not raise exception, just return empty or partial data
        assert isinstance(result, list), "Should return list even for future dates"
        print(f"✓ Future date range handled gracefully")
        print(f"  Records returned: {len(result)}")


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short", "-s"])
