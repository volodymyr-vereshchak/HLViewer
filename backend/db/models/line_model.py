from typing import TYPE_CHECKING
from sqlmodel import Field, UniqueConstraint, Relationship
from sqlalchemy import Index

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
    include_in_report: bool = Field(default=False)
    include_in_trends: bool = Field(default=False)
    is_high_pressure: bool = Field(default=False)


LINE_CONSTRAINT = ["gas_volume_calc_id", "line"]


class Line(LineBase, table=True):
    __tablename__ = "gas_volume_line"
    __table_args__ = (
        UniqueConstraint(*LINE_CONSTRAINT, name="line_gas_volume_line_constraint"),
        # Оптимизированные индексы для поиска по gas_volume_calc_id
        Index("idx_line_gas_volume_calc", "gas_volume_calc_id"),
        Index("idx_line_gas_volume_line", "gas_volume_calc_id", "line"),
        # Индекс для поиска по номеру линии
        Index("idx_line_number", "line"),
        # Индекс для поиска по имени
        Index("idx_line_name", "name"),
        # Индекс для поиска по meter
        Index("idx_line_meter", "meter"),
        Index("idx_line_include_in_report", "include_in_report"),
        Index("idx_line_include_in_trends", "include_in_trends"),
        Index("idx_line_is_high_pressure", "is_high_pressure"),
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
    line: int | None = None
    meter: bool | None = None
    name: str | None = None
    include_in_report: bool | None = None
    include_in_trends: bool | None = None
    is_high_pressure: bool | None = None
