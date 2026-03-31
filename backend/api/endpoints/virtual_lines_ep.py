"""
Virtual Lines API Endpoints

Endpoints for managing and querying virtual lines (rings).
Uses DB-backed virtual line data; falls back to JSON config when DB is empty.
"""

from fastapi import APIRouter, Depends, status, HTTPException
from typing import List
from sqlmodel import select
from sqlalchemy import delete as sa_delete

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.engine import async_session_factory
from backend.db.dao.line_dao import LineDao
from backend.db.models.grmu_branch_model import (
    VirtualLine,
    VirtualLineMember,
    VirtualLineList,
    VirtualLineCreate,
)
from backend.db.models.virtual_line_models import (
    VisibleLineResponse,
    VirtualLinesValidationResponse,
)
from backend.services.virtual_lines_config import (
    # File-based fallbacks (keep for backward compatibility)
    get_active_virtual_lines,
    get_physical_lines_in_rings,
    validate_config,
    # DB-backed async versions
    get_active_virtual_lines_db,
    get_physical_lines_in_rings_db,
    validate_config_db,
)


class VirtualLinesRouter:
    """Router for virtual lines endpoints."""

    def __init__(self):
        self.router = APIRouter(dependencies=[Depends(get_current_user)])

        self.router.add_api_route(
            path="/virtual_lines/visible",
            tags=["virtual_lines"],
            endpoint=self.get_visible_lines,
            methods=["GET"],
            response_model=List[VisibleLineResponse],
            status_code=status.HTTP_200_OK,
            summary="Get visible lines",
            description=(
                "Returns list of lines visible in frontend: virtual lines + "
                "physical lines not in rings. Prefers DB-backed virtual lines; "
                "falls back to JSON config when the DB has no virtual lines."
            ),
        )

        self.router.add_api_route(
            path="/virtual_lines/validate",
            tags=["virtual_lines"],
            endpoint=self.validate_configuration,
            methods=["GET"],
            response_model=VirtualLinesValidationResponse,
            status_code=status.HTTP_200_OK,
            summary="Validate virtual lines configuration",
            description="Validates virtual lines configuration and returns validation results.",
        )

        self.router.add_api_route(
            path="/virtual_lines/",
            tags=["virtual_lines"],
            endpoint=self.get_all,
            methods=["GET"],
            response_model=List[VirtualLineList],
            status_code=status.HTTP_200_OK,
            summary="List all virtual lines",
        )

        self.router.add_api_route(
            path="/virtual_lines/",
            tags=["virtual_lines"],
            endpoint=self.create,
            methods=["POST"],
            response_model=VirtualLineList,
            status_code=status.HTTP_201_CREATED,
            summary="Create virtual line",
        )

        self.router.add_api_route(
            path="/virtual_lines/{vl_id}",
            tags=["virtual_lines"],
            endpoint=self.update,
            methods=["PATCH"],
            response_model=VirtualLineList,
            status_code=status.HTTP_200_OK,
            summary="Update virtual line",
        )

        self.router.add_api_route(
            path="/virtual_lines/{vl_id}",
            tags=["virtual_lines"],
            endpoint=self.delete_vl,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Delete virtual line",
        )

    async def get_visible_lines(self) -> List[VisibleLineResponse]:
        """
        Get list of lines visible in frontend.

        Tries DB virtual lines first; falls back to JSON file if none exist.
        """
        async with async_session_factory() as session:
            # Try DB-backed virtual lines
            db_virtual_lines = await get_active_virtual_lines_db(session)

            if db_virtual_lines:
                physical_in_rings = await get_physical_lines_in_rings_db(session)
            else:
                # Fallback: JSON file
                db_virtual_lines = get_active_virtual_lines()
                physical_in_rings = get_physical_lines_in_rings()

            all_physical_lines = await LineDao(session=session).get_all()

        result = []

        # Add virtual lines
        for vline_id_str, vline_data in db_virtual_lines.items():
            result.append(
                VisibleLineResponse(
                    id=int(vline_id_str),
                    name=vline_data["name"],
                    is_virtual=True,
                    physical_line_ids=vline_data["physical_line_ids"],
                    description=vline_data.get("description"),
                    lumg_id=vline_data.get("lumg_id"),
                    branch_id=vline_data.get("branch_id"),
                )
            )

        # Add physical lines NOT in rings
        for pline in all_physical_lines:
            if pline.id not in physical_in_rings:
                result.append(
                    VisibleLineResponse(
                        id=pline.id,
                        name=pline.name,
                        is_virtual=False,
                        physical_line_ids=None,
                        description=None,
                        lumg_id=None,
                        branch_id=None,
                    )
                )

        result.sort(key=lambda x: x.id)
        return result

    async def validate_configuration(self) -> VirtualLinesValidationResponse:
        """Validate virtual lines configuration (DB-backed with JSON fallback)."""
        try:
            async with async_session_factory() as session:
                db_virtual_lines = await get_active_virtual_lines_db(session)

            if db_virtual_lines:
                async with async_session_factory() as session:
                    validation_result = await validate_config_db(session)
            else:
                validation_result = validate_config()

            return VirtualLinesValidationResponse(**validation_result)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Error validating virtual lines configuration: {str(e)}",
            )

    async def get_all(
        self,
        include_in_trends: bool = None,
        branch_id: int = None,
    ) -> List[VirtualLineList]:
        """List all virtual lines with their physical line members."""
        async with async_session_factory() as session:
            stmt = select(VirtualLine)
            if include_in_trends is not None:
                stmt = stmt.where(VirtualLine.include_in_trends == include_in_trends)
            if branch_id is not None:
                stmt = stmt.where(VirtualLine.branch_id == branch_id)
            result = await session.execute(stmt)
            vlines = result.scalars().all()
            out = []
            for vl in vlines:
                members = await session.execute(
                    select(VirtualLineMember).where(
                        VirtualLineMember.virtual_line_id == vl.id
                    ).order_by(VirtualLineMember.sort_order)
                )
                physical_ids = [m.line_id for m in members.scalars().all()]
                item = VirtualLineList(
                    **vl.model_dump(exclude={"members", "branch", "lumg"}),
                    physical_line_ids=physical_ids,
                )
                out.append(item)
            return out

    async def create(self, data: VirtualLineCreate) -> VirtualLineList:
        """Create a new virtual line with physical line members."""
        async with async_session_factory() as session:
            vl = VirtualLine(**data.model_dump(exclude={"physical_line_ids"}))
            session.add(vl)
            await session.flush()
            for lid in data.physical_line_ids:
                session.add(VirtualLineMember(virtual_line_id=vl.id, line_id=lid))
            await session.commit()
            await session.refresh(vl)
            return VirtualLineList(
                **vl.model_dump(exclude={"members", "branch", "lumg"}),
                physical_line_ids=data.physical_line_ids,
            )

    async def update(self, vl_id: int, data: VirtualLineCreate) -> VirtualLineList:
        """Update a virtual line and replace its physical line members."""
        async with async_session_factory() as session:
            vl = await session.get(VirtualLine, vl_id)
            if not vl:
                raise HTTPException(status_code=404, detail="Virtual line not found")
            for k, v in data.model_dump(exclude={"physical_line_ids"}).items():
                setattr(vl, k, v)
            await session.execute(
                sa_delete(VirtualLineMember).where(
                    VirtualLineMember.virtual_line_id == vl_id
                )
            )
            for lid in data.physical_line_ids:
                session.add(VirtualLineMember(virtual_line_id=vl_id, line_id=lid))
            await session.commit()
            await session.refresh(vl)
            return VirtualLineList(
                **vl.model_dump(exclude={"members", "branch", "lumg"}),
                physical_line_ids=data.physical_line_ids,
            )

    async def delete_vl(self, vl_id: int):
        """Delete a virtual line and its members."""
        async with async_session_factory() as session:
            vl = await session.get(VirtualLine, vl_id)
            if not vl:
                raise HTTPException(status_code=404, detail="Virtual line not found")
            await session.delete(vl)
            await session.commit()


virtual_lines_router = VirtualLinesRouter().router
