"""Unit tests for the JWT/password primitives in backend/api/endpoints/auth_ep.py.

Pure-function tests: no database, no HTTP. Time-dependent behaviour (expiry,
sliding-session refresh, absolute cap) is driven with freezegun.
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from jose import jwt

from backend.api.endpoints.auth_ep import (
    JWT_ALGORITHM,
    JWT_ABSOLUTE_MAX_SECONDS,
    JWT_EXPIRE_HOURS,
    JWT_REMEMBER_ME_DAYS,
    JWT_SECRET,
    _create_token,
    _maybe_refresh_token,
    _verify_password,
    decode_jwt,
    hash_password,
)


class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        hashed = hash_password("s3cret-pass")
        assert hashed != "s3cret-pass"
        assert _verify_password("s3cret-pass", hashed)

    def test_wrong_password_rejected(self):
        hashed = hash_password("s3cret-pass")
        assert not _verify_password("wrong-pass", hashed)

    def test_salted_hashes_differ(self):
        assert hash_password("same") != hash_password("same")


class TestCreateToken:
    def test_default_token_claims(self):
        with freeze_time("2026-01-01 12:00:00"):
            token = _create_token(42, role="admin", branch_ids=[1, 3])
            claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert claims["sub"] == "42"
        assert claims["role"] == "admin"
        assert claims["branches"] == [1, 3]
        assert claims["ttl"] == JWT_EXPIRE_HOURS * 3600
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp()
        assert claims["iat"] == int(now)
        assert claims["exp"] == int(now) + JWT_EXPIRE_HOURS * 3600

    def test_remember_me_extends_ttl(self):
        token = _create_token(1, remember_me=True)
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert claims["ttl"] == JWT_REMEMBER_ME_DAYS * 86400

    def test_optional_claims_omitted(self):
        token = _create_token(1)
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert "role" not in claims
        assert "branches" not in claims


class TestDecodeJwt:
    def test_valid_token(self):
        token = _create_token(7, role="viewer")
        claims = decode_jwt(token)
        assert claims is not None
        assert claims["sub"] == "7"

    def test_none_and_garbage(self):
        assert decode_jwt(None) is None
        assert decode_jwt("") is None
        assert decode_jwt("not-a-jwt") is None

    def test_wrong_secret_rejected(self):
        forged = jwt.encode(
            {"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            "attacker-secret",
            algorithm=JWT_ALGORITHM,
        )
        assert decode_jwt(forged) is None

    def test_expired_token_rejected(self):
        with freeze_time("2026-01-01 00:00:00"):
            token = _create_token(1)
        with freeze_time(f"2026-01-01 {JWT_EXPIRE_HOURS + 1:02d}:00:01"):
            assert decode_jwt(token) is None


class TestMaybeRefreshToken:
    def test_fresh_token_not_refreshed(self):
        with freeze_time("2026-01-01 00:00:00"):
            token = _create_token(1, role="admin")
        # 1h into a 12h token → well above the 50% threshold
        with freeze_time("2026-01-01 01:00:00"):
            assert _maybe_refresh_token(token) is None

    def test_past_threshold_reissues_same_lifetime(self):
        with freeze_time("2026-01-01 00:00:00"):
            token = _create_token(1, role="admin", branch_ids=[5])
            original_iat = decode_jwt(token)["iat"]
        # 7h into a 12h token → less than 50% remaining → refresh
        with freeze_time("2026-01-01 07:00:00"):
            refreshed = _maybe_refresh_token(token)
            assert refreshed is not None
            new_token, max_age = refreshed
            claims = decode_jwt(new_token)
            now = datetime.now(timezone.utc).timestamp()
        assert max_age == JWT_EXPIRE_HOURS * 3600
        assert claims["ttl"] == JWT_EXPIRE_HOURS * 3600
        # first-login time survives the refresh (absolute-cap anchor)
        assert claims["iat"] == original_iat
        assert claims["exp"] == int(now) + JWT_EXPIRE_HOURS * 3600
        # role/branches carried over
        assert claims["role"] == "admin"
        assert claims["branches"] == [5]

    def test_expired_token_not_refreshed(self):
        with freeze_time("2026-01-01 00:00:00"):
            token = _create_token(1)
        with freeze_time("2026-01-02 00:00:00"):
            assert _maybe_refresh_token(token) is None

    def test_absolute_cap_stops_sliding(self):
        # Token still valid and past the refresh threshold, but the session
        # (iat) is older than the 30-day cap → no refresh.
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "1",
            "exp": now + timedelta(hours=1),
            "ttl": JWT_EXPIRE_HOURS * 3600,
            "iat": int(now.timestamp()) - JWT_ABSOLUTE_MAX_SECONDS - 60,
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        assert _maybe_refresh_token(token) is None

    def test_legacy_token_without_ttl_not_refreshed(self):
        payload = {
            "sub": "1",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        assert _maybe_refresh_token(token) is None

    def test_invalid_token_not_refreshed(self):
        assert _maybe_refresh_token("garbage") is None
