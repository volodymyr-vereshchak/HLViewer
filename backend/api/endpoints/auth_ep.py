"""
JWT Authentication endpoints.

POST /auth/login  — username + password → JWT httpOnly cookie
POST /auth/logout — clear cookie
GET  /auth/me     — returns current user from JWT
GET  /auth/users  — list all users (admin only)
"""

import asyncio
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import bcrypt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.app_user_dao import AppUserDao
from backend.db.engine import async_session_factory, get_session
from backend.services.ldap_auth import ldap_authenticate, ldap_enabled
from backend.db.models.app_user_model import AppUser, AppUserRead
from backend.db.models.lumg_model import Lumg
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.line_model import Line

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ── crypto ────────────────────────────────────────────────────────────────────
# Fail-closed: never silently fall back to a hardcoded secret. The old default
# ("hlviewer-dev-secret-change-in-prod") is committed to the public repo, so any
# deployment that started without JWT_SECRET would sign tokens with a publicly
# known key — trivially forgeable admin tokens. Refuse to start unless a real
# secret is provided. DEBUG=true allows a throwaway dev secret for local runs.
_DEV_JWT_SECRET = "hlviewer-dev-secret-change-in-prod"
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET or JWT_SECRET == _DEV_JWT_SECRET:
    if os.getenv("DEBUG", "false").lower() == "true":
        JWT_SECRET = _DEV_JWT_SECRET  # local/dev only — never reached in prod
    else:
        raise RuntimeError(
            "JWT_SECRET is not set (or is the insecure default). Set a strong, "
            "unique JWT_SECRET in the environment before starting in production."
        )
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 12
JWT_REMEMBER_ME_DAYS = 30
# Sliding session: any authenticated request whose token has less than this
# fraction of its lifetime left gets a fresh cookie (same claims, same lifetime).
# So an actively-used tab keeps working; see _maybe_refresh_token + the
# sliding-session middleware in api/main.py.
JWT_REFRESH_BELOW_RATIO = 0.5
# Absolute cap: sliding refresh is allowed only within this window from the
# FIRST login (the token's `iat`, which is preserved across refreshes). After
# it, the token is left to expire naturally and the user must re-authenticate
# (seamless under AUTO_LOGIN). Prevents an active tab from living forever.
JWT_ABSOLUTE_MAX_SECONDS = 30 * 86400  # 30 days
COOKIE_NAME = "hlviewer_token"
# Set COOKIE_SECURE=true when serving over HTTPS so the auth cookie is not sent
# over plain HTTP. Defaults to false for internal HTTP deployments.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
# Shown when a token is refused because the account's rights changed under it.
# Distinct from the plain "not authenticated" so the reason reaches the user.
PERMS_CHANGED_DETAIL = "Права доступу змінено. Увійдіть у систему знову"
# ...and marked on the response, so the frontend can tell "your rights changed"
# apart from an ordinary expiry and say so in the user's own language instead of
# echoing this string back at them.
SESSION_ENDED_HEADER = "X-Session-Ended"
SESSION_ENDED_PERMS = "perms-changed"
# A domain account on a server whose domain login has been switched off. Such an
# account cannot authenticate by any path, so it must not keep a session either.
DOMAIN_LOGIN_OFF_DETAIL = (
    "Вхід за доменними обліковими даними вимкнено на сервері. "
    "Зверніться до адміністратора"
)
SESSION_ENDED_DOMAIN_OFF = "domain-login-off"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_token(
    user_id: int,
    remember_me: bool = False,
    role: str | None = None,
    branch_ids: list[int] | None = None,
    perms_version: int | None = None,
) -> str:
    delta = timedelta(days=JWT_REMEMBER_ME_DAYS) if remember_me else timedelta(hours=JWT_EXPIRE_HOURS)
    now = datetime.now(timezone.utc)
    expire = now + delta
    # ttl = the token's full lifetime in seconds. Embedded so the sliding-session
    # middleware can re-mint with the same lifetime (and cookie max_age) without
    # having to know whether this was a remember_me login. iat = first-login time,
    # preserved across refreshes to enforce the absolute session cap.
    payload = {
        "sub": str(user_id),
        "exp": expire,
        "ttl": int(delta.total_seconds()),
        "iat": int(now.timestamp()),
    }
    # Embed role + allowed branches so per-request auth can skip the
    # AppUserBranchAccess lookup. Read back in get_branch_filter.
    if role is not None:
        payload["role"] = role
    if branch_ids is not None:
        payload["branches"] = branch_ids
    # pv = the account's rights generation at login. Re-checked on every request
    # so that changing those rights ends the sessions that predate the change.
    if perms_version is not None:
        payload["pv"] = perms_version
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _maybe_refresh_token(token: str) -> Optional[tuple[str, int]]:
    """For the sliding session: if `token` is valid but past the refresh
    threshold (less than JWT_REFRESH_BELOW_RATIO of its lifetime remaining),
    return (new_token, max_age_seconds) carrying the same claims and lifetime.
    Returns None if the token is invalid, lacks a ttl claim (legacy), or is
    still fresh enough."""
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    exp = claims.get("exp")
    ttl = claims.get("ttl")
    iat = claims.get("iat")
    if not exp or not ttl:
        return None  # legacy token without ttl → leave it to normal expiry/re-auth
    now = datetime.now(timezone.utc).timestamp()
    # Absolute cap: once the session is older than the max, stop sliding and let
    # the current token expire on its own.
    if iat and (now - iat) >= JWT_ABSOLUTE_MAX_SECONDS:
        return None
    if (exp - now) > ttl * JWT_REFRESH_BELOW_RATIO:
        return None  # still fresh enough, don't churn the cookie
    expire = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    payload = {"sub": claims["sub"], "exp": expire, "ttl": ttl}
    if iat:
        payload["iat"] = iat  # preserve original login time across refreshes
    if "role" in claims:
        payload["role"] = claims["role"]
    if "branches" in claims:
        payload["branches"] = claims["branches"]
    if "pv" in claims:
        # Carried, not re-read: the refresh must not silently upgrade a token
        # whose rights generation is already out of date. Such a token is
        # refused by the guard before it ever reaches this middleware.
        payload["pv"] = claims["pv"]
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), ttl


# ── helpers ───────────────────────────────────────────────────────────────────
async def _issue_session(
    session: AsyncSession,
    user: AppUser,
    response: Response,
    remember_me: bool = False,
) -> AppUserRead:
    """Mint the JWT cookie for an authenticated user and return their profile.
    Shared by the local-password and LDAP login paths and by AUTO_LOGIN."""
    user_read = await _build_user_read(session, user)
    token = _create_token(
        user.id,
        remember_me=remember_me,
        role=user.role,
        branch_ids=user_read.allowed_branch_ids,
        perms_version=user.perms_version,
    )
    max_age = JWT_REMEMBER_ME_DAYS * 86400 if remember_me else JWT_EXPIRE_HOURS * 3600
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        max_age=max_age,
        samesite="lax",
    )
    return user_read


async def _build_user_read(session: AsyncSession, user: AppUser) -> AppUserRead:
    branch_ids = await AppUserDao(session=session).branch_ids(user.id)
    return AppUserRead(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        active=user.active,
        created_at=user.created_at,
        updated_at=user.updated_at,
        allowed_branch_ids=branch_ids,
        has_password=bool(user.password_hash),
    )


def decode_jwt(token: Optional[str]) -> Optional[dict]:
    """Decode a JWT cookie value outside of a request dependency (used by the
    central auth middleware). Returns the claims dict, or None if the token is
    missing or invalid."""
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


def perms_current(claims: dict, user: AppUser) -> bool:
    """Does this token still describe the rights the account actually has?

    Tokens minted before perms_version existed carry no `pv`; they are read as
    generation 1, which is what every untouched account has. So the change does
    not log the whole userbase out on deploy — a token only goes stale once an
    admin actually edits that account.
    """
    return int(claims.get("pv", 1)) == int(user.perms_version or 1)


def session_refusal(user: Optional[AppUser], claims: dict) -> Optional[str]:
    """Why this token must not be honoured any more, or None if it still may be.

    One place for the whole policy, because it is checked from three: the
    central guard (which runs outside the dependency system), the
    get_current_user dependency, and /auth/me, which is public and therefore
    passes the guard untouched. They disagreed once already — the guard trusted
    the role baked into the token while /auth/me read the database — and the
    result was an admin panel that appeared but refused every save.
    """
    if not user or not user.active:
        return "User not found or inactive"
    # A domain account has no password here; the domain is the only thing that
    # can vouch for it. With domain login switched off it cannot log in, so
    # letting the sessions it already had run on until they expire — up to 30
    # days — is the one hole the login check does not cover.
    if not user.password_hash and not ldap_enabled():
        return DOMAIN_LOGIN_OFF_DETAIL
    if not perms_current(claims, user):
        return PERMS_CHANGED_DETAIL
    return None


def session_ended_marker(detail: str) -> Optional[str]:
    """Value for the X-Session-Ended header: names the sessions the server ended
    on purpose, so the login screen can explain itself instead of just appearing.
    None for an ordinary expiry, which needs no explanation."""
    return {
        PERMS_CHANGED_DETAIL: SESSION_ENDED_PERMS,
        DOMAIN_LOGIN_OFF_DETAIL: SESSION_ENDED_DOMAIN_OFF,
    }.get(detail)


async def resolve_session_user(claims: dict) -> tuple[Optional[AppUser], str]:
    """The account behind a decoded token, or (None, reason) if the token must
    not be honoured any more.

    Used by the central auth_guard middleware, which runs outside the dependency
    system and therefore opens its own short-lived session (closed before the
    request is handed on, so it never holds a pool slot alongside the endpoint's
    own session).

    The DB read is the point of the exercise: role and branches live in the
    token, so without re-reading something per request there is no moment at
    which a rights change can take effect.
    """
    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return None, "Invalid token"
    async with async_session_factory() as session:
        user = await AppUserDao(session=session).get_by_id(user_id)
    refusal = session_refusal(user, claims)
    return (None, refusal) if refusal else (user, "")


async def get_token_claims(hlviewer_token: Optional[str] = Cookie(default=None)) -> dict:
    """Decode and validate the JWT from the httpOnly cookie, once per request.
    FastAPI caches this within a request, so dependent deps share one decode."""
    if not hlviewer_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        return jwt.decode(hlviewer_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def get_current_user(
    claims: dict = Depends(get_token_claims),
    session: AsyncSession = Depends(get_session),
) -> AppUser:
    """Dependency: validates the user from the token still exists and is active."""
    try:
        user_id = int(claims["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = await AppUserDao(session=session).get_by_id(user_id)
    refusal = session_refusal(user, claims)
    if refusal:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=refusal)
    return user


async def require_admin(user: AppUser = Depends(get_current_user)) -> AppUser:
    """Dependency: the current user must have the admin role (403 otherwise)."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


async def get_branch_filter(
    user: AppUser = Depends(get_current_user),
    claims: dict = Depends(get_token_claims),
    session: AsyncSession = Depends(get_session),
) -> list[int] | None:
    """None = no restrictions (admin, or viewer with no branch entries). list[int] = restricted.

    branch_ids come from the JWT (embedded at login) to avoid an AppUserBranchAccess
    query on every request. Tokens issued before this change have no 'branches' claim,
    so we fall back to the DB until the user re-logs in.

    Changing a user's branches bumps their perms_version, which kills the tokens
    that carry the old list — so the change lands on the next login, and there is
    no window in which the stale list is still honoured."""
    if user.role == "admin":
        return None
    if "branches" in claims:
        branch_ids = claims["branches"]
        return None if not branch_ids else branch_ids
    # Legacy token (no claim) → read from DB
    branch_ids = await AppUserDao(session=session).branch_ids(user.id)
    return None if not branch_ids else branch_ids


async def get_allowed_line_ids(
    branch_ids: list[int] | None = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
) -> list[int] | None:
    """None = admin (no restrictions). list[int] = allowed line_ids for viewer."""
    if branch_ids is None:
        return None
    if not branch_ids:
        return []
    lumg_ids = list((await session.execute(
        select(Lumg.id).where(Lumg.branch_id.in_(branch_ids))
    )).scalars())
    if not lumg_ids:
        return []
    calc_ids = list((await session.execute(
        select(GasVolumeCalc.id).where(GasVolumeCalc.lumg_id.in_(lumg_ids))
    )).scalars())
    if not calc_ids:
        return []
    line_ids = list((await session.execute(
        select(Line.id).where(Line.gas_volume_calc_id.in_(calc_ids))
    )).scalars())
    return line_ids


# ── schemas ───────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


# ── endpoints ─────────────────────────────────────────────────────────────────
@router.post("/login", response_model=AppUserRead)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    uname = body.username.strip().lower()
    user = await AppUserDao(session=session).get_by_username(uname)

    # 1. Local password — always available, regardless of the LDAP mode.
    if user and user.password_hash and _verify_password(body.password, user.password_hash):
        if not user.active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Обліковий запис деактивовано")
        return await _issue_session(session, user, response, body.remember_me)

    # 2. Domain credentials (LDAP bind). Rights always come from app_user:
    #    an existing record wins (including active=False — deactivation is a
    #    ban that a valid domain password must NOT bypass). Unknown domain
    #    users are auto-provisioned: with AUTO_LOGIN they enter immediately
    #    as an unrestricted viewer; without it the record is created inactive
    #    and waits for an admin to activate it and assign role/branches.
    if ldap_enabled():
        # ldap3 is synchronous and its timeouts are 5s connect + 10s receive,
        # so binding on the event loop stalls every other request this worker
        # is serving. Eight simultaneous logins against an unreachable domain
        # controller would otherwise freeze the whole API.
        ldap_ok, ldap_name = await asyncio.to_thread(
            ldap_authenticate, uname, body.password
        )
        if ldap_ok:
            if user:
                if not user.active:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Доступ не надано. Зверніться до адміністратора",
                    )
                # Backfill the AD display name for accounts that never got one
                # (auto-provisioned before the lookup existed, or created by an
                # admin with the bare login). Admin-set names are kept.
                if ldap_name and (not user.display_name or user.display_name == user.username):
                    user.display_name = ldap_name
                    session.add(user)
                    await session.commit()
                    await session.refresh(user)  # re-load onupdate columns (updated_at)
                return await _issue_session(session, user, response, body.remember_me)

            auto_viewer = os.getenv("AUTO_LOGIN", "false").lower() == "true"
            user = AppUser(
                username=uname,
                display_name=ldap_name or body.username.strip(),
                password_hash=None,
                role="viewer",
                active=auto_viewer,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info(f"LDAP auto-provisioned user '{uname}' (active={auto_viewer})")

            if not auto_viewer:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Обліковий запис створено. Зверніться до адміністратора для надання прав",
                )
            return await _issue_session(session, user, response, body.remember_me)

    # A domain account on a server with domain login switched off. Without this
    # it falls into the generic 401 below and the user is told their password is
    # wrong — it is not, and no amount of retyping will help. There is nothing
    # here to check it against: no local hash, and no AD to ask.
    if user and not user.password_hash and not ldap_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Вхід за доменними обліковими даними вимкнено на сервері. "
                "Зверніться до адміністратора"
            ),
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Невірний логін або пароль",
    )


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME)
    return {"ok": True}


@router.get("/me", response_model=AppUserRead)
async def get_me(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    dao = AppUserDao(session=session)

    # 1. Try valid cookie
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            user = await dao.get_by_id(int(payload["sub"]))
            refusal = session_refusal(user, payload)
            if user and not refusal:
                return await _build_user_read(session, user)
            marker = session_ended_marker(refusal or "")
            if marker:
                # This endpoint is public and is also the frontend's 401
                # recovery path: answering it from the database would hand back
                # a profile the token can no longer act with, and the app would
                # hydrate as signed in and then fail every call. Drop the cookie
                # so the next load goes to the login form.
                # Returned rather than raised: headers set on the injected
                # Response are dropped when an HTTPException unwinds, and the
                # Set-Cookie is the whole point here.
                stale = JSONResponse(status_code=401, content={"detail": refusal})
                stale.headers[SESSION_ENDED_HEADER] = marker
                stale.delete_cookie(key=COOKIE_NAME)
                return stale
        except Exception:
            pass

    # 2. AUTO_LOGIN — issue real JWT for default user. With LDAP enabled this
    #    automatic sign-in is OFF (everyone must present credentials); AUTO_LOGIN
    #    then only controls whether domain users are provisioned as active
    #    viewers (see login above).
    if os.getenv("AUTO_LOGIN", "false").lower() == "true" and not ldap_enabled():
        default_username = os.getenv("DEFAULT_USERNAME")
        if default_username:
            default_user = await dao.get_by_username(default_username)
            # A password-less default account would be a misconfiguration:
            # DEFAULT_USERNAME pointed at a record that came from a domain
            # login. Minting a session for it here and having the guard refuse
            # it on the very next request is a loop, so refuse it once, loudly.
            if default_user and default_user.active and default_user.password_hash:
                return await _issue_session(session, default_user, response)
            if default_user and not default_user.password_hash:
                logger.warning(
                    "AUTO_LOGIN: DEFAULT_USERNAME '%s' is a domain account with no "
                    "local password — cannot sign in automatically while LDAP is off",
                    default_username,
                )

    raise HTTPException(status_code=401, detail="Not authenticated")


class AuthMode(BaseModel):
    """How this server authenticates, for the admin screens to explain itself.

    Both are deployment switches, not stored settings — the admin panel cannot
    change them, it can only tell the operator what the server is currently
    doing and which accounts that leaves stranded.
    """

    ldap_enabled: bool
    auto_login: bool


@router.get("/mode", response_model=AuthMode)
async def get_auth_mode(admin: AppUser = Depends(require_admin)):
    return AuthMode(
        ldap_enabled=ldap_enabled(),
        auto_login=os.getenv("AUTO_LOGIN", "false").lower() == "true",
    )


@router.get("/users", response_model=List[AppUserRead])
async def list_users(
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    users = await AppUserDao(session=session).get_all_ordered()
    return [await _build_user_read(session, u) for u in users]


class UserCreate(BaseModel):
    username: str
    display_name: Optional[str] = None
    role: str = "viewer"
    branch_ids: list[int] = []
    password: Optional[str] = None  # if None → auto-generate


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    branch_ids: Optional[list[int]] = None


class UserCreateResponse(BaseModel):
    user: AppUserRead
    password: str


def _generate_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.post("/users", response_model=UserCreateResponse)
async def create_user(
    body: UserCreate,
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    plain_pw = body.password if body.password else _generate_password()
    dao = AppUserDao(session=session)

    if await dao.get_by_username(body.username.strip().lower()):
        raise HTTPException(status_code=409, detail="Користувач вже існує")

    user = AppUser(
        username=body.username.strip().lower(),
        display_name=body.display_name,
        password_hash=hash_password(plain_pw),
        role=body.role,
        active=True,
    )
    session.add(user)
    await session.flush()
    await dao.set_branch_access(user.id, body.branch_ids)
    await session.commit()
    await session.refresh(user)

    return UserCreateResponse(user=await _build_user_read(session, user), password=plain_pw)


@router.patch("/users/{user_id}", response_model=AppUserRead)
async def update_user(
    user_id: int,
    body: UserUpdate,
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    dao = AppUserDao(session=session)
    user = await dao.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Guard: never leave the system without an active admin. If this update
    # would stop the user from being an active admin (deactivation or role
    # change), make sure at least one other active admin remains.
    new_role = body.role if body.role is not None else user.role
    new_active = body.active if body.active is not None else user.active
    was_active_admin = user.role == "admin" and user.active
    will_be_active_admin = new_role == "admin" and new_active
    if was_active_admin and not will_be_active_admin:
        if await dao.other_active_admins(user_id) == 0:
            raise HTTPException(
                status_code=400,
                detail="Не можна деактивувати або змінити роль останнього активного адміністратора",
            )

    # Only a real change ends the user's sessions: the admin screen submits the
    # whole form, so comparing here is what keeps a display-name edit from
    # logging someone out. Branch access is compared as a set — order and
    # duplicates in the payload are not a change.
    rights_changed = (body.role is not None and body.role != user.role) or (
        body.active is not None and body.active != user.active
    )
    if body.branch_ids is not None and not rights_changed:
        rights_changed = set(body.branch_ids) != set(await dao.branch_ids(user.id))

    if body.display_name is not None:
        user.display_name = body.display_name
    if body.role is not None:
        user.role = body.role
    if body.active is not None:
        user.active = body.active
    if body.branch_ids is not None:
        await dao.set_branch_access(user.id, body.branch_ids)
    if rights_changed:
        user.perms_version = (user.perms_version or 1) + 1

    session.add(user)
    await session.commit()
    await session.refresh(user)

    return await _build_user_read(session, user)


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    plain_pw = _generate_password()

    user = await AppUserDao(session=session).get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # A domain account has no password of ours to reset. Writing one would not
    # just be pointless: the login flow checks the local password FIRST, so the
    # account would stop being authenticated against AD altogether — it would
    # survive the domain password being changed, and survive being disabled
    # there. Deleting the account is the way to revoke a domain user here.
    if not user.password_hash:
        raise HTTPException(
            status_code=400,
            detail=(
                "Це доменний обліковий запис — пароль зберігається в Active "
                "Directory. Змінити його можна лише засобами домену."
            ),
        )
    user.password_hash = hash_password(plain_pw)
    # A reset whose point is to lock someone out of an account is pointless if
    # the session opened with the old password keeps running.
    user.perms_version = (user.perms_version or 1) + 1
    session.add(user)
    await session.commit()

    return {"password": plain_pw}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    # Guard: an admin may not delete their own account
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Не можна видалити власний обліковий запис")

    dao = AppUserDao(session=session)
    user = await dao.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Guard: never leave the system without an active admin
    if user.role == "admin":
        if await dao.other_active_admins(user_id) == 0:
            raise HTTPException(status_code=400, detail="Не можна видалити останнього активного адміністратора")

    # Remove branch-access rows explicitly (FK is ON DELETE CASCADE, but this
    # avoids the ORM trying to null out loaded children).
    await dao.delete_branch_access(user_id)

    await session.delete(user)
    await session.commit()

    return {"deleted": True, "id": user_id}


auth_router = router
