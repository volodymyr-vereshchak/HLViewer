from typing import TYPE_CHECKING
from sqlmodel import Field, UniqueConstraint, Relationship

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_model import GasVolumeCalc
    from .daily_archive_model import DailyArchive
    from .hourly_archive_model import HourlyArchive
    from .edit_archive_model import EditArchive
    from .sys_archive_model import SysArchive
    from .params_model import Param


class LineBase(HlBaseModel):
    line: int
    meter: bool
    name: str = Field(max_length=255)


LINE_CONSTRAINT = ["gas_volume_calc_id", "line"]


class Line(LineBase, table=True):
    __tablename__ = "gas_volume_line"
    __table_args__ = (
        UniqueConstraint(*LINE_CONSTRAINT, name="line_gas_volume_line_constraint"),
    )

    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calc_id: int | None = Field(
        default=None, foreign_key="gas_volume_calc.id", ondelete="CASCADE"
    )
    gas_volume_calc: "GasVolumeCalc" = Relationship(back_populates="lines")
    daily_archives: list["DailyArchive"] = Relationship(
        back_populates="line", cascade_delete=True
    )
    hourly_archives: list["HourlyArchive"] = Relationship(
        back_populates="line", cascade_delete=True
    )
    edit_archives: list["EditArchive"] = Relationship(
        back_populates="line", cascade_delete=True
    )
    sys_archives: list["SysArchive"] = Relationship(
        back_populates="line", cascade_delete=True
    )
    params: list["Param"] = Relationship(back_populates="line", cascade_delete=True)


class LineList(LineBase):
    id: int
    gas_volume_calc_id: int


class LineCreate(LineBase):
    gas_volume_calc_id: int


class LineUpdate(LineBase):
    meter: bool | None
    name: str | None
