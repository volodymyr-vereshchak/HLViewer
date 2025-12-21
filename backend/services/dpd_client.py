"""
DPD API Client for fetching enterprise volume data.

This module provides async client for DPD API with JWT authentication,
token management, and volume data fetching.
"""

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

        # Initialize tokens on creation
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(self._authenticate())
        except RuntimeError:
            # If no event loop is running, create new one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._authenticate())
            loop.close()

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
            proxies=None
        ) as client:
            try:
                response = await client.post(self.auth_url, json=payload)
                response.raise_for_status()

                data = response.json()
                self.access_token = data["access"]
                self.refresh_token = data["refresh"]

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
            proxies=None
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

    async def get_volumes(
        self,
        devices: List[Dict],
        date_from: datetime,
        date_to: datetime,
        max_retries: int = 3
    ) -> List[Dict]:
        """
        Fetch volume data for multiple devices from DPD API.

        Args:
            devices: List of device dicts with keys: serNum, mfDev, typeDev, chNum
            date_from: Start date for data range
            date_to: End date for data range
            max_retries: Maximum number of retry attempts

        Returns:
            List of dicts with volume data, each containing:
                - serNum, mfDev, typeDev, chNum (device identifiers)
                - date (str): Date in YYYY-MM-DD format
                - dvstAlwrk (float): Daily standard volume
                - dvwrkAlwrk (float): Daily work volume
                - pressure (float): Pressure reading
                - temperature (float): Temperature reading

        Raises:
            httpx.HTTPError: If API request fails after retries
        """
        if not devices:
            logger.warning("No devices provided to get_volumes")
            return []

        endpoint = f"{self.base_url}devices/volumes"
        params = {
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to.strftime("%Y-%m-%d"),
            "page": 0,
            "size": 6000
        }

        for attempt in range(1, max_retries + 1):
            try:
                headers = {"Authorization": f"Bearer {self.access_token}"}

                async with httpx.AsyncClient(
                    verify=False,
                    timeout=self.timeout,
                    proxies=None
                ) as client:
                    response = await client.post(endpoint, headers=headers, params=params)

                    if response.status_code == 200:
                        data = response.json()
                        content = data.get("content", [])

                        # Filter only devices we requested
                        device_set = {
                            (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"])
                            for d in devices
                        }

                        filtered_data = [
                            record for record in content
                            if (
                                record.get("serNum"),
                                record.get("mfDev"),
                                record.get("typeDev"),
                                record.get("chNum")
                            ) in device_set
                        ]

                        logger.info(
                            f"Fetched {len(filtered_data)} volume records "
                            f"for {len(devices)} devices (attempt {attempt})"
                        )
                        return filtered_data

                    elif response.status_code in (401, 403):
                        logger.warning(f"Authentication failed (attempt {attempt}), refreshing tokens")
                        await self._refresh_tokens()
                        continue

                    else:
                        logger.error(
                            f"DPD API returned {response.status_code}: {response.text}"
                        )
                        if attempt == max_retries:
                            raise httpx.HTTPStatusError(
                                f"DPD API error: {response.status_code}",
                                request=response.request,
                                response=response
                            )

            except httpx.RequestError as e:
                logger.error(f"DPD API request error (attempt {attempt}): {e}")
                if attempt == max_retries:
                    raise

        return []

    async def close(self):
        """Close any resources (for cleanup)."""
        # httpx.AsyncClient is used as context manager, no cleanup needed
        pass
