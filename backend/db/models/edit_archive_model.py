from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .edit_type_model import EditType
    from .line_model import Line


class EditArchiveBase(SQLModel):
    period: datetime = Field(index=True)
    old_value: int
    new_value: int


EDIT_ARCHIVE_CONSTRAINT = [
    "period",
    "edit_id",
    "gas_vol_calc_id",
    "new_value",
    "old_value",
]


class EditArchive(EditArchiveBase, table=True):
    __tablename__ = "edit_archive"
    __table_args__ = (
        UniqueConstraint(*EDIT_ARCHIVE_CONSTRAINT, name="edit_all_constraint"),
    )
    id: int | None = Field(default=None, primary_key=True)
    edit_id: int | None = Field(
        default=None, foreign_key="edit_type.id", ondelete="CASCADE"
    )
    edit: "EditType" = Relationship(back_populates="edit_archives")
    line_id: int | None = Field(default=None, foreign_key="line.id", ondelete="CASCADE")
    gas_volume_calc: "Line" = Relationship(back_populates="edit_archives")


class EditArchiveList(EditArchiveBase):
    id: int
    edit_id: int
    gas_vol_calc_id: int


class EditArchiveCreate(EditArchiveBase):
    edit_id: int
    gas_vol_calc_id: int
