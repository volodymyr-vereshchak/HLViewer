from sqlmodel import Field, Relationship, UniqueConstraint
from typing import TYPE_CHECKING

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_type_model import GasVolumeCalcType
    from .edit_archive_model import EditArchive


class EditTypeBase(HlBaseModel):
    edit_type_id: int
    edit_name: str = Field(max_length=255)


EDIT_TYPE_CONSTRAINT = ["edit_type_id", "gas_volume_calc_type_id"]


class EditType(EditTypeBase, table=True):
    __tablename__ = "edit_type"
    __table_args__ = (
        UniqueConstraint(*EDIT_TYPE_CONSTRAINT, name="edit_type_id_constraint"),
    )
    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calc_type_id: int | None = Field(
        default=None, foreign_key="gas_vol_calc_type.id", ondelete="CASCADE"
    )
    gas_volume_calc_type: "GasVolumeCalcType" = Relationship(
        back_populates="edit_types"
    )

    edit_archives: list["EditArchive"] = Relationship(
        back_populates="edit", cascade_delete=True
    )


class EditTypeList(EditTypeBase):
    id: int
    gas_volume_calc_type_id: int


class EditTypeCreate(EditTypeBase):
    edit_type_id: int
    gas_volume_calc_type_id: int
    edit_name: str


class EditTypeUpdate(EditTypeBase):
    edit_name: str | None = None
