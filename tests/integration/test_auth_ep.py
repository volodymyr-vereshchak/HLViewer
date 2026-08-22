"""Integration tests for the auth endpoints and the central auth middleware.

Covers: login/logout//auth/me, the default-deny auth_guard matrix
(anon 401 / viewer read 200 / viewer write 403 / admin-marker paths),
sliding-session cookie refresh, startup seeding (_seed_admin/_seed_default_user)
and the admin user-management guards (last active admin, self-delete).
"""

from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from backend.api.main import app, _seed_admin, _seed_default_user
from backend.api.endpoints.auth_ep import (
    COOKIE_NAME,
    JWT_EXPIRE_HOURS,
    JWT_REMEMBER_ME_DAYS,
    DOMAIN_LOGIN_OFF_DETAIL,
    PERMS_CHANGED_DETAIL,
    SESSION_ENDED_DOMAIN_OFF,
    SESSION_ENDED_HEADER,
    SESSION_ENDED_PERMS,
    _create_token,
    decode_jwt,
)
from backend.db.engine import async_session_factory
from backend.db.models.app_user_model import AppUser
from backend.db.models.grmu_branch_model import GrmuBranch

from tests.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    VIEWER_PASSWORD,
    VIEWER_USERNAME,
)


def _fresh_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _insert_user(**kwargs) -> int:
    async with async_session_factory() as session:
        user = AppUser(**kwargs)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id


async def _get_user(user_id: int) -> AppUser | None:
    async with async_session_factory() as session:
        return (
            await session.execute(select(AppUser).where(AppUser.id == user_id))
        ).scalar_one_or_none()


async def _insert_branch(name: str = "Тестова філія") -> int:
    async with async_session_factory() as session:
        branch = GrmuBranch(name=name)
        session.add(branch)
        await session.commit()
        await session.refresh(branch)
        return branch.id


# ── /auth/login ───────────────────────────────────────────────────────────────
class TestLogin:
    async def test_login_ok_sets_cookie(self, anon_client, seed_users):
        resp = await anon_client.post(
            "/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == ADMIN_USERNAME
        assert body["role"] == "admin"
        assert body["active"] is True

        token = anon_client.cookies.get(COOKIE_NAME)
        assert token, "auth cookie was not set"
        claims = decode_jwt(token)
        assert claims["sub"] == str(seed_users["admin"])
        assert claims["role"] == "admin"
        assert claims["ttl"] == JWT_EXPIRE_HOURS * 3600

    async def test_login_normalizes_username(self, anon_client, seed_users):
        resp = await anon_client.post(
            "/auth/login",
            json={"username": f"  {ADMIN_USERNAME.upper()}  ", "password": ADMIN_PASSWORD},
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == ADMIN_USERNAME

    async def test_login_wrong_password(self, anon_client, seed_users):
        resp = await anon_client.post(
            "/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong-password"},
        )
        assert resp.status_code == 401
        assert COOKIE_NAME not in anon_client.cookies

    async def test_login_unknown_user(self, anon_client, seed_users):
        resp = await anon_client.post(
            "/auth/login",
            json={"username": "ghost", "password": "whatever"},
        )
        assert resp.status_code == 401

    async def test_login_inactive_user_403(self, anon_client, seed_users, _viewer_hash):
        await _insert_user(
            username="sleeper", role="viewer", active=False, password_hash=_viewer_hash
        )
        resp = await anon_client.post(
            "/auth/login",
            json={"username": "sleeper", "password": VIEWER_PASSWORD},
        )
        assert resp.status_code == 403

    async def test_login_remember_me_long_ttl(self, anon_client, seed_users):
        resp = await anon_client.post(
            "/auth/login",
            json={
                "username": VIEWER_USERNAME,
                "password": VIEWER_PASSWORD,
                "remember_me": True,
            },
        )
        assert resp.status_code == 200
        claims = decode_jwt(anon_client.cookies.get(COOKIE_NAME))
        assert claims["ttl"] == JWT_REMEMBER_ME_DAYS * 86400


# ── /auth/logout + /auth/me ───────────────────────────────────────────────────
class TestSession:
    async def test_logout_kills_session(self, admin_client):
        assert (await admin_client.get("/lumgs/")).status_code == 200
        resp = await admin_client.post("/auth/logout")
        assert resp.status_code == 200
        assert (await admin_client.get("/lumgs/")).status_code == 401

    async def test_me_authenticated(self, viewer_client, seed_users):
        resp = await viewer_client.get("/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == VIEWER_USERNAME
        assert body["role"] == "viewer"

    async def test_me_anonymous_401(self, anon_client):
        # AUTO_LOGIN is forced off in tests → no fallback session
        resp = await anon_client.get("/auth/me")
        assert resp.status_code == 401

    async def test_me_deactivated_user_401(self, admin_client, seed_users):
        # deactivate the viewer, then their (still-valid) token must be refused
        async with _fresh_client() as client:
            resp = await client.post(
                "/auth/login",
                json={"username": VIEWER_USERNAME, "password": VIEWER_PASSWORD},
            )
            assert resp.status_code == 200
            patch = await admin_client.patch(
                f"/auth/users/{seed_users['viewer']}", json={"active": False}
            )
            assert patch.status_code == 200
            assert (await client.get("/auth/me")).status_code == 401


# ── central auth_guard middleware matrix ─────────────────────────────────────
class TestAuthGuard:
    async def test_anon_read_401(self, anon_client):
        resp = await anon_client.get("/lumgs/")
        assert resp.status_code == 401

    async def test_anon_write_401(self, anon_client):
        resp = await anon_client.post("/lumgs/", json={"name": "x", "branch_id": 1})
        assert resp.status_code == 401

    async def test_viewer_read_200(self, viewer_client):
        resp = await viewer_client.get("/lumgs/")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_viewer_write_403(self, viewer_client):
        resp = await viewer_client.post("/lumgs/", json={"name": "x", "branch_id": 1})
        assert resp.status_code == 403

    async def test_admin_write_ok(self, admin_client):
        branch_id = await _insert_branch()
        resp = await admin_client.post(
            "/lumgs/", json={"name": "Новий ЛУМГ", "branch_id": branch_id}
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Новий ЛУМГ"

    async def test_viewer_admin_marker_path_403(self, viewer_client):
        # GET would pass the read-only rule, but "data-path" is an admin marker
        resp = await viewer_client.get("/lumgs/1/data-path")
        assert resp.status_code == 403

    async def test_admin_marker_path_passes_guard(self, admin_client):
        # guard lets the admin through; the route itself answers 404 (no data)
        resp = await admin_client.get("/lumgs/1/data-path")
        assert resp.status_code == 404

    async def test_viewer_scan_eis_403(self, viewer_client):
        resp = await viewer_client.get("/lumgs/1/scan-eis")
        assert resp.status_code == 403

    async def test_viewer_list_users_403(self, viewer_client):
        # route-level admin check (GET passes the middleware)
        resp = await viewer_client.get("/auth/users")
        assert resp.status_code == 403

    async def test_admin_list_users_200(self, admin_client):
        resp = await admin_client.get("/auth/users")
        assert resp.status_code == 200
        usernames = {u["username"] for u in resp.json()}
        assert {ADMIN_USERNAME, VIEWER_USERNAME} <= usernames


# ── sliding session (middleware in api/main.py) ──────────────────────────────
class TestSlidingSession:
    async def test_refresh_past_threshold(self, seed_users):
        async with _fresh_client() as client:
            with freeze_time("2026-01-01 00:00:00") as frozen:
                resp = await client.post(
                    "/auth/login",
                    json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                )
                assert resp.status_code == 200
                original = decode_jwt(client.cookies.get(COOKIE_NAME))

                # 7h into a 12h token → less than half remaining → re-issued
                frozen.move_to("2026-01-01 07:00:00")
                resp = await client.get("/lumgs/")
                assert resp.status_code == 200
                assert "set-cookie" in resp.headers

                refreshed = decode_jwt(client.cookies.get(COOKIE_NAME))
                assert refreshed["exp"] > original["exp"]
                # first-login time is preserved (absolute-cap anchor)
                assert refreshed["iat"] == original["iat"]

    async def test_no_refresh_while_fresh(self, seed_users):
        async with _fresh_client() as client:
            with freeze_time("2026-01-01 00:00:00") as frozen:
                resp = await client.post(
                    "/auth/login",
                    json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                )
                assert resp.status_code == 200

                frozen.move_to("2026-01-01 01:00:00")
                resp = await client.get("/lumgs/")
                assert resp.status_code == 200
                assert "set-cookie" not in resp.headers


# ── rights changes end the sessions that predate them ────────────────────────
# The token carries role and branches, so before perms_version a change to an
# account did not reach the sessions it already had: a demoted admin kept write
# access at the central guard for the life of the token (up to 30 days with
# remember-me), and a promoted viewer was shown the admin screens by /auth/me
# while every save came back 403.
class TestPermsVersion:
    async def _login_viewer(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/auth/login",
            json={"username": VIEWER_USERNAME, "password": VIEWER_PASSWORD},
        )
        assert resp.status_code == 200

    async def test_promotion_kills_the_old_token(self, admin_client, seed_users):
        async with _fresh_client() as client:
            await self._login_viewer(client)
            assert (await client.get("/lumgs/")).status_code == 200

            patch = await admin_client.patch(
                f"/auth/users/{seed_users['viewer']}", json={"role": "admin"}
            )
            assert patch.status_code == 200

            resp = await client.get("/lumgs/")
            assert resp.status_code == 401
            assert resp.json()["detail"] == PERMS_CHANGED_DETAIL
            # marks this apart from an ordinary expiry for the login screen
            assert resp.headers[SESSION_ENDED_HEADER] == SESSION_ENDED_PERMS

    async def test_ordinary_401_carries_no_marker(self, anon_client):
        resp = await anon_client.get("/lumgs/")
        assert resp.status_code == 401
        assert SESSION_ENDED_HEADER not in resp.headers

    async def test_stale_token_cannot_re_hydrate_via_me(self, admin_client, seed_users):
        # /auth/me is public and is the frontend's 401 recovery path: if it
        # answered from the database the app would come back up "signed in"
        # holding a token that fails every call.
        async with _fresh_client() as client:
            await self._login_viewer(client)
            await admin_client.patch(
                f"/auth/users/{seed_users['viewer']}", json={"role": "admin"}
            )
            resp = await client.get("/auth/me")
            assert resp.status_code == 401
            assert resp.json()["detail"] == PERMS_CHANGED_DETAIL
            assert resp.headers[SESSION_ENDED_HEADER] == SESSION_ENDED_PERMS
            assert not client.cookies.get(COOKIE_NAME)  # cookie cleared

    async def test_demotion_stops_writes_at_once(self, admin_client, seed_users, _admin_hash):
        # The dangerous direction: an admin token that still says "admin".
        second = await _insert_user(
            username="admin2", role="admin", active=True, password_hash=_admin_hash
        )
        async with _fresh_client() as client:
            login = await client.post(
                "/auth/login", json={"username": "admin2", "password": ADMIN_PASSWORD}
            )
            assert login.status_code == 200
            assert decode_jwt(client.cookies.get(COOKIE_NAME))["role"] == "admin"

            await admin_client.patch(f"/auth/users/{second}", json={"role": "viewer"})

            resp = await client.post("/lumgs/", json={"name": "x", "branch_id": 1})
            assert resp.status_code == 401

    async def test_re_login_restores_access_with_the_new_role(self, admin_client, seed_users):
        async with _fresh_client() as client:
            await self._login_viewer(client)
            await admin_client.patch(
                f"/auth/users/{seed_users['viewer']}", json={"role": "admin"}
            )
            assert (await client.get("/lumgs/")).status_code == 401

            again = await client.post(
                "/auth/login",
                json={"username": VIEWER_USERNAME, "password": VIEWER_PASSWORD},
            )
            assert again.status_code == 200
            assert again.json()["role"] == "admin"

            branch_id = await _insert_branch("Філія після підвищення")
            write = await client.post(
                "/lumgs/", json={"name": "Після підвищення", "branch_id": branch_id}
            )
            assert write.status_code == 201

    async def test_branch_access_change_kills_the_token(self, admin_client, seed_users):
        branch_id = await _insert_branch()
        async with _fresh_client() as client:
            await self._login_viewer(client)
            await admin_client.patch(
                f"/auth/users/{seed_users['viewer']}", json={"branch_ids": [branch_id]}
            )
            assert (await client.get("/lumgs/")).status_code == 401

    async def test_same_branches_resubmitted_is_not_a_change(self, admin_client, seed_users):
        # The admin form posts the whole user on every save; re-sending the same
        # branch list must not throw the user out.
        branch_id = await _insert_branch()
        await admin_client.patch(
            f"/auth/users/{seed_users['viewer']}", json={"branch_ids": [branch_id]}
        )
        async with _fresh_client() as client:
            await self._login_viewer(client)
            await admin_client.patch(
                f"/auth/users/{seed_users['viewer']}", json={"branch_ids": [branch_id]}
            )
            assert (await client.get("/lumgs/")).status_code == 200

    async def test_display_name_edit_keeps_the_session(self, admin_client, seed_users):
        async with _fresh_client() as client:
            await self._login_viewer(client)
            patch = await admin_client.patch(
                f"/auth/users/{seed_users['viewer']}", json={"display_name": "Нове ім'я"}
            )
            assert patch.status_code == 200
            assert (await client.get("/lumgs/")).status_code == 200

    async def test_password_reset_kills_the_session(self, admin_client, seed_users):
        async with _fresh_client() as client:
            await self._login_viewer(client)
            reset = await admin_client.post(
                f"/auth/users/{seed_users['viewer']}/reset-password"
            )
            assert reset.status_code == 200
            assert (await client.get("/lumgs/")).status_code == 401

    async def test_token_without_pv_still_works(self, seed_users):
        # Tokens minted before this existed carry no `pv`; they are read as
        # generation 1 so the deploy does not sign everybody out.
        legacy = _create_token(seed_users["viewer"], role="viewer", branch_ids=[])
        assert "pv" not in decode_jwt(legacy)
        async with _fresh_client() as client:
            client.cookies.set(COOKIE_NAME, legacy)
            assert (await client.get("/lumgs/")).status_code == 200

    async def test_stale_token_is_not_slid_forward(self, admin_client, seed_users):
        # The sliding session must not hand back a fresh copy of a token the
        # guard has just refused.
        with freeze_time("2026-01-01 00:00:00") as frozen:
            async with _fresh_client() as client:
                await self._login_viewer(client)
                await admin_client.patch(
                    f"/auth/users/{seed_users['viewer']}", json={"role": "admin"}
                )
                frozen.move_to("2026-01-01 07:00:00")  # past the refresh threshold
                resp = await client.get("/auth/me")
                assert resp.status_code == 401
                assert not client.cookies.get(COOKIE_NAME)


# ── startup seeding (lifespan helpers) ────────────────────────────────────────
class TestSeeding:
    async def test_seed_admin_creates_user(self, clean_db, monkeypatch):
        monkeypatch.setenv("ADMIN_USERNAME", "envadmin")
        monkeypatch.setenv("ADMIN_PASSWORD", "env-admin-pass")
        await _seed_admin()
        async with async_session_factory() as session:
            user = (
                await session.execute(select(AppUser).where(AppUser.username == "envadmin"))
            ).scalar_one()
        assert user.role == "admin"
        assert user.active is True

        # idempotent: a second call must not duplicate or fail
        await _seed_admin()
        async with async_session_factory() as session:
            count = len(
                (await session.execute(select(AppUser).where(AppUser.username == "envadmin")))
                .scalars()
                .all()
            )
        assert count == 1

    async def test_seed_admin_without_password_is_noop(self, clean_db, monkeypatch):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        await _seed_admin()
        async with async_session_factory() as session:
            users = (await session.execute(select(AppUser))).scalars().all()
        assert users == []

    async def test_seed_default_user_creates_viewer(self, clean_db, monkeypatch):
        monkeypatch.setenv("DEFAULT_USERNAME", "envviewer")
        monkeypatch.setenv("DEFAULT_PASSWORD", "env-viewer-pass")
        await _seed_default_user()
        async with async_session_factory() as session:
            user = (
                await session.execute(select(AppUser).where(AppUser.username == "envviewer"))
            ).scalar_one()
        assert user.role == "viewer"

    async def test_seed_default_user_migrates_viewer_all(self, clean_db, monkeypatch, _viewer_hash):
        user_id = await _insert_user(
            username="legacy", role="viewer_all", active=True, password_hash=_viewer_hash
        )
        monkeypatch.setenv("DEFAULT_USERNAME", "legacy")
        monkeypatch.setenv("DEFAULT_PASSWORD", VIEWER_PASSWORD)
        await _seed_default_user()
        user = await _get_user(user_id)
        assert user.role == "viewer"


# ── admin user management (/auth/users CRUD + guards) ────────────────────────
class TestUserManagement:
    async def test_create_user_and_login(self, admin_client):
        resp = await admin_client.post(
            "/auth/users",
            json={"username": "NewUser", "role": "viewer", "branch_ids": []},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["username"] == "newuser"  # normalized to lowercase
        assert body["password"]  # auto-generated

        async with _fresh_client() as client:
            login = await client.post(
                "/auth/login",
                json={"username": "newuser", "password": body["password"]},
            )
            assert login.status_code == 200

    async def test_create_duplicate_409(self, admin_client):
        resp = await admin_client.post(
            "/auth/users", json={"username": VIEWER_USERNAME, "role": "viewer"}
        )
        assert resp.status_code == 409

    async def test_cannot_deactivate_last_admin(self, admin_client, seed_users):
        resp = await admin_client.patch(
            f"/auth/users/{seed_users['admin']}", json={"active": False}
        )
        assert resp.status_code == 400

    async def test_cannot_demote_last_admin(self, admin_client, seed_users):
        resp = await admin_client.patch(
            f"/auth/users/{seed_users['admin']}", json={"role": "viewer"}
        )
        assert resp.status_code == 400

    async def test_demote_ok_with_second_admin(self, admin_client, seed_users, _admin_hash):
        await _insert_user(
            username="admin2", role="admin", active=True, password_hash=_admin_hash
        )
        resp = await admin_client.patch(
            f"/auth/users/{seed_users['admin']}", json={"role": "viewer"}
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "viewer"

    async def test_cannot_delete_own_account(self, admin_client, seed_users):
        resp = await admin_client.delete(f"/auth/users/{seed_users['admin']}")
        assert resp.status_code == 400

    async def test_delete_viewer(self, admin_client, seed_users):
        resp = await admin_client.delete(f"/auth/users/{seed_users['viewer']}")
        assert resp.status_code == 200
        assert await _get_user(seed_users["viewer"]) is None

    async def test_reset_password_flow(self, admin_client, seed_users):
        resp = await admin_client.post(
            f"/auth/users/{seed_users['viewer']}/reset-password"
        )
        assert resp.status_code == 200
        new_password = resp.json()["password"]

        async with _fresh_client() as client:
            old = await client.post(
                "/auth/login",
                json={"username": VIEWER_USERNAME, "password": VIEWER_PASSWORD},
            )
            assert old.status_code == 401
            new = await client.post(
                "/auth/login",
                json={"username": VIEWER_USERNAME, "password": new_password},
            )
            assert new.status_code == 200

    async def test_domain_account_password_reset_refused(self, admin_client):
        # Provisioned by an LDAP login: no local password. Resetting one would
        # create a local credential that outranks AD on the next login.
        domain_id = await _insert_user(
            username="domain.only", role="viewer", active=True, password_hash=None
        )
        resp = await admin_client.post(f"/auth/users/{domain_id}/reset-password")
        assert resp.status_code == 400
        assert "Active Directory" in resp.json()["detail"]

        # and the account is left exactly as it was — still domain-only
        assert (await _get_user(domain_id)).password_hash is None

    async def test_has_password_marks_domain_accounts(self, admin_client, seed_users):
        await _insert_user(
            username="domain.only", role="viewer", active=True, password_hash=None
        )
        by_name = {u["username"]: u for u in (await admin_client.get("/auth/users")).json()}
        assert by_name[VIEWER_USERNAME]["has_password"] is True
        assert by_name["domain.only"]["has_password"] is False

    async def test_update_nonexistent_user_404(self, admin_client):
        resp = await admin_client.patch("/auth/users/99999", json={"active": False})
        assert resp.status_code == 404


# ── domain accounts on a server with domain login switched off ───────────────
# Nothing revokes these accounts when LDAP_ENABLED goes false: sessions already
# open keep running to their expiry, and only then does the account turn out to
# be unusable. The least the login can do is say why.
class TestLdapSwitchedOff:
    async def _domain_user(self, username: str = "domain.only") -> int:
        return await _insert_user(
            username=username, role="viewer", active=True, password_hash=None
        )

    async def test_login_says_domain_login_is_off(self, anon_client, seed_users, monkeypatch):
        monkeypatch.setenv("LDAP_ENABLED", "false")
        await self._domain_user()
        resp = await anon_client.post(
            "/auth/login", json={"username": "domain.only", "password": "their-ad-password"}
        )
        assert resp.status_code == 403
        assert "вимкнено" in resp.json()["detail"]

    async def test_unknown_user_still_gets_the_generic_401(
        self, anon_client, seed_users, monkeypatch
    ):
        # The domain message is only for accounts that exist; a typo in the
        # login must not be answered with a different status than a typo in
        # the password.
        monkeypatch.setenv("LDAP_ENABLED", "false")
        resp = await anon_client.post(
            "/auth/login", json={"username": "no.such.person", "password": "x"}
        )
        assert resp.status_code == 401

    async def test_local_account_unaffected(self, anon_client, seed_users, monkeypatch):
        monkeypatch.setenv("LDAP_ENABLED", "false")
        resp = await anon_client.post(
            "/auth/login", json={"username": VIEWER_USERNAME, "password": "wrong"}
        )
        assert resp.status_code == 401  # still just a wrong password

    async def test_message_not_shown_while_ldap_is_up(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        mocker.patch(
            "backend.api.endpoints.auth_ep.ldap_authenticate", return_value=(False, None)
        )
        await self._domain_user()
        resp = await anon_client.post(
            "/auth/login", json={"username": "domain.only", "password": "bad-ad-password"}
        )
        assert resp.status_code == 401  # a failed bind is a wrong password

    async def test_open_session_dies_at_the_next_request(
        self, seed_users, monkeypatch, mocker
    ):
        # The whole point: switching domain login off must not leave domain
        # users working for up to 30 days on the tokens they already hold.
        monkeypatch.setenv("LDAP_ENABLED", "true")
        mocker.patch(
            "backend.api.endpoints.auth_ep.ldap_authenticate", return_value=(True, None)
        )
        await self._domain_user()
        async with _fresh_client() as client:
            login = await client.post(
                "/auth/login", json={"username": "domain.only", "password": "ad-pass"}
            )
            assert login.status_code == 200
            assert (await client.get("/lumgs/")).status_code == 200

            monkeypatch.setenv("LDAP_ENABLED", "false")

            resp = await client.get("/lumgs/")
            assert resp.status_code == 401
            assert resp.json()["detail"] == DOMAIN_LOGIN_OFF_DETAIL
            assert resp.headers[SESSION_ENDED_HEADER] == SESSION_ENDED_DOMAIN_OFF

    async def test_stranded_session_cannot_re_hydrate_via_me(
        self, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        mocker.patch(
            "backend.api.endpoints.auth_ep.ldap_authenticate", return_value=(True, None)
        )
        await self._domain_user()
        async with _fresh_client() as client:
            assert (
                await client.post(
                    "/auth/login", json={"username": "domain.only", "password": "ad-pass"}
                )
            ).status_code == 200

            monkeypatch.setenv("LDAP_ENABLED", "false")

            resp = await client.get("/auth/me")
            assert resp.status_code == 401
            assert resp.headers[SESSION_ENDED_HEADER] == SESSION_ENDED_DOMAIN_OFF
            assert not client.cookies.get(COOKIE_NAME)

    async def test_local_session_unaffected(self, viewer_client, monkeypatch):
        monkeypatch.setenv("LDAP_ENABLED", "false")
        assert (await viewer_client.get("/lumgs/")).status_code == 200

    async def test_auto_login_refuses_a_password_less_default_user(
        self, anon_client, clean_db, monkeypatch
    ):
        # DEFAULT_USERNAME pointing at a domain record: minting a session here
        # and refusing it on the next request would be a loop.
        await self._domain_user("auto.default")
        monkeypatch.setenv("LDAP_ENABLED", "false")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        monkeypatch.setenv("DEFAULT_USERNAME", "auto.default")
        resp = await anon_client.get("/auth/me")
        assert resp.status_code == 401
        assert COOKIE_NAME not in anon_client.cookies

    async def test_password_reset_still_refused(self, admin_client, monkeypatch):
        # The escape hatch stays shut: a local password minted here would not go
        # away when LDAP comes back, leaving a credential that outlives the
        # domain account it was made for.
        monkeypatch.setenv("LDAP_ENABLED", "false")
        uid = await self._domain_user()
        assert (await admin_client.post(f"/auth/users/{uid}/reset-password")).status_code == 400

    async def test_admin_can_see_the_mode(self, admin_client, monkeypatch):
        monkeypatch.setenv("LDAP_ENABLED", "false")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        resp = await admin_client.get("/auth/mode")
        assert resp.status_code == 200
        assert resp.json() == {"ldap_enabled": False, "auto_login": True}

    async def test_mode_is_admin_only(self, viewer_client):
        assert (await viewer_client.get("/auth/mode")).status_code == 403


# ── LDAP login (domain bind + auto-provisioning) ─────────────────────────────
# The bind itself is mocked at the point of use (auth_ep imported the function),
# so no LDAP server is needed; the provisioning matrix is what's under test:
#   LDAP on + AUTO_LOGIN on  → unknown domain user becomes an ACTIVE viewer-all
#   LDAP on + AUTO_LOGIN off → unknown domain user is created INACTIVE (pending)
#   existing DB record always wins over auto-viewer (incl. active=False = ban)
class TestLdapLogin:
    def _bind(self, mocker, ok: bool, display_name: str | None = None):
        return mocker.patch(
            "backend.api.endpoints.auth_ep.ldap_authenticate",
            return_value=(ok, display_name),
        )

    async def _find_user(self, username: str) -> AppUser | None:
        async with async_session_factory() as session:
            return (
                await session.execute(
                    select(AppUser).where(AppUser.username == username)
                )
            ).scalar_one_or_none()

    async def test_auto_login_provisions_active_viewer(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        self._bind(mocker, True)

        resp = await anon_client.post(
            "/auth/login", json={"username": "Domain.User", "password": "dompass"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["username"] == "domain.user"
        assert body["role"] == "viewer"
        assert body["active"] is True
        assert body["allowed_branch_ids"] == []  # no restrictions = all branches
        assert COOKIE_NAME in anon_client.cookies

        user = await self._find_user("domain.user")
        assert user is not None
        assert user.password_hash is None  # domain-only account

    async def test_display_name_taken_from_ldap(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        self._bind(mocker, True, display_name="Верещак Володимир")

        resp = await anon_client.post(
            "/auth/login", json={"username": "named.user", "password": "dompass"}
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Верещак Володимир"

    async def test_display_name_backfilled_on_existing_account(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        # Account provisioned before the AD-name lookup existed: display_name
        # equals the login. Next successful domain login backfills it.
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        self._bind(mocker, True, display_name="Справжнє Ім'я")

        await _insert_user(
            username="old.user", display_name="old.user",
            role="viewer", active=True, password_hash=None,
        )
        resp = await anon_client.post(
            "/auth/login", json={"username": "old.user", "password": "dompass"}
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Справжнє Ім'я"

    async def test_admin_set_display_name_not_overwritten(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        self._bind(mocker, True, display_name="Ім'я з AD")

        await _insert_user(
            username="custom.user", display_name="Ім'я від адміна",
            role="viewer", active=True, password_hash=None,
        )
        resp = await anon_client.post(
            "/auth/login", json={"username": "custom.user", "password": "dompass"}
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Ім'я від адміна"

    async def test_without_auto_login_creates_pending_inactive(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "false")
        self._bind(mocker, True)

        resp = await anon_client.post(
            "/auth/login", json={"username": "newbie", "password": "dompass"}
        )
        assert resp.status_code == 403
        assert "створено" in resp.json()["detail"]
        assert COOKIE_NAME not in anon_client.cookies

        user = await self._find_user("newbie")
        assert user is not None
        assert user.active is False
        assert user.role == "viewer"

    async def test_pending_user_second_login_still_denied_no_duplicate(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "false")
        self._bind(mocker, True)

        first = await anon_client.post(
            "/auth/login", json={"username": "pending", "password": "dompass"}
        )
        assert first.status_code == 403
        second = await anon_client.post(
            "/auth/login", json={"username": "pending", "password": "dompass"}
        )
        assert second.status_code == 403
        assert "Доступ не надано" in second.json()["detail"]

        async with async_session_factory() as session:
            count = len((await session.execute(
                select(AppUser).where(AppUser.username == "pending")
            )).scalars().all())
        assert count == 1

    async def test_existing_user_keeps_db_rights(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        # Domain creds for an account that exists in the DB with a configured
        # role must NOT be demoted to auto-viewer.
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        self._bind(mocker, True)

        branch_id = await _insert_branch()
        user_id = await _insert_user(
            username="configured", role="viewer", active=True, password_hash=None
        )
        async with async_session_factory() as session:
            from backend.db.models.app_user_model import AppUserBranchAccess
            session.add(AppUserBranchAccess(user_id=user_id, branch_id=branch_id))
            await session.commit()

        resp = await anon_client.post(
            "/auth/login", json={"username": "configured", "password": "dompass"}
        )
        assert resp.status_code == 200
        assert resp.json()["allowed_branch_ids"] == [branch_id]

    async def test_deactivated_user_denied_even_with_valid_domain_password(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "true")  # auto-viewer mode must not bypass the ban
        self._bind(mocker, True)

        await _insert_user(
            username="banned", role="viewer", active=False, password_hash=None
        )
        resp = await anon_client.post(
            "/auth/login", json={"username": "banned", "password": "dompass"}
        )
        assert resp.status_code == 403
        assert COOKIE_NAME not in anon_client.cookies

    async def test_failed_bind_401_and_no_user_created(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("AUTO_LOGIN", "true")
        self._bind(mocker, False)

        resp = await anon_client.post(
            "/auth/login", json={"username": "intruder", "password": "badpass"}
        )
        assert resp.status_code == 401
        assert await self._find_user("intruder") is None

    async def test_local_password_still_works_with_ldap_on(
        self, anon_client, seed_users, monkeypatch, mocker
    ):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        bind = self._bind(mocker, False)

        resp = await anon_client.post(
            "/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"
        bind.assert_not_called()  # local password matched → no LDAP round-trip

    async def test_auto_login_disabled_when_ldap_on(
        self, anon_client, seed_users, monkeypatch
    ):
        monkeypatch.setenv("AUTO_LOGIN", "true")
        monkeypatch.setenv("DEFAULT_USERNAME", VIEWER_USERNAME)
        monkeypatch.setenv("LDAP_ENABLED", "true")
        resp = await anon_client.get("/auth/me")
        assert resp.status_code == 401  # form login required

    async def test_auto_login_works_when_ldap_off(
        self, anon_client, seed_users, monkeypatch
    ):
        monkeypatch.setenv("AUTO_LOGIN", "true")
        monkeypatch.setenv("DEFAULT_USERNAME", VIEWER_USERNAME)
        monkeypatch.setenv("LDAP_ENABLED", "false")
        resp = await anon_client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == VIEWER_USERNAME
