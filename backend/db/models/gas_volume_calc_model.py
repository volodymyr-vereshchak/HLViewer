from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lumg_model import Lumg
    from .gas_volume_calc_type_model import GasVolumeCalcType
    from .daily_archive_model import DailyArchive
    from .hourly_archive_model import HourlyArchive
    from .edit_archive_model import EditArchive
    from .sys_archive_model import SysArchive

class GasVolumeCalcBase(SQLModel):
    address: int
    meter: bool
    name: str = Field(max_length=255, unique=True)
    c_time: int

GAS_VOLUME_CALC_CONSTRAINT = ["lumg_id", "address"]

class GasVolumeCalc(GasVolumeCalcBase, table=True):
    __tablename__ = "gas_volume_calc"
    __table_args__ = (
        UniqueConstraint(*GAS_VOLUME_CALC_CONSTRAINT, name="lumg_adress_constraint"),
    )

    id: int | None = Field(default=None, primary_key=True)
    lumg_id: int | None = Field(default=None, foreign_key="lumg.id", ondelete="CASCADE")
    lumg: "Lumg" = Relationship(back_populates="gas_volume_calcs", cascade_delete=True)
    type_id: int | None = Field(default=None, foreign_key="gas_vol_calc_type.id")
    type: "GasVolumeCalcType" = Relationship(back_populates="gas_volume_calcs", cascade_delete=True)
    daily_archives: list["DailyArchive"] = Relationship(back_populates="gas_volume_calc", cascade_delete=True)
    hourly_archives: list["HourlyArchive"] = Relationship(back_populates="gas_volume_calc", cascade_delete=True)
    edit_archives: list["EditArchive"] = Relationship(back_populates="gas_volume_calc", cascade_delete=True)
    sys_archives: list["SysArchive"] = Relationship(back_populates="gas_volume_calc", cascade_delete=True)

class GasVolumeCalcList(GasVolumeCalcBase):
    id: int
    lumg_id: int
    type_id: int

class GasVolumeCalcCreate(GasVolumeCalcBase):
    lumg_id: int
    type_id: int

class GasVolumeCalcUpdate(GasVolumeCalcBase):
    lumg_id: int | None = None
    type_id: int | None = None
    name: str | None = None
    c_time: int | None = None
    address: int | None = None
    meter: bool | None = None
