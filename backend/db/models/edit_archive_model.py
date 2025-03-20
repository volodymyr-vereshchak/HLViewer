from sqlmodel import Field, Relationship, UniqueConstraint
from datetime import datetime
from typing import TYPE_CHECKING

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .line_model import Line


class EditArchiveBase(HlBaseModel):
    period: datetime = Field(index=True)
    old_value: int
    new_value: int
    edit_type_id: int


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
    )
    id: int | None = Field(default=None, primary_key=True)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE", index=True
    )
    line: "Line" = Relationship(back_populates="edit_archives")


class EditArchiveList(EditArchiveBase):
    id: int
    line_id: int


class EditArchiveCreate(EditArchiveBase):
    line_id: int


class EditArchiveEndpointList(EditArchiveList):
    edit_name: str | None = None
