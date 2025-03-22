from sqlmodel import Field, Relationship, UniqueConstraint
from datetime import datetime
from decimal import Decimal
from pydantic import field_validator
from typing import TYPE_CHECKING

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .line_model import Line


class HourlyArchiveBase(HlBaseModel):
    period: datetime = Field(index=True)
    volume: float
    w_volume_dp: float
    pressure: float
    temperature: float
    density: float


HOURLY_ARCHIVE_CONSTRAINT = ["line_id", "period", "volume"]


class HourlyArchive(HourlyArchiveBase, table=True):
    __tablename__ = "hourly_archive"
    __table_args__ = (
        UniqueConstraint(
            *HOURLY_ARCHIVE_CONSTRAINT, name="hour_calc_id_line_period_constraint"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE", index=True
    )
    line: "Line" = Relationship(back_populates="hourly_archives")


class HourlyArchiveList(HourlyArchiveBase):
    id: int
    line_id: int

    @field_validator("density")
    def validate_density(cls, value: Decimal):
        if value > 1 or value < 0.5:
            value = 0
        return value


class HourlyArchiveCreate(HourlyArchiveBase):
    line_id: int
