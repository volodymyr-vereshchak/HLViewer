from sqlmodel import SQLModel, Field, Relationship
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gas_volume_calc_model import GasVolumeCalc

class GasVolumeCalcTypeBase(SQLModel):
    type_id: int
    type_name: str = Field(max_length=255, unique=True)

class GasVolumeCalcType(GasVolumeCalcTypeBase, table=True):
    __tablename__ = "gas_vol_calc_type"

    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(back_populates="type", cascade_delete=True)

class GasVolumeCalcTypeList(GasVolumeCalcTypeBase):
    id: int

class GasVolumeCalcTypeCreate(GasVolumeCalcTypeBase):
    pass
