"""
Virtual Lines API Endpoints

Endpoints for managing and querying virtual lines (rings).
Uses DB-backed virtual line data; falls back to JSON config when DB is empty.
"""

from fastapi import APIRouter, status, HTTPException
from typing import List

from backend.db.engine import async_session_factory
from backend.db.dao.line_dao import LineDao
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
        self.router = APIRouter()

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


virtual_lines_router = VirtualLinesRouter().router
