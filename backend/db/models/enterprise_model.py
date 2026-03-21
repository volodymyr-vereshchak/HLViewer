"""
DB model for enterprise (промисловість) — devices mapped to gas lines.
Replaces the Excel-based EnterpriseMapping.
"""

from typing import Optional
from sqlmodel import SQLModel, Field


class EnterpriseBase(SQLModel):
    enterprise_name: str = Field(index=True)
    branch_id: Optional[int] = Field(default=None, foreign_key="grmu_branch.id")
    line_id: Optional[int] = Field(default=None, foreign_key="gas_volume_line.id")
    ser_num: int
    mf_dev: int    # 1=РадмирТех, 3=ГРЕМПІС, 4=Тандем, 5=Укргазтех
    type_dev: int
    ch_num: int    # channel number (0-based)
    active: bool = Field(default=True)    # our flag for inclusion in queries
    enabled: bool = Field(default=True)   # metering point status from DPD


class Enterprise(EnterpriseBase, table=True):
    __tablename__ = "enterprise"

    id: Optional[int] = Field(default=None, primary_key=True)


class EnterpriseRead(EnterpriseBase):
    id: int


class EnterpriseCreate(EnterpriseBase):
    pass


class EnterpriseUpdate(SQLModel):
    enterprise_name: Optional[str] = None
    branch_id: Optional[int] = None
    line_id: Optional[int] = None
    ser_num: Optional[int] = None
    mf_dev: Optional[int] = None
    type_dev: Optional[int] = None
    ch_num: Optional[int] = None
    active: Optional[bool] = None
    enabled: Optional[bool] = None
