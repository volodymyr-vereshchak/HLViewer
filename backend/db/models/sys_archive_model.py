from decimal import Decimal

from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sys_type_model import SysType
    from .gas_volume_calc_model import GasVolumeCalc


class SysArchiveBase(SQLModel):
    period: datetime = Field(index=True)
    line: int
    standard_volume: Decimal = Field(max_digits=20, decimal_places=3)


SYS_ARCHIVE_CONSTRAINT = ["period", "sys_type_id", "gas_vol_calc_id"]


class SysArchive(SysArchiveBase, table=True):
    __tablename__ = "sys_archive"
    __table_args__ = (
        UniqueConstraint(*SYS_ARCHIVE_CONSTRAINT, name="line_period_constraint"),
    )
    id: int | None = Field(default=None, primary_key=True)
    sys_type_id: int = Field(foreign_key="sys_type.id", ondelete="CASCADE")
    sys_type: "SysType" = Relationship(back_populates="sys_archives")
    gas_vol_calc_id: int | None = Field(
        default=None, foreign_key="gas_volume_calc.id", ondelete="CASCADE"
    )
    gas_volume_calc: "GasVolumeCalc" = Relationship(back_populates="sys_archives")


class SysArchiveList(SysArchiveBase):
    id: int
    sys_type_id: int
    gas_vol_calc_id: int


class SysArchiveCreate(SysArchiveBase):
    sys_type_id: int
    gas_vol_calc_id: int
