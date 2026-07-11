"""Unit tests for DPDClient retry and lifecycle logic.

All HTTP traffic goes through httpx.MockTransport injected via _build_client —
no network, no database. Covers the production failure modes seen in the
2026-07 backend.log: 404s retried pointlessly, immediate retry hammering, and
device tasks crashing on a closed client after request cancellation.
"""

import asyncio
from datetime import datetime

import httpx
import pytest

from backend.services.dpd_client import DPDClient

AUTH_URL = "https://dpd.test/auth/login"
BASE_URL = "https://dpd.test/api/"
DEVICE = {"serNum": 111, "mfDev": 1, "typeDev": 5, "chNum": 0}
DATE_FROM = datetime(2026, 7, 1)
DATE_TO = datetime(2026, 7, 3)


def auth_response() -> httpx.Response:
    return httpx.Response(200, json={"access": "acc", "refresh": "ref"})


def make_client(handler, built=None) -> DPDClient:
    """DPDClient whose pooled client talks to a MockTransport."""
    client = DPDClient(
        base_url=BASE_URL,
        auth_url=AUTH_URL,
        username="user",
        password="pass",
        timeout=5,
    )

    def build():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if built is not None:
            built.append(http_client)
        return http_client

    client._build_client = build
    return client


class TestRetryPolicy:
    async def test_404_not_retried_and_summarized_once(self, caplog):
        calls = {"indications": 0}

        def handler(request):
            if request.url.path.endswith("/auth/login"):
                return auth_response()
            calls["indications"] += 1
            return httpx.Response(404, text="no such device")

        client = make_client(handler)
        with caplog.at_level("WARNING", logger="backend.services.dpd_client"):
            records = await client.get_volumes([DEVICE], DATE_FROM, DATE_TO)

        assert records == []
        assert calls["indications"] == 1  # deterministic error: single attempt
        summaries = [r for r in caplog.records if "unknown to DPD" in r.message]
        assert len(summaries) == 1
        assert "111" in summaries[0].message

    async def test_5xx_retried_with_backoff(self, monkeypatch):
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(
            "backend.services.dpd_client.asyncio.sleep", fake_sleep
        )
        calls = {"indications": 0}

        def handler(request):
            if request.url.path.endswith("/auth/login"):
                return auth_response()
            calls["indications"] += 1
            return httpx.Response(500, text="boom")

        client = make_client(handler)
        records = await client.get_volumes([DEVICE], DATE_FROM, DATE_TO)

        assert records == []
        assert calls["indications"] == 3
        assert sleeps == [1, 2]  # no sleep after the final attempt

    async def test_network_error_retried_with_backoff(self, monkeypatch):
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(
            "backend.services.dpd_client.asyncio.sleep", fake_sleep
        )
        calls = {"indications": 0}

        def handler(request):
            if request.url.path.endswith("/auth/login"):
                return auth_response()
            calls["indications"] += 1
            raise httpx.ConnectError("connection refused")

        client = make_client(handler)
        records = await client.get_volumes([DEVICE], DATE_FROM, DATE_TO)

        assert records == []
        assert calls["indications"] == 3
        assert sleeps == [1, 2]

    async def test_success_attaches_device_identifiers(self):
        def handler(request):
            if request.url.path.endswith("/auth/login"):
                return auth_response()
            return httpx.Response(
                200,
                json={"table": {"data": [
                    {"date": "2026-07-01", "dvstAlwrk": 5.0, "dvwrkAlwrk": 5.2},
                ]}},
            )

        client = make_client(handler)
        records = await client.get_volumes([DEVICE], DATE_FROM, DATE_TO)

        assert len(records) == 1
        assert records[0]["serNum"] == 111
        assert records[0]["mfDev"] == 1
        assert records[0]["dvstAlwrk"] == 5.0


class TestRequestParams:
    @staticmethod
    def make_recording_client(seen: dict):
        """Client whose handler records per-serNum (from, to) query params."""
        def handler(request):
            if request.url.path.endswith("/auth/login"):
                return auth_response()
            params = dict(request.url.params)
            seen[int(params["serNUM"])] = (params["from"], params["to"])
            return httpx.Response(200, json={"table": {"data": []}})

        return make_client(handler)

    async def test_hourly_bounds_mapped_to_commercial_dates(self):
        """DPD from/to take bare dates; the hourly range is aligned to
        commercial days on both ends (from=D starts at D 07:00, to=D runs
        through D+1 06:00), so a datetime bound before 07:00 belongs to the
        previous commercial date."""
        seen = {}
        client = self.make_recording_client(seen)

        await client.get_volumes(
            [DEVICE],
            datetime(2026, 7, 1, 3, 0),
            datetime(2026, 7, 8, 6, 0),
            type_request="hourly",
        )

        assert seen[111] == ("2026-06-30", "2026-07-07")

    async def test_hourly_bounds_at_contract_hour_keep_their_date(self):
        seen = {}
        client = self.make_recording_client(seen)

        await client.get_volumes(
            [DEVICE],
            datetime(2026, 7, 1, 7, 0),
            datetime(2026, 7, 8, 23, 0),
            type_request="hourly",
        )

        assert seen[111] == ("2026-07-01", "2026-07-08")

    async def test_daily_dates_passed_verbatim(self):
        seen = {}
        client = self.make_recording_client(seen)

        await client.get_volumes([DEVICE], DATE_FROM, DATE_TO, type_request="daily")

        assert seen[111] == ("2026-07-01", "2026-07-03")

    async def test_progress_cb_fires_once_per_completed_device(self):
        seen = {}
        client = self.make_recording_client(seen)
        devices = [dict(DEVICE, serNum=100 + i) for i in range(5)]
        calls = []

        await client.get_volumes(
            devices, DATE_FROM, DATE_TO,
            progress_cb=lambda done, total: calls.append((done, total)),
        )

        assert [c[0] for c in calls] == [1, 2, 3, 4, 5]
        assert all(total == 5 for _, total in calls)

    async def test_device_ranges_override_per_device(self):
        seen = {}
        client = self.make_recording_client(seen)
        dev_a = dict(DEVICE, serNum=111)
        dev_b = dict(DEVICE, serNum=222)

        await client.get_volumes(
            [dev_a, dev_b],
            DATE_FROM,
            DATE_TO,
            device_ranges={
                (222, 1, 5, 0): (datetime(2026, 7, 3), datetime(2026, 7, 3)),
            },
        )

        assert seen[111] == ("2026-07-01", "2026-07-03")  # default range
        assert seen[222] == ("2026-07-03", "2026-07-03")  # own span


class TestCancellation:
    async def test_cancel_reaps_device_tasks_and_closes_client(self):
        """Cancelling get_volumes mid-poll must leave no orphan device tasks
        (the 'NoneType has no attribute get' crashes from production) and must
        close the pooled client."""
        first_request_started = asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("/auth/login"):
                return auth_response()
            first_request_started.set()
            await asyncio.sleep(30)  # never finishes within the test
            return httpx.Response(200, json={"table": {"data": []}})

        built = []
        client = make_client(handler, built=built)
        devices = [dict(DEVICE, serNum=1000 + i) for i in range(25)]

        poll = asyncio.create_task(
            client.get_volumes(devices, DATE_FROM, DATE_TO)
        )
        await first_request_started.wait()
        poll.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poll

        assert built, "pooled client was never built"
        assert built[0].is_closed
        orphans = [
            t for t in asyncio.all_tasks()
            if ("_poll_device" in repr(t) or "_get_device_indications" in repr(t))
            and not t.done()
        ]
        assert orphans == []
