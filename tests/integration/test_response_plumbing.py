"""Response plumbing: NaN/Inf sanitization in NaNSafeJSONResponse and the
global unhandled-exception handler (500 with generic body, no traceback leak).

Uses throwaway routes registered on the real app so the full middleware stack
(auth guard, sliding session, response class) is exercised.
"""

import math

import pytest

from backend.api.main import _sanitize_nan, app


def _boom():
    raise RuntimeError("secret internal details")


def _nan_payload():
    return {
        "value": math.nan,
        "inf": math.inf,
        "nested": {"list": [1.0, math.nan, {"deep": -math.inf}]},
        "ok": 42.5,
    }


@pytest.fixture
def plumbing_routes():
    """Temporarily register test-only routes, removing them afterwards."""
    app.router.add_api_route("/_test/boom", _boom, methods=["GET"])
    app.router.add_api_route("/_test/nan", _nan_payload, methods=["GET"])
    added = {"/_test/boom", "/_test/nan"}
    yield
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) not in added
    ]


class TestSanitizeNan:
    def test_replaces_non_finite_recursively(self):
        result = _sanitize_nan(
            {"a": math.nan, "b": [math.inf, {"c": -math.inf}], "d": 1.5}
        )
        assert result == {"a": None, "b": [None, {"c": None}], "d": 1.5}

    def test_leaves_other_types_alone(self):
        data = {"s": "text", "i": 7, "b": True, "n": None}
        assert _sanitize_nan(data) == data


class TestNaNSafeResponses:
    async def test_nan_and_inf_become_null(self, plumbing_routes, admin_client):
        resp = await admin_client.get("/_test/nan")
        assert resp.status_code == 200
        body = resp.json()
        assert body["value"] is None
        assert body["inf"] is None
        assert body["nested"]["list"] == [1.0, None, {"deep": None}]
        assert body["ok"] == 42.5
        # the raw body must be valid strict JSON (no bare NaN tokens)
        assert b"NaN" not in resp.content
        assert b"Infinity" not in resp.content


class TestUnhandledExceptionHandler:
    async def test_500_with_generic_detail(self, plumbing_routes, seed_users):
        # Starlette re-raises the original exception after sending the 500, so
        # this client must not propagate app exceptions to see the response.
        from httpx import ASGITransport, AsyncClient

        from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/auth/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            )
            assert login.status_code == 200
            resp = await client.get("/_test/boom")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal Server Error"}
        # internal error text must not leak to the client
        assert "secret internal details" not in resp.text
