from sqlmodel import Field, Relationship, UniqueConstraint
from typing import TYPE_CHECKING
from sqlalchemy import Index

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_type_model import GasVolumeCalcType


class SysTypeBase(HlBaseModel):
    sys_type_id: int
    gas_volume_calc_type_id: int
    sys_name: str


SYS_TYPE_CONSTRAINT = ["sys_type_id", "gas_volume_calc_type_id"]


class SysType(SysTypeBase, table=True):
    __tablename__ = "sys_type"
    __table_args__ = (
        UniqueConstraint(*SYS_TYPE_CONSTRAINT, name="sys_type_id_constraint"),
        # Индекс для поиска по sys_type_id
        Index("idx_sys_type_id", "sys_type_id"),
        # Индекс для поиска по gas_volume_calc_type_id
        Index("idx_sys_gas_volume_calc_type", "gas_volume_calc_type_id"),
        # Индекс для поиска по sys_name
        Index("idx_sys_name", "sys_name"),
    )
    id: int | None = Field(default=None, primary_key=True)


class SysTypeList(SysTypeBase):
    id: int


class SysTypeCreate(SysTypeBase):
    pass


class SysTypeUpdate(SysTypeBase):
    sys_name: str | None = None
