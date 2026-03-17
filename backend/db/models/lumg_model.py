from typing import TYPE_CHECKING, Optional

from sqlalchemy import Index
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_model import GasVolumeCalc
    from .grmu_branch_model import GrmuBranch


class LumgBase(HlBaseModel):
    name: str = Field(max_length=255)


class Lumg(LumgBase, table=True):
    __tablename__ = "lumg"
    __table_args__ = (
        # Unique per branch (was global unique before)
        UniqueConstraint("branch_id", "name", name="uq_lumg_grmu_branch_name"),
        Index("idx_lumg_name", "name"),
        Index("idx_lumg_branch", "branch_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    branch_id: int | None = Field(
        default=None,
        foreign_key="grmu_branch.id",
        ondelete="CASCADE",
    )

    branch: Optional["GrmuBranch"] = Relationship(back_populates="lumgs")
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(
        back_populates="lumg", cascade_delete=True
    )
    data_path: Optional["LumgDataPath"] = Relationship(back_populates="lumg")


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

    id: int | None = Field(default=None, primary_key=True)
    lumg_id: int = Field(foreign_key="lumg.id", ondelete="CASCADE", unique=True)
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
