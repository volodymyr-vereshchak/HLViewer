"""
DB model for enterprise (промисловість) — devices mapped to gas lines.
Replaces the Excel-based EnterpriseMapping.
"""

from typing import Optional
from sqlalchemy import BigInteger
from sqlmodel import SQLModel, Field, UniqueConstraint


class EnterpriseBase(SQLModel):
    enterprise_name: str = Field(index=True)
    branch_id: Optional[int] = Field(default=None, foreign_key="grmu_branch.id", ondelete="CASCADE", sa_type=BigInteger)
    line_id: Optional[int] = Field(default=None, foreign_key="gas_volume_line.id", ondelete="SET NULL", sa_type=BigInteger)
    # Alternative line link: a DPD line instead of a physical one. At most one
    # of line_id/dpd_line_id may be set (ck_enterprise_single_line). Ids never
    # collide across line kinds (shared_line_id_seq), so consumers group by
    # the effective id COALESCE(line_id, dpd_line_id).
    dpd_line_id: Optional[int] = Field(default=None, foreign_key="dpd_line.id", ondelete="SET NULL", sa_type=BigInteger)
    # Corrector model the device identity is derived from. SOURCE OF TRUTH for
    # mf_dev/type_dev: when set, the effective codes come from
    # corector_type.type_dev and corector_type.manufacturer.mf_dev, so editing
    # the catalog propagates to every enterprise. RESTRICT prevents deleting a
    # corrector type that is still in use. The legacy mf_dev/type_dev columns
    # below are kept as a fallback only for rows that aren't linked yet.
    corector_type_id: Optional[int] = Field(default=None, foreign_key="corector_type.id", ondelete="RESTRICT", sa_type=BigInteger)
    ser_num: int
    mf_dev: Optional[int] = None    # legacy fallback (used only when corector_type_id is NULL)
    type_dev: Optional[int] = None  # legacy fallback (used only when corector_type_id is NULL)
    ch_num: int    # channel number (0-based)
    active: bool = Field(default=True)    # our flag for inclusion in queries
    enabled: bool = Field(default=True)   # metering point status from DPD


class Enterprise(EnterpriseBase, table=True):
    __tablename__ = "enterprise"
    __table_args__ = (
        # Device identity per corrector type. Must match the Alembic migration
        # e8f9a0b1c2d3 — the Excel upload upserts ON CONFLICT ON CONSTRAINT by
        # this exact name.
        UniqueConstraint(
            "ser_num", "corector_type_id", "ch_num", name="uq_enterprise_device_ct"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)


class EnterpriseRead(EnterpriseBase):
    id: int
    # Enriched, resolved through corector_type when linked (else legacy values):
    #   mf_dev / type_dev here are the EFFECTIVE codes;
    #   model_name / manufacturer_short_name are for display.
    model_name: Optional[str] = None
    manufacturer_short_name: Optional[str] = None


class EnterpriseCreate(EnterpriseBase):
    pass


class EnterpriseUpdate(SQLModel):
    enterprise_name: Optional[str] = None
    branch_id: Optional[int] = None
    line_id: Optional[int] = None
    dpd_line_id: Optional[int] = None
    corector_type_id: Optional[int] = None
    ser_num: Optional[int] = None
    mf_dev: Optional[int] = None
    type_dev: Optional[int] = None
    ch_num: Optional[int] = None
    active: Optional[bool] = None
    enabled: Optional[bool] = None
