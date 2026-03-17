"""
Windows Auth endpoints.

GET /auth/me  — returns current user, auto-provisions on first visit.
GET /auth/users — list all users (admin, Phase 2 placeholder).
"""

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Request, status
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.app_user_model import AppUser, AppUserBranchAccess, AppUserRead
from backend.settings import backend_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _extract_username(request: Request) -> str | None:
    """
    Read username from headers.

    Production: X-Remote-User set by IIS Windows Auth (DOMAIN\\username).
    Dev:        X-Debug-User allowed only when DEBUG=true in settings.
    """
    raw = request.headers.get("X-Remote-User")
    if not raw and backend_settings.get("DEBUG", "false").lower() == "true":
        raw = request.headers.get("X-Debug-User")
    if not raw:
        return None
    # Strip domain prefix: DOMAIN\username → username
    if "\\" in raw:
        raw = raw.split("\\", 1)[1]
    return raw.strip().lower()


@router.get("/me", response_model=AppUserRead, summary="Get current Windows Auth user")
async def get_me(request: Request):
    username = _extract_username(request)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated user (missing X-Remote-User header)",
        )

    async with async_session_factory() as session:
        result = await session.execute(
            select(AppUser).where(AppUser.windows_username == username)
        )
        user = result.scalar_one_or_none()

        if user is None:
            is_debug = backend_settings.get("DEBUG", "false").lower() == "true"
            default_role = "admin" if is_debug else "viewer"
            user = AppUser(windows_username=username, role=default_role, active=True)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info("Auto-provisioned new user: %s (role=%s)", username, default_role)

        # Load branch access IDs
        access_result = await session.execute(
            select(AppUserBranchAccess.branch_id).where(
                AppUserBranchAccess.user_id == user.id
            )
        )
        branch_ids = list(access_result.scalars().all())

    return AppUserRead(
        id=user.id,
        windows_username=user.windows_username,
        display_name=user.display_name,
        role=user.role,
        active=user.active,
        created_at=user.created_at,
        updated_at=user.updated_at,
        allowed_branch_ids=branch_ids,
    )


@router.get("/users", response_model=List[AppUserRead], summary="List all users (admin, Phase 2)")
async def list_users():
    """Placeholder — open in Phase 1, will require admin role in Phase 2."""
    async with async_session_factory() as session:
        result = await session.execute(select(AppUser).order_by(AppUser.windows_username))
        users = result.scalars().all()

    out = []
    for user in users:
        async with async_session_factory() as session:
            access_result = await session.execute(
                select(AppUserBranchAccess.branch_id).where(
                    AppUserBranchAccess.user_id == user.id
                )
            )
            branch_ids = list(access_result.scalars().all())
        out.append(
            AppUserRead(
                id=user.id,
                windows_username=user.windows_username,
                display_name=user.display_name,
                role=user.role,
                active=user.active,
                created_at=user.created_at,
                updated_at=user.updated_at,
                allowed_branch_ids=branch_ids,
            )
        )
    return out


auth_router = router
