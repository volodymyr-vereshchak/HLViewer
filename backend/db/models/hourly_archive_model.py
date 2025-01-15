from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint
from datetime import datetime
from decimal import Decimal
from pydantic import field_validator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gas_volume_calc_model import GasVolumeCalc


class HourlyArchiveBase(SQLModel):
    line: int
    period: datetime = Field(index=True)
    volume: Decimal = Field(decimal_places=3)
    w_volume_dp: Decimal = Field(decimal_places=3)
    pressure: Decimal = Field(decimal_places=3)
    temperature: Decimal = Field(decimal_places=3)
    density: Decimal = Field(decimal_places=3)


HOURLY_ARCHIVE_CONSTRAINT = ["gas_vol_calc_id", "line", "period", "volume"]


class HourlyArchive(HourlyArchiveBase, table=True):
    __tablename__ = "hourly_archive"
    __table_args__ = (
        UniqueConstraint(
            *HOURLY_ARCHIVE_CONSTRAINT, name="hour_calc_id_line_period_constraint"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    gas_vol_calc_id: int | None = Field(
        default=None, foreign_key="gas_volume_calc.id", ondelete="CASCADE"
    )
    gas_volume_calc: "GasVolumeCalc" = Relationship(back_populates="hourly_archives")


class HourlyArchiveList(HourlyArchiveBase):
    id: int
    gas_vol_calc_id: int

    @field_validator("density")
    def validate_density(cls, value: Decimal):
        if value > 1 or value < 0.5:
            value = Decimal(0)
        return value


class HourlyArchiveCreate(HourlyArchiveBase):
    gas_vol_calc_id: int
