from sqlmodel import Field, Relationship, UniqueConstraint
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import Index

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .line_model import Line


class SysArchiveBase(HlBaseModel):
    period: datetime = Field(index=True)
    sys_type_id: int
    volume: float


SYS_ARCHIVE_CONSTRAINT = ["period", "sys_type_id", "line_id", "volume"]


class SysArchive(SysArchiveBase, table=True):
    __tablename__ = "sys_archive"
    __table_args__ = (
        UniqueConstraint(*SYS_ARCHIVE_CONSTRAINT, name="sys_all_constraint"),
        # Оптимизированные индексы для запросов по диапазону дат и line_id
        Index("idx_sys_line_period", "line_id", "period"),
        Index("idx_sys_period_line", "period", "line_id"),
        # Индекс для поиска по sys_type_id
        Index("idx_sys_type_period", "sys_type_id", "period"),
        Index("idx_sys_line_type", "line_id", "sys_type_id"),
        # Индекс для агрегации по часам
        Index("idx_sys_period_hour", "period"),
    )
    id: int | None = Field(default=None, primary_key=True)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE", index=True
    )
    line: "Line" = Relationship(back_populates="sys_archives")


class SysArchiveList(SysArchiveBase):
    id: int
    line_id: int


class SysArchiveCreate(SysArchiveBase):
    line_id: int


class SysArchiveEndpointList(SysArchiveList):
    sys_name: str | None = None
