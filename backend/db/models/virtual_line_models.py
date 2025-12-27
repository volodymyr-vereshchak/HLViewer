"""
Virtual Lines Pydantic Models

Models for virtual lines (rings) configuration and API responses.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


class VirtualLineConfig(BaseModel):
    """Configuration of a virtual line (ring)."""
    id: int = Field(ge=1000, description="Virtual line ID (must be >= 1000)")
    name: str = Field(description="Display name of the virtual line")
    physical_line_ids: List[int] = Field(min_length=1, description="List of physical line IDs in this ring")
    is_virtual: bool = Field(default=True, description="Always True for virtual lines")
    active: bool = Field(default=True, description="Whether this virtual line is active")
    description: Optional[str] = Field(default=None, description="Optional description")


class VisibleLineResponse(BaseModel):
    """Line for display in frontend (virtual or physical)."""
    id: int = Field(description="Line ID (>= 1000 for virtual, < 1000 for physical)")
    name: str = Field(description="Display name")
    is_virtual: bool = Field(description="True if virtual line, False if physical")
    physical_line_ids: Optional[List[int]] = Field(
        default=None,
        description="List of physical line IDs (only for virtual lines)"
    )
    description: Optional[str] = Field(default=None, description="Optional description")


class VirtualLinesValidationResponse(BaseModel):
    """Response for virtual lines configuration validation."""
    valid: bool = Field(description="Whether configuration is valid")
    virtual_lines_count: int = Field(description="Total number of virtual lines in config")
    active_count: int = Field(description="Number of active virtual lines")
    hidden_physical_lines: List[int] = Field(
        default_factory=list,
        description="Physical line IDs that will be hidden (part of active rings)"
    )
    errors: List[str] = Field(default_factory=list, description="Validation errors")
    warnings: List[str] = Field(default_factory=list, description="Validation warnings")
