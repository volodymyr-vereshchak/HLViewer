"""
GrmuBranch (Філіал ГРМУ) API Endpoints

Provides CRUD for branches and read access to per-branch device mappings.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.grmu_branch_model import (
    GrmuBranch,
    GrmuBranchCreate,
    GrmuBranchList,
    GrmuBranchUpdate,
    GrmuBranchDeviceMappingList,
)
from backend.services.grmu_branch_mappings import get_all_mappings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/grmu_branch", tags=["grmu_branch"])


# ─── Branch CRUD ──────────────────────────────────────────────────────────────


@router.get("/", response_model=List[GrmuBranchList], summary="List all branches")
async def list_branches(active_only: bool = Query(default=False)):
    async with async_session_factory() as session:
        stmt = select(GrmuBranch)
        if active_only:
            stmt = stmt.where(GrmuBranch.active == True)  # noqa: E712
        stmt = stmt.order_by(GrmuBranch.name)
        result = await session.execute(stmt)
        branches = result.scalars().all()
    return branches


@router.get(
    "/{branch_id}",
    response_model=GrmuBranchList,
    summary="Get a single branch by ID",
)
async def get_branch(branch_id: int):
    async with async_session_factory() as session:
        branch = await session.get(GrmuBranch, branch_id)
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    return branch


@router.post(
    "/",
    response_model=GrmuBranchList,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new branch",
)
async def create_branch(data: GrmuBranchCreate):
    branch = GrmuBranch.model_validate(data)
    async with async_session_factory() as session:
        session.add(branch)
        await session.commit()
        await session.refresh(branch)
    return branch


@router.patch(
    "/{branch_id}",
    response_model=GrmuBranchList,
    summary="Update a branch",
)
async def update_branch(branch_id: int, data: GrmuBranchUpdate):
    async with async_session_factory() as session:
        branch = await session.get(GrmuBranch, branch_id)
        if not branch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(branch, key, value)
        session.add(branch)
        await session.commit()
        await session.refresh(branch)
    return branch


# ─── Device Mappings ──────────────────────────────────────────────────────────


@router.get(
    "/{branch_id}/device-mappings",
    response_model=List[GrmuBranchDeviceMappingList],
    summary="Get device mappings for a branch (replaces Excel)",
)
async def get_device_mappings(branch_id: int):
    async with async_session_factory() as session:
        rows = await get_all_mappings(session, branch_id=branch_id)
    return rows


@router.get(
    "/device-mappings/all",
    response_model=List[GrmuBranchDeviceMappingList],
    summary="Get all device mappings across all branches",
)
async def get_all_device_mappings(
    branch_id: Optional[int] = Query(default=None, description="Optional branch filter"),
):
    async with async_session_factory() as session:
        rows = await get_all_mappings(session, branch_id=branch_id)
    return rows


grmu_branch_router = router
