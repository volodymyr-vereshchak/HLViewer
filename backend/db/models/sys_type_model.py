from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gas_volume_calc_type_model import GasVolumeCalcType
    from .sys_archive_model import SysArchive


class SysTypeBase(SQLModel):
    sys_type_id: int
    sys_name: str


SYS_TYPE_CONSTRAINT = ["sys_type_id", "gas_volume_calc_type_id"]


class SysType(SysTypeBase, table=True):
    __tablename__ = "sys_type"
    __table_args__ = (
        UniqueConstraint(*SYS_TYPE_CONSTRAINT, name="sys_type_id_constraint"),
    )
    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calc_type_id: int | None = Field(
        default=None, foreign_key="gas_vol_calc_type.id", ondelete="CASCADE"
    )
    gas_volume_calc_type: "GasVolumeCalcType" = Relationship(back_populates="sys_types")

    sys_archives: list["SysArchive"] = Relationship(
        back_populates="sys_type", cascade_delete=True
    )


class SysTypeList(SysTypeBase):
    id: int
    gas_volume_calc_type_id: int


class SysTypeCreate(SysTypeBase):
    gas_volume_calc_type_id: int


class SysTypeUpdate(SysTypeBase):
    sys_name: str | None = None
