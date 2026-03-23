from typing import TYPE_CHECKING

from sqlalchemy import Index
from sqlmodel import Field, Relationship, UniqueConstraint

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .lumg_model import Lumg
    from .gas_volume_calc_type_model import GasVolumeCalcType
    from .line_model import Line


class GasVolumeCalcBase(HlBaseModel):
    address: int
    name: str = Field(max_length=255)
    c_time: int


GAS_VOLUME_CALC_CONSTRAINT = ["lumg_id", "address"]


class GasVolumeCalc(GasVolumeCalcBase, table=True):
    __tablename__ = "gas_volume_calc"
    __table_args__ = (
        UniqueConstraint(*GAS_VOLUME_CALC_CONSTRAINT, name="lumg_adress_constraint"),
        # Per-lumg name uniqueness (replaces global unique on name)
        UniqueConstraint("lumg_id", "name", name="uq_gvc_lumg_name"),
        # Indexes
        Index("idx_gvc_lumg", "lumg_id"),
        Index("idx_gvc_lumg_address", "lumg_id", "address"),
        Index("idx_gvc_address", "address"),
        Index("idx_gvc_type", "type_id"),
        Index("idx_gvc_name", "name"),
    )

    id: int | None = Field(default=None, primary_key=True)
    lumg_id: int | None = Field(default=None, foreign_key="lumg.id", ondelete="CASCADE")
    lumg: "Lumg" = Relationship(back_populates="gas_volume_calcs")
    type_id: int | None = Field(
        default=None, foreign_key="gas_vol_calc_type.id", ondelete="CASCADE"
    )
    type: "GasVolumeCalcType" = Relationship(back_populates="gas_volume_calcs")
    lines: list["Line"] = Relationship(
        back_populates="gas_volume_calc", cascade_delete=True
    )


class GasVolumeCalcList(GasVolumeCalcBase):
    id: int
    lumg_id: int
    type_id: int | None


class GasVolumeCalcCreate(GasVolumeCalcBase):
    lumg_id: int
    type_id: int | None = None


class GasVolumeCalcUpdate(GasVolumeCalcBase):
    name: str | None = None
    c_time: int | None = None
    address: int | None = None
    type_id: int | None = None
