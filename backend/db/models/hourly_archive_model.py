from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint
from datetime import datetime
from decimal import Decimal
from pydantic import field_validator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .line_model import Line


class HourlyArchiveBase(SQLModel):
    period: datetime = Field(index=True)
    volume: Decimal = Field(decimal_places=3)
    w_volume_dp: Decimal = Field(decimal_places=3)
    pressure: Decimal = Field(decimal_places=3)
    temperature: Decimal = Field(decimal_places=3)
    density: Decimal = Field(decimal_places=3)


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
            value = Decimal(0)
        return value


class HourlyArchiveCreate(HourlyArchiveBase):
    line_id: int
