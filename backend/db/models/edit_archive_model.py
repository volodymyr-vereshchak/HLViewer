from sqlmodel import Field, Relationship, UniqueConstraint
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import Index, BigInteger

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .line_model import Line


class EditArchiveBase(HlBaseModel):
    period: datetime = Field(index=True)
    old_value: int
    new_value: int
    edit_type_id: int = Field(sa_type=BigInteger)


EDIT_ARCHIVE_CONSTRAINT = [
    "period",
    "edit_type_id",
    "line_id",
    "new_value",
    "old_value",
]


class EditArchive(EditArchiveBase, table=True):
    __tablename__ = "edit_archive"
    __table_args__ = (
        UniqueConstraint(*EDIT_ARCHIVE_CONSTRAINT, name="edit_all_constraint"),
        # Оптимизированные индексы для запросов по диапазону дат и line_id
        Index("idx_edit_line_period", "line_id", "period"),
        Index("idx_edit_period_line", "period", "line_id"),
        # Индекс для поиска по edit_type_id
        Index("idx_edit_type_period", "edit_type_id", "period"),
        Index("idx_edit_line_type", "line_id", "edit_type_id"),
        # «Звірка ФХП» reads one line's history of ONE quantity over a range,
        # and probes backwards for the value in force before it. Without the
        # period in the key both queries read every edit type of the line.
        Index("idx_edit_line_type_period", "line_id", "edit_type_id", "period"),
        # Индекс для агрегации по часам
        Index("idx_edit_period_hour", "period"),
    )
    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE",
        index=True, sa_type=BigInteger,
    )
    line: "Line" = Relationship(back_populates="edit_archives")


class EditArchiveList(EditArchiveBase):
    id: int
    line_id: int


class EditArchiveCreate(EditArchiveBase):
    line_id: int


class EditArchiveEndpointList(EditArchiveList):
    edit_name: str | None = None
    # Computer (vychislitel) type — lets the frontend scope value decoding to a
    # verified device type instead of guessing the byte layout globally.
    gas_volume_calc_type_id: int | None = None
