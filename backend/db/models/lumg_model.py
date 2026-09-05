from typing import TYPE_CHECKING, Optional

from sqlalchemy import Index, BigInteger
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_model import GasVolumeCalc
    from .grmu_branch_model import GrmuBranch, VirtualLine


class LumgBase(HlBaseModel):
    name: str = Field(max_length=255)


class Lumg(LumgBase, table=True):
    __tablename__ = "lumg"
    __table_args__ = (
        # Unique per branch (was global unique before)
        UniqueConstraint("branch_id", "name", name="uq_lumg_grmu_branch_name"),
        Index("idx_lumg_branch", "branch_id"),
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    # NOT NULL in the database since the branch rollout, and every ЛУМГ does
    # belong to one. The model kept saying Optional, so autogenerate proposed
    # loosening the column on every run.
    branch_id: int = Field(
        foreign_key="grmu_branch.id",
        ondelete="CASCADE",
        sa_type=BigInteger,
    )

    branch: Optional["GrmuBranch"] = Relationship(back_populates="lumgs")
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(
        back_populates="lumg", cascade_delete=True
    )
    data_path: Optional["LumgDataPath"] = Relationship(back_populates="lumg")
    virtual_lines: list["VirtualLine"] = Relationship(back_populates="lumg")


class LumgList(LumgBase):
    id: int
    branch_id: Optional[int] = None


class LumgCreate(LumgBase):
    branch_id: int


class LumgUpdate(LumgBase):
    name: str | None = None
    branch_id: int | None = None


class LumgDataPath(HlBaseModel, table=True):
    __tablename__ = "lumg_data_path"

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    lumg_id: int = Field(
        foreign_key="lumg.id", ondelete="CASCADE", unique=True, sa_type=BigInteger,
    )
    path: str = Field(max_length=1024)
    active: bool = Field(default=True)

    lumg: Optional["Lumg"] = Relationship(back_populates="data_path")


class LumgDataPathRead(SQLModel):
    id: int
    lumg_id: int
    path: str
    active: bool


class LumgDataPathUpsert(SQLModel):
    path: str
    active: bool = True


class LumgEisCode(HlBaseModel, table=True):
    __tablename__ = "lumg_eis_code"
    __table_args__ = (
        UniqueConstraint("eis_code", name="uq_lumg_eis_code"),
        Index("idx_lumg_eis_code_lumg_id", "lumg_id"),
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    lumg_id: int = Field(
        foreign_key="lumg.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    eis_code: str = Field(max_length=100)


class LumgEisCodeRead(SQLModel):
    id: int
    lumg_id: int
    eis_code: str


class LumgEisCodeCreate(SQLModel):
    eis_code: str
