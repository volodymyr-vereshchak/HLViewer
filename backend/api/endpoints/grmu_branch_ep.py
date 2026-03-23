"""
GrmuBranch (Філіал ГРМУ) API Endpoints

Provides CRUD for branches and read access to per-branch device mappings.
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text
from sqlmodel import select

from backend.db.engine import async_session_factory
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
    DpdGlobalConfig,
    DpdGlobalConfigUpdate,
    GrmuBranchDpdCredential,
    GrmuBranchDpdCredentialUpdate,
)
from backend.db.models.lumg_model import Lumg
from backend.hl_engine.config_reader import ConfigReader
from backend.services.grmu_branch_mappings import get_all_mappings

_update_lock = asyncio.Lock()

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


@router.delete(
    "/{branch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a branch",
)
async def delete_branch(branch_id: int):
    async with async_session_factory() as session:
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


# ─── Branch Data Path ─────────────────────────────────────────────────────────


@router.get(
    "/{branch_id}/data-path",
    response_model=BranchDataPathRead,
    summary="Get data path for a branch",
)
async def get_branch_data_path(branch_id: int):
    async with async_session_factory() as session:
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
async def upsert_branch_data_path(branch_id: int, body: BranchDataPathUpsert):
    async with async_session_factory() as session:
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
async def delete_branch_data_path(branch_id: int):
    async with async_session_factory() as session:
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
    "/{branch_id}/config-debug",
    summary="Raw debug dump of CFG file structure",
)
async def debug_branch_config(branch_id: int):
    import struct
    from backend.hl_engine.data_classes.cfg_dataclass import HeaderStruct, GisStruct, FlowStruct, LineStruct, GasVolumeCalcStruct

    async with async_session_factory() as session:
        result = await session.execute(
            select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
        )
        dp = result.scalars().first()
    if not dp:
        raise HTTPException(status_code=404, detail="No config path")

    import os
    file_size = os.path.getsize(dp.path)
    output = [f"File size: {file_size} bytes"]
    output.append(f"Struct sizes: Header={HeaderStruct.size} GIS={GisStruct.size} Flow={FlowStruct.size} Line={LineStruct.size} GVC={GasVolumeCalcStruct.size}")

    with open(dp.path, "rb") as f:
        raw = f.read()

    hdr = HeaderStruct.unpack(raw[:HeaderStruct.size])
    output.append(f"Header: '{HeaderStruct.get_string_from_bytes(hdr.header)}', gis_num={hdr.gis_num}, zero_byte={hdr.zero_byte}")
    output.append(f"Header unk hex: {hdr.unk.hex()}")

    # --- Phase 1: scan entire file for \x04\x00 occurrences ---
    marker = b'\x04\x00'
    marker_offsets = []
    pos = 0
    while True:
        idx = raw.find(marker, pos)
        if idx == -1:
            break
        marker_offsets.append(idx)
        pos = idx + 1

    output.append(f"\n--- All \\x04\\x00 occurrences ({len(marker_offsets)} total) ---")
    for off in marker_offsets[:60]:  # limit to first 60
        if off + GisStruct.size <= len(raw):
            chunk = raw[off:off + GisStruct.size]
            g = GisStruct.unpack(chunk)
            name = GisStruct.get_string_from_bytes(g.gis_name)[:g.gis_name_length]
            output.append(f"  offset={off} name_len={g.gis_name_length} name='{name}' flow_num={g.flow_num} z0={g.zero_byte} z1={g.zero_byte1}")

    # --- Phase 2: walk from after header, skip GIS 0 correctly, find gap ---
    output.append(f"\n--- Phase 2: walk GIS 0 and find gap ---")
    cursor = HeaderStruct.size

    def walk_gis(data, start):
        """Walk one GIS entry; return (end_offset, gis_name, flow_count, ok)."""
        pos = start
        if pos + GisStruct.size > len(data):
            return pos, "", 0, False
        g = GisStruct.unpack(data[pos:pos + GisStruct.size])
        gname = GisStruct.get_string_from_bytes(g.gis_name)[:g.gis_name_length]
        pos += GisStruct.size
        for _ in range(g.flow_num):
            if pos + FlowStruct.size > len(data):
                return pos, gname, g.flow_num, False
            fl = FlowStruct.unpack(data[pos:pos + FlowStruct.size])
            pos += FlowStruct.size
            for _ in range(fl.line_num):
                if pos + LineStruct.size > len(data):
                    return pos, gname, g.flow_num, False
                pos += LineStruct.size
            if pos + GasVolumeCalcStruct.size > len(data):
                return pos, gname, g.flow_num, False
            pos += GasVolumeCalcStruct.size
        return pos, gname, g.flow_num, True

    end0, gname0, flows0, ok0 = walk_gis(raw, cursor)
    output.append(f"GIS 0: name='{gname0}' flows={flows0} ok={ok0} ends_at={end0}")

    # Show 64 bytes after GIS 0 end
    gap_bytes = raw[end0:end0 + 64]
    output.append(f"Bytes after GIS 0 ({end0}..{end0+64}): {gap_bytes.hex()}")
    output.append(f"  as text: {gap_bytes.decode('windows-1251', errors='replace')!r}")

    # Find next \x04\x00 after end of GIS 0
    next_marker = raw.find(marker, end0)
    if next_marker != -1:
        gap_size = next_marker - end0
        output.append(f"Next \\x04\\x00 after GIS 0: offset={next_marker} (gap={gap_size} bytes)")
        output.append(f"  gap hex: {raw[end0:next_marker].hex()}")
        output.append(f"  gap text: {raw[end0:next_marker].decode('windows-1251', errors='replace')!r}")

        # Try to parse GIS 1 from there
        end1, gname1, flows1, ok1 = walk_gis(raw, next_marker)
        output.append(f"GIS 1 at {next_marker}: name='{gname1}' flows={flows1} ok={ok1} ends_at={end1}")

        # Find next \x04\x00 after GIS 1
        next_marker2 = raw.find(marker, end1)
        if next_marker2 != -1:
            gap2 = next_marker2 - end1
            output.append(f"Next \\x04\\x00 after GIS 1: offset={next_marker2} (gap={gap2} bytes)")
            end2, gname2, flows2, ok2 = walk_gis(raw, next_marker2)
            output.append(f"GIS 2 at {next_marker2}: name='{gname2}' flows={flows2} ok={ok2} ends_at={end2}")

    return {"debug": "\n".join(output)}


@router.get(
    "/{branch_id}/config-preview",
    summary="Read CFG file and return list of GIS (LUMGs) with flows",
)
async def preview_branch_config(branch_id: int):
    async with async_session_factory() as session:
        result = await session.execute(
            select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
        )
        dp = result.scalars().first()
    if not dp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config path not set for this branch")
    try:
        gis_list = ConfigReader(file=dp.path).read()
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
async def get_config_mappings(branch_id: int):
    async with async_session_factory() as session:
        result = await session.execute(
            select(BranchConfigMapping).where(BranchConfigMapping.branch_id == branch_id)
        )
        return result.scalars().all()


@router.put(
    "/{branch_id}/config-mappings",
    response_model=List[BranchConfigMappingRead],
    summary="Save GIS→LUMG mappings for a branch (replaces existing)",
)
async def upsert_config_mappings(branch_id: int, mappings: List[BranchConfigMappingUpsert]):
    async with async_session_factory() as session:
        # Delete existing mappings for this branch
        existing = await session.execute(
            select(BranchConfigMapping).where(BranchConfigMapping.branch_id == branch_id)
        )
        for row in existing.scalars().all():
            await session.delete(row)

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
async def update_branch_names(branch_id: int):
    if _update_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Update is already in progress. Please try again later.",
        )
    async with _update_lock:
        async with async_session_factory() as session:
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
        async with async_session_factory() as session:
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
            await ConfigReader(file=dp.path).update_db_with_mapping(mapping_dict)
            return {"message": f"Names updated for branch {branch_id}", "last_updated": datetime.now().isoformat()}
        except FileNotFoundError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error(f"Error updating names for branch {branch_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ─── DPD Global Config ────────────────────────────────────────────────────────


@router.get(
    "/dpd-config",
    summary="Get global DPD API config (api_base_url, auth_url, timeout_sec)",
)
async def get_dpd_global_config():
    async with async_session_factory() as session:
        result = await session.execute(select(DpdGlobalConfig))
        cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DPD global config not set")
    return {"id": cfg.id, "api_base_url": cfg.api_base_url, "auth_url": cfg.auth_url, "timeout_sec": cfg.timeout_sec}


@router.put(
    "/dpd-config",
    summary="Create or update global DPD API config",
)
async def upsert_dpd_global_config(body: DpdGlobalConfigUpdate):
    async with async_session_factory() as session:
        result = await session.execute(select(DpdGlobalConfig))
        cfg = result.scalars().first()
        update_data = body.model_dump(exclude_unset=True)
        if cfg:
            for key, value in update_data.items():
                setattr(cfg, key, value)
        else:
            required = {"api_base_url", "auth_url"}
            missing = required - update_data.keys()
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Missing required fields: {', '.join(sorted(missing))}",
                )
            cfg = DpdGlobalConfig(**update_data)
            session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
    return {"id": cfg.id, "api_base_url": cfg.api_base_url, "auth_url": cfg.auth_url, "timeout_sec": cfg.timeout_sec}


# ─── DPD Credentials (per branch) ────────────────────────────────────────────


@router.get(
    "/{branch_id}/dpd-credential",
    summary="Get DPD credentials for a branch (username only, no password)",
)
async def get_dpd_credential(branch_id: int):
    async with async_session_factory() as session:
        result = await session.execute(
            select(GrmuBranchDpdCredential).where(GrmuBranchDpdCredential.branch_id == branch_id)
        )
        cred = result.scalars().first()
    if not cred:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DPD credentials not found")
    return {"id": cred.id, "branch_id": cred.branch_id, "username": cred.username}


@router.put(
    "/{branch_id}/dpd-credential",
    summary="Create or update DPD credentials for a branch",
)
async def upsert_dpd_credential(branch_id: int, body: GrmuBranchDpdCredentialUpdate):
    async with async_session_factory() as session:
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
    return {"id": cred.id, "branch_id": cred.branch_id, "username": cred.username}


@router.delete(
    "/{branch_id}/dpd-credential",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete DPD credentials for a branch",
)
async def delete_dpd_credential(branch_id: int):
    async with async_session_factory() as session:
        result = await session.execute(
            select(GrmuBranchDpdCredential).where(GrmuBranchDpdCredential.branch_id == branch_id)
        )
        cred = result.scalars().first()
        if not cred:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DPD credentials not found")
        await session.delete(cred)
        await session.commit()


grmu_branch_router = router
