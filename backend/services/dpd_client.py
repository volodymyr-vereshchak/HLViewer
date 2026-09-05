"""
DPD API Client for fetching enterprise volume data.

This module provides async client for DPD API with JWT authentication,
token management, and volume data fetching.
"""

import asyncio
import httpx
import logging
import warnings
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from backend.settings import backend_settings

# Disable SSL warnings (DPD API uses self-signed certificates)
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

logger = logging.getLogger(__name__)

# Re-login attempts allowed per page request before the status is believed.
MAX_RELOGINS = 2

# Rows per page of the event endpoints.
#
# Measured against DPD, a month of accidents for one branch: the list endpoint
# takes ~6s whether it is asked for 500 rows or 5000, because the cost is the
# query and not the transfer. At 500 the 2847-device answer came back in six
# sequential pages — 35s — and at 3000 it came back whole in one, at 6s. The
# per-device detail behaves the same: 954 rows arrive in 0.26s as one page and
# in two round trips as two.
#
# The loop still pages, so this is a hint and not a limit; it is only sized so
# that the common answer fits in a single request.
EVENT_PAGE_SIZE = 5000


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
        # base_url is a prefix that endpoint names are suffixed onto (e.g.
        # f"{base_url}indications"), so it MUST end with exactly one slash. A
        # credential saved as ".../api/v1" (no slash) would otherwise produce
        # ".../api/v1indications" → 403 from DPD.
        self.base_url = base_url.rstrip("/") + "/"
        # auth_url is used as a complete endpoint URL (client.post(self.auth_url)),
        # so it must NOT have a trailing slash — ".../auth/login/" can return
        # 404/403. Strip any accidental trailing slash from the credential.
        self.auth_url = auth_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

        # Only the access token is kept: the login response also carries a
        # "refresh" token, but DPD's refresh endpoint is not reachable from
        # this client (auth lives on auth-direct, data on rest-direct), so an
        # expired token is recovered with a plain re-login instead.
        self.access_token: Optional[str] = None
        self._authenticated: bool = False
        # One pooled httpx client per request (created in get_volumes, closed in
        # its finally). The pool caps concurrency and reuses keep-alive
        # connections across all device sub-requests + auth calls.
        self._client: Optional[httpx.AsyncClient] = None

    def _build_client(self) -> httpx.AsyncClient:
        """Create the shared pooled client for this request.

        max_connections bounds simultaneous DPD requests; pool timeout is
        disabled (None) so device tasks queued behind the pool wait for a free
        connection instead of raising PoolTimeout on large fan-outs.
        """
        max_conn = backend_settings["DPD_MAX_CONCURRENCY"]
        return httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(self.timeout, pool=None),
            trust_env=False,
            limits=httpx.Limits(
                max_connections=max_conn,
                max_keepalive_connections=max_conn,
            ),
        )

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

        # Reuse the request's pooled client (disable proxy/SSL for corp network).
        client = self._client
        try:
            response = await client.post(self.auth_url, json=payload)
            response.raise_for_status()

            data = response.json()
            self.access_token = data["access"]
            self._authenticated = True

            logger.info("DPD API authentication successful")

        except httpx.HTTPStatusError as e:
            logger.error(f"DPD API authentication failed: {e}")
            raise
        except Exception as e:
            logger.error(f"DPD API authentication error: {e}")
            raise

    async def _get_device_indications(
        self,
        client: httpx.AsyncClient,
        device: Dict,
        date_from: datetime,
        date_to: datetime,
        type_request: str = "daily",
        max_retries: int = 3,
        not_found: Optional[List] = None,
    ) -> List[Dict]:
        """
        Fetch indications for a single device from DPD API.

        Args:
            client: Pooled httpx client shared by the whole get_volumes call.
                Passed explicitly (not read from self._client) so a device task
                that wakes up after the request was cancelled still holds a
                valid reference instead of crashing on self._client = None.
            device: Device dict with keys: serNum, mfDev, typeDev, chNum
            date_from: Start date for data range
            date_to: End date for data range
            type_request: Request type - "daily" or "hourly"
            max_retries: Maximum number of retry attempts
            not_found: Shared list collecting serNums the API answered 404 for,
                summarized once per batch by get_volumes.

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

        # DPD's from/to accept only dates (YYYY-MM-DD); typeRequest sets the
        # granularity. For hourly data the range is aligned to commercial days
        # on both ends: from=D returns records starting at D 07:00 and to=D
        # returns records through D+1 06:00. Map each datetime bound to the
        # commercial date containing it (stamps before CONTRACT_HOUR belong to
        # the previous commercial day), so a span 01.07 03:00..08.07 06:00
        # asks from=30.06&to=07.07.
        if type_request == "hourly":
            contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
            if date_from.hour < contract_hour:
                date_from = date_from - timedelta(days=1)
            if date_to.hour < contract_hour:
                date_to = date_to - timedelta(days=1)
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
                        # Caller's opaque handle, echoed back so records can be
                        # attributed without a reverse lookup by identity. The
                        # quadruple is no longer unique per caller-side row:
                        # one corrector can be requested twice in a request,
                        # once per metering point it served.
                        if "tag" in device:
                            record["tag"] = device["tag"]

                    logger.debug(
                        f"Device {device['serNum']}: {len(result)} records "
                        f"(attempt {attempt})"
                    )

                    return result

                elif response.status_code in (401, 403):
                    # The client lives for one get_volumes call, so a token
                    # rarely outlives its request. When it does, a plain
                    # re-login is the whole recovery: DPD's refresh endpoint
                    # is not on this host (auth lives on auth-direct, data on
                    # rest-direct) and its contract is unverified.
                    logger.warning(
                        f"Auth failed for device {device['serNum']} "
                        f"(attempt {attempt}), re-authenticating"
                    )
                    await self._authenticate()
                    continue

                elif 400 <= response.status_code < 500:
                    # Deterministic client error (404 = device unknown to DPD,
                    # e.g. a stale mapping) — retrying cannot change the answer.
                    if response.status_code == 404 and not_found is not None:
                        not_found.append(device["serNum"])
                    else:
                        logger.warning(
                            f"DPD API rejected device {device['serNum']}: "
                            f"{response.status_code} - {response.text}"
                        )
                    return []

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
                    # Back off before retrying: when DPD degrades, immediate
                    # retries only pile more load on it.
                    await asyncio.sleep(attempt)

            except httpx.RequestError as e:
                logger.error(
                    f"Request error for device {device['serNum']} "
                    f"(attempt {attempt}): {e!r}"
                )
                if attempt == max_retries:
                    logger.warning(
                        f"Failed to fetch data for device {device['serNum']} "
                        f"after {max_retries} attempts due to network error"
                    )
                    return []
                await asyncio.sleep(attempt)

        return []

    async def get_volumes(
        self,
        devices: List[Dict],
        date_from: datetime,
        date_to: datetime,
        type_request: str = "daily",
        max_retries: int = 3,
        device_ranges: Optional[Dict[Tuple, Tuple[datetime, datetime]]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
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
            device_ranges: Optional per-device (date_from, date_to) overrides
                keyed by (serNum, mfDev, typeDev, chNum). A device missing only
                one cached day then downloads that day, not the whole range.
                Devices absent from the dict use the date_from/date_to args.
                A device dict may instead carry its own ("range": (from, to)),
                which takes precedence — the quadruple cannot distinguish two
                entries of one corrector, e.g. the same device polled for two
                metering points over different windows. Such a dict may also
                carry a "tag", echoed onto every record it produces.
            progress_cb: Optional SYNCHRONOUS callable invoked as
                progress_cb(done, total) after each device request completes.
                It must be non-blocking (fire-and-forget) — it runs on the
                polling path and must never slow a device request down.

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
        if not devices:
            logger.warning("No devices provided to get_volumes")
            return []

        # One pooled client for the whole request; the pool caps how many device
        # requests hit DPD at once. Closed in the finally below.
        client = self._build_client()
        self._client = client
        not_found: List = []
        try:
            # Lazy authentication on first call (uses the pooled client)
            if not self._authenticated:
                await self._authenticate()

            logger.info(
                f"Fetching {type_request} volumes for {len(devices)} devices "
                f"from {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')} "
                f"(max {backend_settings['DPD_MAX_CONCURRENCY']} concurrent)"
            )

            # Create parallel tasks for each device. The shared client's connection
            # pool limits how many actually run at once (the rest await a free slot).
            device_ranges = device_ranges or {}
            total_devices = len(devices)
            done_devices = 0

            async def _poll_device(device, dev_from, dev_to):
                nonlocal done_devices
                result = await self._get_device_indications(
                    client, device, dev_from, dev_to, type_request,
                    max_retries, not_found,
                )
                done_devices += 1
                if progress_cb is not None:
                    progress_cb(done_devices, total_devices)
                return result

            tasks = []
            for device in devices:
                # A device's own range wins: device_ranges is keyed by the
                # identity quadruple, which the enterprise backfill cannot
                # rely on — it works in device ids and carries the span on the
                # device dict instead.
                dev_from, dev_to = device.get("range") or device_ranges.get(
                    (device["serNum"], device["mfDev"], device["typeDev"], device["chNum"]),
                    (date_from, date_to),
                )
                tasks.append(
                    asyncio.create_task(_poll_device(device, dev_from, dev_to))
                )

            try:
                # Execute all requests in parallel
                # return_exceptions=True prevents one failure from stopping others
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                # If this request was cancelled (browser/nginx dropped the
                # connection) the gather above unwinds early. Reap every device
                # task before the client is closed, so no task wakes up later
                # against a closed client and dies unretrieved.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

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

            if not_found:
                logger.warning(
                    f"{len(not_found)} devices unknown to DPD (404), check "
                    f"device mappings: {sorted(not_found)}"
                )

            logger.info(
                f"Fetched {len(all_records)} volume records total: "
                f"{successful_devices} devices succeeded, {failed_devices} failed/empty"
            )

            return all_records
        finally:
            await self.close()

    # DPD exposes alarms and interventions through the same pair of endpoints:
    # a paged POST that lists the DEVICES having events in the window, and a
    # paged GET that returns one device's events. Only the path segment
    # differs, so both kinds share the code below.
    EVENT_KINDS = {
        "accidents": ("devices/accidents", "accidents"),
        "interventions": ("devices/interventions", "interventions"),
    }

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Pooled client + a live token, created on first use.

        get_volumes owns its client for one call; the event endpoints are
        driven by a caller that makes many requests in a row, so it opens the
        client once and calls close() itself when done.
        """
        if self._client is None:
            self._client = self._build_client()
        if not self._authenticated:
            await self._authenticate()
        return self._client

    async def _paged(self, method: str, url: str, date_from, date_to,
                     *, body=None, page_size: int = EVENT_PAGE_SIZE, page_cb=None) -> List[Dict]:
        """Drain a Spring Data page envelope into one list.

        DPD answers {content, page, size, totalElements}; totalElements counts
        rows, not pages, so the loop stops on a short/empty page rather than
        computing a page count that a concurrent write could invalidate.
        """
        client = await self._ensure_client()
        out: List[Dict] = []
        page = 0
        relogins = 0
        while True:
            params = {
                "from": date_from.strftime("%Y-%m-%d"),
                "to": date_to.strftime("%Y-%m-%d"),
                "page": page,
                "size": page_size,
            }
            headers = {"Authorization": f"Bearer {self.access_token}"}
            if method == "POST":
                resp = await client.post(url, json=body or {}, headers=headers, params=params)
            else:
                resp = await client.get(url, headers=headers, params=params)
            if resp.status_code in (401, 403) and relogins < MAX_RELOGINS:
                # Same recovery as the volume path: the token is cheap to renew.
                # Bounded, because a 403 that is about PERMISSIONS rather than
                # an expired token survives any number of logins, and an
                # unbounded retry here would hammer /auth/login forever while
                # holding this request's connection.
                relogins += 1
                await self._authenticate()
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content") or []
            out.extend(content)
            if page_cb is not None:
                page_cb(len(out), data.get("totalElements"))
            if len(content) < page_size:
                return out
            page += 1

    async def get_event_devices(self, kind: str, date_from, date_to,
                                *, filters=None, page_cb=None) -> List[Dict]:
        """Devices that had alarms (or interventions) in the window.

        This is the cheap half of the pair: one paged call answers "who has
        anything to say", so the per-device calls that follow are only made
        for devices that actually do.
        """
        path, _ = self.EVENT_KINDS[kind]
        return await self._paged(
            "POST", self.base_url + path, date_from, date_to,
            body=filters or {}, page_cb=page_cb,
        )

    async def get_device_events(self, kind: str, device_id, date_from, date_to) -> List[Dict]:
        """One device's alarms (or interventions), all pages.

        device_id is DPD's own row id from get_event_devices — not serNum.
        """
        _, path = self.EVENT_KINDS[kind]
        return await self._paged(
            "GET", f"{self.base_url}{path}/{device_id}", date_from, date_to,
        )

    async def close(self):
        """Close the pooled client and release its connections."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
