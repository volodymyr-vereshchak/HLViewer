"""
GrmuBranch (Філіал ГРМУ) and related DB models.

New hierarchy:
  GrmuBranch
    ├── GrmuBranchDpdCredential  (1:1)
    ├── Lumg                     (branch_id FK)
    ├── GrmuBranchDeviceMapping  (replaces Excel)
    └── VirtualLine              (replaces JSON)
          └── VirtualLineMember  (junction)
"""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Index
from sqlmodel import Field, Relationship, UniqueConstraint

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .lumg_model import Lumg


# ─── GrmuBranch ───────────────────────────────────────────────────────────────


class GrmuBranchBase(HlBaseModel):
    name: str = Field(max_length=255)
    short_name: Optional[str] = Field(default=None, max_length=64)
    region: Optional[str] = Field(default=None, max_length=255)
    active: bool = Field(default=True)


class GrmuBranch(GrmuBranchBase, table=True):
    __tablename__ = "grmu_branch"
    __table_args__ = (
        UniqueConstraint("name", name="uq_grmu_branch_name"),
        Index("idx_grmu_branch_name", "name"),
        Index("idx_grmu_branch_active", "active"),
    )

    id: int | None = Field(default=None, primary_key=True)

    lumgs: List["Lumg"] = Relationship(
        back_populates="branch", cascade_delete=True
    )
    dpd_credential: Optional["GrmuBranchDpdCredential"] = Relationship(
        back_populates="branch", cascade_delete=True
    )
    device_mappings: List["GrmuBranchDeviceMapping"] = Relationship(
        back_populates="branch", cascade_delete=True
    )
    virtual_lines: List["VirtualLine"] = Relationship(
        back_populates="branch", cascade_delete=True
    )


class GrmuBranchList(GrmuBranchBase):
    id: int


class GrmuBranchCreate(GrmuBranchBase):
    pass


class GrmuBranchUpdate(GrmuBranchBase):
    name: Optional[str] = None
    active: Optional[bool] = None


# ─── GrmuBranchDpdCredential ──────────────────────────────────────────────────


class GrmuBranchDpdCredentialBase(HlBaseModel):
    api_base_url: str
    auth_url: str
    username: str = Field(max_length=255)
    password: str
    timeout_sec: int = Field(default=30)


class GrmuBranchDpdCredential(GrmuBranchDpdCredentialBase, table=True):
    __tablename__ = "grmu_branch_dpd_credential"

    id: int | None = Field(default=None, primary_key=True)
    branch_id: int = Field(
        foreign_key="grmu_branch.id",
        unique=True,
        ondelete="CASCADE",
    )

    branch: "GrmuBranch" = Relationship(back_populates="dpd_credential")


class GrmuBranchDpdCredentialCreate(GrmuBranchDpdCredentialBase):
    branch_id: int


class GrmuBranchDpdCredentialUpdate(GrmuBranchDpdCredentialBase):
    api_base_url: Optional[str] = None
    auth_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    timeout_sec: Optional[int] = None


# ─── GrmuBranchDeviceMapping ──────────────────────────────────────────────────


class GrmuBranchDeviceMappingBase(HlBaseModel):
    ser_num: int
    mf_dev: int
    type_dev: int
    ch_num: int
    grmu_branch_name: Optional[str] = Field(default=None, max_length=255)
    counterpart: Optional[str] = Field(default=None, max_length=255)
    sector: Optional[str] = Field(default=None, max_length=255)
    status: Optional[str] = Field(default=None, max_length=32)
    status_changed_at: Optional[datetime] = None
    device_type: Optional[str] = None
    manufacturer: Optional[str] = None
    active: Optional[bool] = None


class GrmuBranchDeviceMapping(GrmuBranchDeviceMappingBase, table=True):
    __tablename__ = "grmu_branch_device_mapping"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "ser_num", "mf_dev", "type_dev", "ch_num",
            name="uq_branch_device",
        ),
        Index("idx_branch_device_mapping_branch", "branch_id"),
        Index("idx_branch_device_mapping_line", "line_id"),
        Index(
            "idx_branch_device_mapping_device",
            "ser_num", "mf_dev", "type_dev", "ch_num",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    branch_id: int = Field(foreign_key="grmu_branch.id", ondelete="CASCADE")
    # SET NULL so orphaned mappings remain visible when a line is deleted
    line_id: int | None = Field(
        default=None,
        foreign_key="gas_volume_line.id",
        ondelete="SET NULL",
    )

    branch: "GrmuBranch" = Relationship(back_populates="device_mappings")


class GrmuBranchDeviceMappingList(GrmuBranchDeviceMappingBase):
    id: int
    branch_id: int
    line_id: Optional[int] = None


class GrmuBranchDeviceMappingCreate(GrmuBranchDeviceMappingBase):
    branch_id: int
    line_id: Optional[int] = None


# ─── VirtualLine ──────────────────────────────────────────────────────────────


class VirtualLineBase(HlBaseModel):
    name: str = Field(max_length=255)
    description: Optional[str] = None
    active: bool = Field(default=True)
    include_in_report: bool = Field(default=False)
    is_high_pressure: bool = Field(default=False)


class VirtualLine(VirtualLineBase, table=True):
    __tablename__ = "virtual_line"
    __table_args__ = (
        UniqueConstraint("branch_id", "name", name="uq_virtual_line_branch_name"),
        Index("idx_virtual_line_branch", "branch_id"),
        Index("idx_virtual_line_active", "active"),
        Index("idx_virtual_line_include_in_report", "include_in_report"),
    )

    id: int | None = Field(default=None, primary_key=True)
    branch_id: int = Field(foreign_key="grmu_branch.id", ondelete="CASCADE")

    branch: "GrmuBranch" = Relationship(back_populates="virtual_lines")
    members: List["VirtualLineMember"] = Relationship(
        back_populates="virtual_line", cascade_delete=True
    )


class VirtualLineList(VirtualLineBase):
    id: int
    branch_id: int


class VirtualLineCreate(VirtualLineBase):
    branch_id: int


# ─── VirtualLineMember ────────────────────────────────────────────────────────


class VirtualLineMember(HlBaseModel, table=True):
    __tablename__ = "virtual_line_member"
    __table_args__ = (
        UniqueConstraint(
            "virtual_line_id", "line_id",
            name="uq_vlm_virtual_line_line",
        ),
        Index("idx_vlm_virtual_line", "virtual_line_id"),
        Index("idx_vlm_line", "line_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    virtual_line_id: int = Field(
        foreign_key="virtual_line.id", ondelete="CASCADE"
    )
    line_id: int = Field(
        foreign_key="gas_volume_line.id", ondelete="CASCADE"
    )
    sort_order: int = Field(default=0)

    virtual_line: "VirtualLine" = Relationship(back_populates="members")


class VirtualLineMemberList(HlBaseModel):
    id: int
    virtual_line_id: int
    line_id: int
    sort_order: int
