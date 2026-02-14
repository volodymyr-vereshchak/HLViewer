"""
Pydantic models for enterprise volume data.

These models define the structure of data returned by the enterprise volumes endpoint.
"""

from pydantic import BaseModel, Field, ConfigDict
from datetime import date, datetime
from typing import List, Optional, Union


class DeviceVolume(BaseModel):
    """Individual device volume data."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "serNum": 123456,
                "mfDev": 16,
                "typeDev": 1,
                "chNum": 1,
                "enterprise_name": "ТОВ Завод №1",
                "volume": 1234.56,
                "temperature": 15.5,
                "pressure": 3.2
            }
        }
    )

    serNum: int = Field(description="Device serial number")
    mfDev: int = Field(description="Manufacturer device code")
    typeDev: int = Field(description="Device type code")
    chNum: int = Field(description="Channel number")
    enterprise_name: str = Field(description="Enterprise name")
    volume: Optional[float] = Field(None, ge=0, description="Daily standard volume (dvstAlwrk), None if no data")
    temperature: Optional[float] = Field(None, description="Temperature reading")
    pressure: Optional[float] = Field(None, description="Pressure reading")


class EnterpriseVolumeResponse(BaseModel):
    """Aggregated enterprise volume data for a line and date."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "line_id": 1,
                "period": "2025-12-01",
                "total_volume": 2345.67,
                "device_count": 2,
                "devices": [
                    {
                        "serNum": 123456,
                        "mfDev": 16,
                        "typeDev": 1,
                        "chNum": 1,
                        "enterprise_name": "ТОВ Завод №1",
                        "volume": 1234.56
                    },
                    {
                        "serNum": 123457,
                        "mfDev": 16,
                        "typeDev": 1,
                        "chNum": 1,
                        "enterprise_name": "ТОВ Завод №2",
                        "volume": 1111.11
                    }
                ]
            }
        }
    )

    line_id: int = Field(description="Gas line ID")
    period: Union[date, datetime] = Field(description="Date (daily) or datetime (hourly) of the measurement")
    total_volume: float = Field(ge=0, description="Total volume for all devices on this line")
    device_count: int = Field(ge=0, description="Number of devices contributing to total")
    devices: List[DeviceVolume] = Field(description="Individual device volumes")


class EnterpriseVolumeError(BaseModel):
    """Error response for enterprise volume endpoint."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "DPD API unavailable",
                "details": "Connection timeout after 30 seconds"
            }
        }
    )

    error: str = Field(description="Error message")
    details: Optional[str] = Field(None, description="Additional error details")


class EnterpriseMapping(BaseModel):
    """Enterprise mapping data from Excel files."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "line_id": 1,
                "serNum": 123456,
                "mfDev": 16,
                "typeDev": 1,
                "chNum": 1,
                "enterprise_name": "ТОВ Завод №1",
                "active": True
            }
        }
    )

    line_id: Optional[int] = Field(None, description="Gas line ID (None if enterprise has no line mapping)")
    serNum: int = Field(description="Device serial number")
    mfDev: int = Field(description="Manufacturer device code")
    typeDev: int = Field(description="Device type code")
    chNum: int = Field(description="Channel number")
    enterprise_name: str = Field(description="Enterprise name")
    active: bool = Field(description="Whether the enterprise is active")
