from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
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
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    lumg_id: int | None = Field(
        default=None, foreign_key="lumg.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    lumg: "Lumg" = Relationship(back_populates="gas_volume_calcs")
    type_id: int | None = Field(
        default=None, foreign_key="gas_vol_calc_type.id", ondelete="CASCADE",
        sa_type=BigInteger,
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
    # The poll cycle is parsed out of the HostLib CFG but never propagated
    # (config_reader drops it), so every row in the base carries this same 7 and
    # nothing reads it back. Requiring it of a caller asked for a number that
    # cannot be anything else — and rejected every create that left it out.
    c_time: int = 7


class GasVolumeCalcUpdate(GasVolumeCalcBase):
    name: str | None = None
    c_time: int | None = None
    address: int | None = None
    type_id: int | None = None
