from sqlmodel import Field, Relationship
from typing import TYPE_CHECKING
from sqlalchemy import Index

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_model import GasVolumeCalc
    from .edit_type_model import EditType
    from .sys_type_model import SysType


class GasVolumeCalcTypeBase(HlBaseModel):
    type_id: int
    type_name: str = Field(max_length=255, unique=True)


GAS_VOLUME_CALC_TYPE_CONSTRAINT = ["type_name"]


class GasVolumeCalcType(GasVolumeCalcTypeBase, table=True):
    __tablename__ = "gas_vol_calc_type"
    __table_args__ = (
        # Индекс для поиска по type_id
        Index("idx_gvct_type_id", "type_id"),
        # Индекс для поиска по type_name
        Index("idx_gvct_type_name", "type_name"),
    )

    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(
        back_populates="type", cascade_delete=True
    )


class GasVolumeCalcTypeList(GasVolumeCalcTypeBase):
    id: int


class GasVolumeCalcTypeCreate(GasVolumeCalcTypeBase):
    pass


class GasVolumeCalcTypeUpdate(GasVolumeCalcTypeBase):
    type_id: int | None = None
    type_name: str | None = None
