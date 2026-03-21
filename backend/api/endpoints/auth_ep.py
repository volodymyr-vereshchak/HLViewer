"""
JWT Authentication endpoints.

POST /auth/login  — username + password → JWT httpOnly cookie
POST /auth/logout — clear cookie
GET  /auth/me     — returns current user from JWT
GET  /auth/users  — list all users (admin only)
"""

import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import bcrypt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.app_user_model import AppUser, AppUserBranchAccess, AppUserRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ── crypto ────────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "hlviewer-dev-secret-change-in-prod")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 12
JWT_REMEMBER_ME_DAYS = 30
COOKIE_NAME = "hlviewer_token"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_token(user_id: int, remember_me: bool = False) -> str:
    delta = timedelta(days=JWT_REMEMBER_ME_DAYS) if remember_me else timedelta(hours=JWT_EXPIRE_HOURS)
    expire = datetime.now(timezone.utc) + delta
    return jwt.encode({"sub": str(user_id), "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ── helpers ───────────────────────────────────────────────────────────────────
async def _build_user_read(user: AppUser) -> AppUserRead:
    async with async_session_factory() as session:
        result = await session.execute(
            select(AppUserBranchAccess.branch_id).where(AppUserBranchAccess.user_id == user.id)
        )
        branch_ids = list(result.scalars().all())
    return AppUserRead(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        active=user.active,
        created_at=user.created_at,
        updated_at=user.updated_at,
        allowed_branch_ids=branch_ids,
    )


async def get_current_user(hlviewer_token: Optional[str] = Cookie(default=None)) -> AppUser:
    """Dependency: extracts and validates JWT from httpOnly cookie."""
    if not hlviewer_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(hlviewer_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    async with async_session_factory() as session:
        result = await session.execute(select(AppUser).where(AppUser.id == user_id))
        user = result.scalar_one_or_none()

    if not user or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def get_branch_filter(
    user: AppUser = Depends(get_current_user),
) -> list[int] | None:
    """None = admin (no restrictions). list[int] = allowed branch_ids for viewer."""
    if user.role == "admin":
        return None
    async with async_session_factory() as session:
        result = await session.execute(
            select(AppUserBranchAccess.branch_id).where(AppUserBranchAccess.user_id == user.id)
        )
        return list(result.scalars().all())


# ── schemas ───────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


# ── endpoints ─────────────────────────────────────────────────────────────────
@router.post("/login", response_model=AppUserRead)
async def login(body: LoginRequest, response: Response):
    async with async_session_factory() as session:
        result = await session.execute(
            select(AppUser).where(AppUser.username == body.username.strip().lower())
        )
        user = result.scalar_one_or_none()

    if not user or not user.password_hash or not _verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невірний логін або пароль",
        )

    if not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Обліковий запис деактивовано")

    token = _create_token(user.id, remember_me=body.remember_me)
    max_age = JWT_REMEMBER_ME_DAYS * 86400 if body.remember_me else JWT_EXPIRE_HOURS * 3600
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=max_age,
        samesite="lax",
    )
    return await _build_user_read(user)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME)
    return {"ok": True}


@router.get("/me", response_model=AppUserRead)
async def get_me(user: AppUser = Depends(get_current_user)):
    return await _build_user_read(user)


@router.get("/users", response_model=List[AppUserRead])
async def list_users(user: AppUser = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    async with async_session_factory() as session:
        result = await session.execute(select(AppUser).order_by(AppUser.username))
        users = result.scalars().all()
    return [await _build_user_read(u) for u in users]


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


async def _set_branch_access(session, user_id: int, branch_ids: list[int]):
    existing = await session.execute(
        select(AppUserBranchAccess).where(AppUserBranchAccess.user_id == user_id)
    )
    for row in existing.scalars().all():
        await session.delete(row)
    for bid in branch_ids:
        session.add(AppUserBranchAccess(user_id=user_id, branch_id=bid))


@router.post("/users", response_model=UserCreateResponse)
async def create_user(body: UserCreate, admin: AppUser = Depends(get_current_user)):
    if admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    plain_pw = body.password if body.password else _generate_password()

    async with async_session_factory() as session:
        existing = (await session.execute(
            select(AppUser).where(AppUser.username == body.username.strip().lower())
        )).scalar_one_or_none()
        if existing:
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
        await _set_branch_access(session, user.id, body.branch_ids)
        await session.commit()
        await session.refresh(user)

    return UserCreateResponse(user=await _build_user_read(user), password=plain_pw)


@router.patch("/users/{user_id}", response_model=AppUserRead)
async def update_user(user_id: int, body: UserUpdate, admin: AppUser = Depends(get_current_user)):
    if admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    async with async_session_factory() as session:
        user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if body.display_name is not None:
            user.display_name = body.display_name
        if body.role is not None:
            user.role = body.role
        if body.active is not None:
            user.active = body.active
        if body.branch_ids is not None:
            await _set_branch_access(session, user.id, body.branch_ids)

        session.add(user)
        await session.commit()
        await session.refresh(user)

    return await _build_user_read(user)


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, admin: AppUser = Depends(get_current_user)):
    if admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    plain_pw = _generate_password()

    async with async_session_factory() as session:
        user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.password_hash = hash_password(plain_pw)
        session.add(user)
        await session.commit()

    return {"password": plain_pw}


auth_router = router
