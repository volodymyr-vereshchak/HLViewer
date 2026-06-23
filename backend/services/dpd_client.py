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

# Disable SSL warnings (DPD API uses self-signed certificates)
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

logger = logging.getLogger(__name__)


class DPDClient:
    """Async HTTP client for DPD API with JWT authentication."""

    def __init__(
        self,
        *,
        base_url: str,
        auth_url: str,
        username: str,
        password: str,
        timeout: int,
    ):
        # Endpoints are built by suffixing this (e.g. f"{base_url}indications"),
        # so guarantee exactly one trailing slash. A credential saved as
        # ".../api/v1" (no slash) would otherwise produce ".../api/v1indications"
        # → 403 from DPD. Normalizing here makes the trailing slash irrelevant.
        self.base_url = base_url.rstrip("/") + "/"
        self.auth_url = auth_url
        self.username = username
        self.password = password
        self.timeout = timeout

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._authenticated: bool = False

    @classmethod
    async def for_branch(cls, branch_id: int, session) -> "DPDClient":
        """Load credentials from grmu_branch_dpd_credential. Raises if not configured."""
        from sqlmodel import select
        from backend.db.models.grmu_branch_model import GrmuBranchDpdCredential

        stmt = select(GrmuBranchDpdCredential).where(
            GrmuBranchDpdCredential.branch_id == branch_id
        )
        result = await session.execute(stmt)
        cred = result.scalars().first()

        if not cred:
            raise ValueError(
                f"No DPD credentials configured for branch_id={branch_id}."
            )
        if not cred.api_base_url or not cred.auth_url:
            raise ValueError(
                f"DPD credentials for branch_id={branch_id} are missing api_base_url or auth_url."
            )

        logger.debug("Loaded DPD credentials from DB for branch_id=%d", branch_id)
        return cls(
            base_url=cred.api_base_url,
            auth_url=cred.auth_url,
            username=cred.username,
            password=cred.password,
            timeout=cred.timeout_sec,
        )

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
        type_request: str = "daily",
        max_retries: int = 3
    ) -> List[Dict]:
        """
        Fetch indications for a single device from DPD API.

        Args:
            device: Device dict with keys: serNum, mfDev, typeDev, chNum
            date_from: Start date for data range
            date_to: End date for data range
            type_request: Request type - "daily" or "hourly"
            max_retries: Maximum number of retry attempts

        Returns:
            List of indication records for this device.
            Each record contains:
                - date (str): Date in YYYY-MM-DD format (daily) or datetime (hourly)
                - dvstAlwrk (float or None): Daily/hourly standard volume
                - dvwrkAlwrk (float or None): Daily/hourly work volume
                - press (float): Pressure reading
                - temper (float): Temperature reading
                - serNum, mfDev, typeDev, chNum (device identifiers)

            Returns empty list if device request fails after retries.
        """
        endpoint = f"{self.base_url}indications"

        # Format dates - DPD API accepts only date format (YYYY-MM-DD) for both daily and hourly
        # The typeRequest parameter determines the granularity, not the date format
        date_from_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")

        params = {
            "from": date_from_str,
            "to": date_to_str,
            "serNUM": device["serNum"],
            "mfDEV": device["mfDev"],
            "typeDEV": device["typeDev"],
            "chNUM": device["chNum"],
            "typeRequest": type_request
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

                        # Add device identifiers to each record for aggregation
                        # API doesn't return device info, so we add it from request
                        for record in result:
                            record["serNum"] = device["serNum"]
                            record["mfDev"] = device["mfDev"]
                            record["typeDev"] = device["typeDev"]
                            record["chNum"] = device["chNum"]

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
        type_request: str = "daily",
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
            type_request: Request type - "daily" (default) or "hourly"
            max_retries: Maximum number of retry attempts per device

        Returns:
            List of dicts with volume data, each containing:
                - serNum, mfDev, typeDev, chNum (device identifiers)
                - date (str): Date (daily) or datetime (hourly) in YYYY-MM-DD format
                - dvstAlwrk (float or None): Daily/hourly standard volume
                - dvwrkAlwrk (float or None): Daily/hourly work volume
                - press (float): Pressure reading (mapped to pressure in internal API)
                - temper (float): Temperature reading (mapped to temperature in internal API)

        Notes:
            - Uses indications endpoint (GET) instead of devices/volumes (POST)
            - Makes one request per device in parallel using asyncio.gather()
            - Returns partial results if some devices fail (no exception raised)
            - Supports both daily and hourly data via type_request parameter
        """
        # Lazy authentication on first call
        if not self._authenticated:
            await self._authenticate()

        if not devices:
            logger.warning("No devices provided to get_volumes")
            return []

        logger.info(
            f"Fetching {type_request} volumes for {len(devices)} devices "
            f"from {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}"
        )

        # Create parallel tasks for each device
        tasks = [
            self._get_device_indications(device, date_from, date_to, type_request, max_retries)
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
