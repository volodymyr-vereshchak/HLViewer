"""
GrmuBranch (Філіал ГРМУ) API Endpoints

Provides CRUD for branches and read access to per-branch device mappings.
"""

import asyncio
import io
import json
import logging
from dataclasses import asdict
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.engine import get_session
from backend.db.models.grmu_branch_model import (
    GrmuBranch,
    GrmuBranchCreate,
    GrmuBranchList,
    GrmuBranchUpdate,
    GrmuBranchDeviceMappingList,
    BranchDataPath,
    BranchDataPathRead,
    BranchDataPathUpsert,
    BranchConfigMapping,
    BranchConfigMappingRead,
    BranchConfigMappingUpsert,
    GrmuBranchDpdCredential,
    GrmuBranchDpdCredentialUpdate,
)
from backend.api.endpoints.auth_ep import get_branch_filter, require_admin
from backend.db.models.app_user_model import AppUser
from backend.db.models.lumg_model import Lumg
from backend.hl_engine.config_reader import ConfigReader
from backend.utils.path_utils import resolve_stored_path
from backend.services import branch_config_transfer
from backend.services.grmu_branch_mappings import get_all_mappings

_update_lock = asyncio.Lock()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/grmu_branch", tags=["grmu_branch"])


# ─── Branch CRUD ──────────────────────────────────────────────────────────────


@router.get("/", response_model=List[GrmuBranchList], summary="List all branches")
async def list_branches(
    active_only: bool = Query(default=False),
    branch_ids: list[int] | None = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(GrmuBranch)
    if active_only:
        stmt = stmt.where(GrmuBranch.active == True)  # noqa: E712
    if branch_ids is not None:
        stmt = stmt.where(GrmuBranch.id.in_(branch_ids))
    stmt = stmt.order_by(GrmuBranch.name)
    result = await session.execute(stmt)
    branches = result.scalars().all()
    return branches


@router.get(
    "/{branch_id}",
    response_model=GrmuBranchList,
    summary="Get a single branch by ID",
)
async def get_branch(branch_id: int, session: AsyncSession = Depends(get_session)):
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
async def create_branch(data: GrmuBranchCreate, session: AsyncSession = Depends(get_session)):
    branch = GrmuBranch.model_validate(data)
    session.add(branch)
    await session.commit()
    await session.refresh(branch)
    return branch


@router.delete(
    "/{branch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a branch",
)
async def delete_branch(branch_id: int, session: AsyncSession = Depends(get_session)):
    exists = await session.get(GrmuBranch, branch_id)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    await session.execute(text("DELETE FROM grmu_branch WHERE id = :id"), {"id": branch_id})
    await session.commit()


@router.patch(
    "/{branch_id}",
    response_model=GrmuBranchList,
    summary="Update a branch",
)
async def update_branch(branch_id: int, data: GrmuBranchUpdate, session: AsyncSession = Depends(get_session)):
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
async def get_device_mappings(branch_id: int, session: AsyncSession = Depends(get_session)):
    rows = await get_all_mappings(session, branch_id=branch_id)
    return rows


@router.get(
    "/device-mappings/all",
    response_model=List[GrmuBranchDeviceMappingList],
    summary="Get all device mappings across all branches",
)
async def get_all_device_mappings(
    branch_id: Optional[int] = Query(default=None, description="Optional branch filter"),
    session: AsyncSession = Depends(get_session),
):
    rows = await get_all_mappings(session, branch_id=branch_id)
    return rows


# ─── Branch Data Path ─────────────────────────────────────────────────────────


@router.get(
    "/{branch_id}/data-path",
    response_model=BranchDataPathRead,
    summary="Get data path for a branch",
)
async def get_branch_data_path(branch_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
    )
    dp = result.scalars().first()
    if not dp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data path not found")
    return dp


@router.put(
    "/{branch_id}/data-path",
    response_model=BranchDataPathRead,
    summary="Create or update data path for a branch",
)
async def upsert_branch_data_path(branch_id: int, body: BranchDataPathUpsert, session: AsyncSession = Depends(get_session)):
    branch = await session.get(GrmuBranch, branch_id)
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    result = await session.execute(
        select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
    )
    dp = result.scalars().first()
    if dp:
        dp.path = body.path
        dp.active = body.active
    else:
        dp = BranchDataPath(branch_id=branch_id, path=body.path, active=body.active)
        session.add(dp)
    await session.commit()
    await session.refresh(dp)
    return dp


@router.delete(
    "/{branch_id}/data-path",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete data path for a branch",
)
async def delete_branch_data_path(branch_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
    )
    dp = result.scalars().first()
    if not dp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data path not found")
    await session.delete(dp)
    await session.commit()


# ─── Config Preview & Mapping ──────────────────────────────────────────────────


@router.get(
    "/{branch_id}/config-preview",
    summary="Read CFG file and return list of GIS (LUMGs) with flows",
)
async def preview_branch_config(branch_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
    )
    dp = result.scalars().first()
    if not dp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config path not set for this branch")
    try:
        gis_list = ConfigReader(file=resolve_stored_path(dp.path)).read()
        # Return preview: gis_name + flow count + total line count (no sensitive data)
        return [
            {
                "gis_name": gis["gis_name"],
                "flow_count": len(gis["flows"]),
                "line_count": sum(len(f["lines"]) for f in gis["flows"]),
            }
            for gis in gis_list
        ]
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"File not found: {dp.path}")
    except Exception as e:
        logger.error(f"Error reading config for branch {branch_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{branch_id}/config-mappings",
    response_model=List[BranchConfigMappingRead],
    summary="Get saved GIS→LUMG mappings for a branch",
)
async def get_config_mappings(branch_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(BranchConfigMapping).where(BranchConfigMapping.branch_id == branch_id)
    )
    return result.scalars().all()


@router.put(
    "/{branch_id}/config-mappings",
    response_model=List[BranchConfigMappingRead],
    summary="Save GIS→LUMG mappings for a branch (replaces existing)",
)
async def upsert_config_mappings(branch_id: int, mappings: List[BranchConfigMappingUpsert], session: AsyncSession = Depends(get_session)):
    # Delete existing mappings for this branch
    existing = await session.execute(
        select(BranchConfigMapping).where(BranchConfigMapping.branch_id == branch_id)
    )
    for row in existing.scalars().all():
        await session.delete(row)
    # Execute the DELETEs before the INSERTs — otherwise SQLAlchemy's unit
    # of work flushes inserts first and re-saving an existing gis_name hits
    # uq_branch_config_mapping.
    await session.flush()

    # Insert new mappings
    new_rows = []
    for m in mappings:
        row = BranchConfigMapping(branch_id=branch_id, gis_name=m.gis_name, lumg_id=m.lumg_id)
        session.add(row)
        new_rows.append(row)

    await session.commit()
    for row in new_rows:
        await session.refresh(row)
    return new_rows


# ─── Branch Update ─────────────────────────────────────────────────────────────


@router.post(
    "/{branch_id}/update-names",
    summary="Update line names from ASK.CFG for a branch",
)
async def update_branch_names(branch_id: int, session: AsyncSession = Depends(get_session)):
    if _update_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Update is already in progress. Please try again later.",
        )
    async with _update_lock:
        result = await session.execute(
            select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
        )
        dp = result.scalars().first()
        if not dp or not dp.active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active config path for this branch",
            )
        # Load saved mappings
        mappings_result = await session.execute(
            select(BranchConfigMapping).where(BranchConfigMapping.branch_id == branch_id)
        )
        mappings = mappings_result.scalars().all()

        if not mappings:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No GIS→LUMG mappings saved for this branch. Configure mappings first.",
            )

        mapping_dict = {m.gis_name: m.lumg_id for m in mappings if m.lumg_id is not None}
        if not mapping_dict:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="All mappings have no LUMG assigned. Set LUMGs in the mapping config.",
            )

        try:
            await ConfigReader(file=resolve_stored_path(dp.path)).update_db_with_mapping(mapping_dict)
            return {"message": f"Names updated for branch {branch_id}", "last_updated": datetime.now().isoformat()}
        except FileNotFoundError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error(f"Error updating names for branch {branch_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ─── DPD Credentials (per branch) ────────────────────────────────────────────


@router.get(
    "/{branch_id}/dpd-credential",
    summary="Get DPD credentials for a branch (username only, no password)",
)
async def get_dpd_credential(branch_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(GrmuBranchDpdCredential).where(GrmuBranchDpdCredential.branch_id == branch_id)
    )
    cred = result.scalars().first()
    if not cred:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DPD credentials not found")
    return {"id": cred.id, "branch_id": cred.branch_id, "username": cred.username,
            "api_base_url": cred.api_base_url, "auth_url": cred.auth_url, "timeout_sec": cred.timeout_sec}


@router.put(
    "/{branch_id}/dpd-credential",
    summary="Create or update DPD credentials for a branch",
)
async def upsert_dpd_credential(branch_id: int, body: GrmuBranchDpdCredentialUpdate, session: AsyncSession = Depends(get_session)):
    branch = await session.get(GrmuBranch, branch_id)
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    result = await session.execute(
        select(GrmuBranchDpdCredential).where(GrmuBranchDpdCredential.branch_id == branch_id)
    )
    cred = result.scalars().first()
    update_data = body.model_dump(exclude_unset=True)
    if cred:
        for key, value in update_data.items():
            setattr(cred, key, value)
    else:
        required = {"username", "password"}
        missing = required - update_data.keys()
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Missing required fields: {', '.join(sorted(missing))}",
            )
        cred = GrmuBranchDpdCredential(branch_id=branch_id, **update_data)
        session.add(cred)
    await session.commit()
    await session.refresh(cred)
    return {"id": cred.id, "branch_id": cred.branch_id, "username": cred.username,
            "api_base_url": cred.api_base_url, "auth_url": cred.auth_url, "timeout_sec": cred.timeout_sec}


@router.delete(
    "/{branch_id}/dpd-credential",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete DPD credentials for a branch",
)
async def delete_dpd_credential(branch_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(GrmuBranchDpdCredential).where(GrmuBranchDpdCredential.branch_id == branch_id)
    )
    cred = result.scalars().first()
    if not cred:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DPD credentials not found")
    await session.delete(cred)
    await session.commit()


# ─── Config transfer (філія → центральний сервер) ─────────────────────────────


@router.get(
    "/{branch_id}/config-export",
    summary="Download this branch's configuration as a JSON bundle",
    response_class=StreamingResponse,
)
async def export_branch_config(
    branch_id: int,
    include_secrets: bool = Query(
        default=True,
        description="Put the DPD password into the file (it is stored in clear text)",
    ),
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """The file carries the DPD password, so this GET must stay admin-only.

    `auth_guard` only demands admin for write methods and for paths listed in
    `_ADMIN_PATH_MARKERS`; "config-export" is in that list, and `require_admin`
    here is the second lock on the same door.
    """
    try:
        bundle = await branch_config_transfer.export_branch(
            session, branch_id, include_secrets=include_secrets
        )
    except branch_config_transfer.BranchNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    # indent=2 + ensure_ascii=False: the file is meant to be read and diffed by
    # a person, and Cyrillic escaped to \uXXXX makes that impossible.
    body = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    stamp = datetime.now().strftime("%Y-%m-%d_%H_%M")
    label = bundle["branch"].get("short_name") or bundle["branch"]["name"]
    pretty = f"branch_{label}_{stamp}.json".replace(" ", "_")
    # HTTP headers are latin-1; the Ukrainian name goes in the RFC 5987 form and
    # an ASCII name stays as the fallback for anything that ignores it.
    disposition = (
        f'attachment; filename="branch_{branch_id}_{stamp}.json"; '
        f"filename*=UTF-8''{quote(pretty)}"
    )
    return StreamingResponse(
        io.BytesIO(body.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": disposition},
    )


@router.post(
    "/config-import",
    summary="Merge a branch configuration bundle into this installation",
)
async def import_branch_config(
    file: UploadFile = File(...),
    dry_run: bool = Query(
        default=True, description="Report what would change without keeping it"
    ),
    target_branch_id: Optional[int] = Query(
        default=None, description="Branch the bundle updates; omit to match by the file"
    ),
    create_new: bool = Query(
        default=False, description="Always create a new branch instead of matching"
    ),
    lumg_map: Optional[str] = Form(
        default=None,
        description='JSON {"ЛУМГ у файлі": <id наявного ЛУМГ>} — renames, not duplicates',
    ),
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Merge a bundle, or preview the merge.

    The preview is the real import rolled back — that is what makes its counts
    exact and lets a constraint the file would violate surface before anyone
    presses «Застосувати» rather than after.

    `target_branch_id` / `create_new` / `lumg_map` carry the one decision the
    file cannot make for itself: which rows here this configuration IS. Without
    them the bundle matches itself by transfer id and then by name, which is
    right for a repeat transfer and wrong the first time it arrives under a name
    that has changed on one of the two sides.
    """
    content = await file.read()
    try:
        bundle = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Файл не читається як JSON: {exc}",
        )

    try:
        mapping = json.loads(lumg_map) if lumg_map else None
        if mapping is not None and not isinstance(mapping, dict):
            raise ValueError("expected an object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Некоректне зіставлення ЛУМГ: {exc}",
        )

    try:
        report = await branch_config_transfer.import_branch(
            session,
            bundle,
            dry_run=dry_run,
            target_branch_id=target_branch_id,
            create_new=create_new,
            lumg_map=mapping,
        )
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.error("Branch config import failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"База відхилила імпорт: {exc.__class__.__name__}",
        )

    applied = report.ok and not dry_run
    if applied:
        await session.commit()
    else:
        await session.rollback()
    return {**asdict(report), "applied": applied}


grmu_branch_router = router
