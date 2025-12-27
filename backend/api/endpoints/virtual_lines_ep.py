"""
Virtual Lines API Endpoints

Endpoints for managing and querying virtual lines (rings).
"""

from fastapi import APIRouter, status, HTTPException
from typing import List

from backend.db.engine import async_session_factory
from backend.db.dao.line_dao import LineDao
from backend.db.models.virtual_line_models import (
    VisibleLineResponse,
    VirtualLinesValidationResponse
)
from backend.services.virtual_lines_config import (
    get_active_virtual_lines,
    get_physical_lines_in_rings,
    validate_config
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
            description="Returns list of lines visible in frontend: virtual lines + physical lines not in rings"
        )

        self.router.add_api_route(
            path="/virtual_lines/validate",
            tags=["virtual_lines"],
            endpoint=self.validate_configuration,
            methods=["GET"],
            response_model=VirtualLinesValidationResponse,
            status_code=status.HTTP_200_OK,
            summary="Validate virtual lines configuration",
            description="Validates virtual_lines.json configuration and returns validation results"
        )

    async def get_visible_lines(self) -> List[VisibleLineResponse]:
        """
        Get list of lines visible in frontend.

        Returns:
            List of VisibleLineResponse with:
            - All active virtual lines (id >= 1000)
            - All physical lines NOT in active virtual lines (id < 1000)

        Logic:
            1. Load active virtual lines from JSON config
            2. Get all physical lines from database
            3. Exclude physical lines that are in virtual lines
            4. Combine virtual + remaining physical lines
        """
        # Load active virtual lines
        virtual_lines = get_active_virtual_lines()

        # Get physical lines that are in rings
        physical_in_rings = get_physical_lines_in_rings()

        # Get all physical lines from database
        async with async_session_factory() as session:
            all_physical_lines = await LineDao(session=session).get_all()

        # Build response
        result = []

        # Add virtual lines
        for vline_id_str, vline_data in virtual_lines.items():
            result.append(VisibleLineResponse(
                id=int(vline_id_str),
                name=vline_data["name"],
                is_virtual=True,
                physical_line_ids=vline_data["physical_line_ids"],
                description=vline_data.get("description")
            ))

        # Add physical lines NOT in rings
        for pline in all_physical_lines:
            if pline.id not in physical_in_rings:
                result.append(VisibleLineResponse(
                    id=pline.id,
                    name=pline.name,
                    is_virtual=False,
                    physical_line_ids=None,
                    description=None
                ))

        # Sort by ID
        result.sort(key=lambda x: x.id)

        return result

    async def validate_configuration(self) -> VirtualLinesValidationResponse:
        """
        Validate virtual lines configuration.

        Returns:
            VirtualLinesValidationResponse with validation results
        """
        try:
            validation_result = validate_config()
            return VirtualLinesValidationResponse(**validation_result)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Error validating virtual lines configuration: {str(e)}"
            )


virtual_lines_router = VirtualLinesRouter().router
