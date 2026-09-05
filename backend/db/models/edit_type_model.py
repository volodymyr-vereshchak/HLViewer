from sqlmodel import Field, Relationship, UniqueConstraint
from typing import TYPE_CHECKING
from sqlalchemy import BigInteger

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_type_model import GasVolumeCalcType


class EditTypeBase(HlBaseModel):
    edit_type_id: int = Field(sa_type=BigInteger)
    edit_name: str = Field(max_length=255)
    gas_volume_calc_type_id: int = Field(sa_type=BigInteger)


EDIT_TYPE_CONSTRAINT = ["edit_type_id", "gas_volume_calc_type_id"]


class EditType(EditTypeBase, table=True):
    __tablename__ = "edit_type"
    __table_args__ = (
        UniqueConstraint(*EDIT_TYPE_CONSTRAINT, name="edit_type_id_constraint"),
    )
    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)


class EditTypeList(EditTypeBase):
    id: int


class EditTypeCreate(EditTypeBase):
    pass


class EditTypeUpdate(EditTypeBase):
    edit_name: str | None = None
