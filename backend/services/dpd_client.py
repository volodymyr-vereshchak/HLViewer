"""
DPD API Client for fetching enterprise volume data.

This module provides async client for DPD API with JWT authentication,
token management, and volume data fetching.
"""

import asyncio
import httpx
import logging
import warnings
from datetime import datetime
from typing import List, Dict, Optional
from backend.settings import backend_settings

# Disable SSL warnings (DPD API uses self-signed certificates)
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

logger = logging.getLogger(__name__)


class DPDClient:
    """Async HTTP client for DPD API with JWT authentication."""

    def __init__(self):
        self.base_url = backend_settings["DPD_API_BASE_URL"]
        self.auth_url = backend_settings["DPD_AUTH_URL"]
        self.username = backend_settings["DPD_USERNAME"]
        self.password = backend_settings["DPD_PASSWORD"]
        self.timeout = backend_settings["DPD_TIMEOUT"]

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._authenticated: bool = False

    async def _authenticate(self):
        """Authenticate with DPD API and get JWT tokens."""
        payload = {
            "login": self.username,
            "password": self.password
        }

        # Disable proxy and SSL verification for corporate network
        async with httpx.AsyncClient(
            verify=False,
            timeout=self.timeout,
            trust_env=False  # Ignore HTTP_PROXY, HTTPS_PROXY env vars
        ) as client:
            try:
                response = await client.post(self.auth_url, json=payload)
                response.raise_for_status()

                data = response.json()
                self.access_token = data["access"]
                self.refresh_token = data["refresh"]
                self._authenticated = True

                logger.info("DPD API authentication successful")

            except httpx.HTTPStatusError as e:
                logger.error(f"DPD API authentication failed: {e}")
                raise
            except Exception as e:
                logger.error(f"DPD API authentication error: {e}")
                raise

    async def _refresh_tokens(self):
        """Refresh access token using refresh token."""
        headers = {"Authorization": f"Bearer {self.refresh_token}"}
        refresh_url = f"{self.base_url}refreshToken"

        async with httpx.AsyncClient(
            verify=False,
            timeout=self.timeout,
            trust_env=False
        ) as client:
            try:
                response = await client.post(refresh_url, headers=headers)
                response.raise_for_status()

                data = response.json()
                self.access_token = data["access"]
                self.refresh_token = data["refresh"]

                logger.info("DPD API tokens refreshed")

            except Exception as e:
                logger.warning(f"Token refresh failed, re-authenticating: {e}")
                await self._authenticate()

    async def _get_device_indications(
        self,
        device: Dict,
        date_from: datetime,
        date_to: datetime,
        max_retries: int = 3
    ) -> List[Dict]:
        """
        Fetch daily indications for a single device from DPD API.

        Args:
            device: Device dict with keys: serNum, mfDev, typeDev, chNum
            date_from: Start date for data range
            date_to: End date for data range
            max_retries: Maximum number of retry attempts

        Returns:
            List of indication records for this device.
            Each record contains:
                - date (str): Date in YYYY-MM-DD format
                - dvstAlwrk (float or None): Daily standard volume
                - dvwrkAlwrk (float or None): Daily work volume
                - pressure (float): Pressure reading
                - temperature (float): Temperature reading
                - serNum, mfDev, typeDev, chNum (device identifiers)

            Returns empty list if device request fails after retries.
        """
        endpoint = f"{self.base_url}indications"
        params = {
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to.strftime("%Y-%m-%d"),
            "serNUM": device["serNum"],
            "mfDEV": device["mfDev"],
            "typeDEV": device["typeDev"],
            "chNUM": device["chNum"],
            "typeRequest": "daily"
        }

        for attempt in range(1, max_retries + 1):
            try:
                headers = {"Authorization": f"Bearer {self.access_token}"}

                async with httpx.AsyncClient(
                    verify=False,
                    timeout=self.timeout,
                    trust_env=False
                ) as client:
                    response = await client.get(endpoint, headers=headers, params=params)

                    if response.status_code == 200:
                        data = response.json()
                        result = data.get("table", {}).get("data", [])

                        logger.debug(
                            f"Device {device['serNum']}: {len(result)} records "
                            f"(attempt {attempt})"
                        )

                        return result

                    elif response.status_code in (401, 403):
                        logger.warning(
                            f"Auth failed for device {device['serNum']} "
                            f"(attempt {attempt}), refreshing tokens"
                        )
                        await self._refresh_tokens()
                        continue

                    else:
                        logger.error(
                            f"DPD API error for device {device['serNum']}: "
                            f"{response.status_code} - {response.text}"
                        )
                        if attempt == max_retries:
                            logger.warning(
                                f"Failed to fetch data for device {device['serNum']} "
                                f"after {max_retries} attempts"
                            )
                            return []

            except httpx.RequestError as e:
                logger.error(
                    f"Request error for device {device['serNum']} "
                    f"(attempt {attempt}): {e}"
                )
                if attempt == max_retries:
                    logger.warning(
                        f"Failed to fetch data for device {device['serNum']} "
                        f"after {max_retries} attempts due to network error"
                    )
                    return []

        return []

    async def get_volumes(
        self,
        devices: List[Dict],
        date_from: datetime,
        date_to: datetime,
        max_retries: int = 3
    ) -> List[Dict]:
        """
        Fetch volume data for multiple devices from DPD API.

        Uses parallel requests to the indications endpoint, one per device.
        Returns all records including those where dvstAlwrk is None.

        Args:
            devices: List of device dicts with keys: serNum, mfDev, typeDev, chNum
            date_from: Start date for data range
            date_to: End date for data range
            max_retries: Maximum number of retry attempts per device

        Returns:
            List of dicts with volume data, each containing:
                - serNum, mfDev, typeDev, chNum (device identifiers)
                - date (str): Date in YYYY-MM-DD format
                - dvstAlwrk (float or None): Daily standard volume
                - dvwrkAlwrk (float or None): Daily work volume
                - pressure (float): Pressure reading
                - temperature (float): Temperature reading

        Notes:
            - Uses indications endpoint (GET) instead of devices/volumes (POST)
            - Makes one request per device in parallel using asyncio.gather()
            - Returns partial results if some devices fail (no exception raised)
        """
        # Lazy authentication on first call
        if not self._authenticated:
            await self._authenticate()

        if not devices:
            logger.warning("No devices provided to get_volumes")
            return []

        logger.info(
            f"Fetching volumes for {len(devices)} devices "
            f"from {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}"
        )

        # Create parallel tasks for each device
        tasks = [
            self._get_device_indications(device, date_from, date_to, max_retries)
            for device in devices
        ]

        # Execute all requests in parallel
        # return_exceptions=True prevents one failure from stopping others
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Aggregate results from all devices
        all_records = []
        successful_devices = 0
        failed_devices = 0

        for device, result in zip(devices, results):
            if isinstance(result, Exception):
                # This shouldn't happen due to error handling in _get_device_indications,
                # but handle it just in case
                logger.error(
                    f"Unexpected exception for device {device['serNum']}: {result}"
                )
                failed_devices += 1
                continue

            if result:
                all_records.extend(result)
                successful_devices += 1
            else:
                # Empty result - device had no data or request failed
                failed_devices += 1

        logger.info(
            f"Fetched {len(all_records)} volume records total: "
            f"{successful_devices} devices succeeded, {failed_devices} failed/empty"
        )

        return all_records

    async def close(self):
        """Close any resources (for cleanup)."""
        # httpx.AsyncClient is used as context manager, no cleanup needed
        pass
