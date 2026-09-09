"""
DB models for enterprise (промисловість): metering points, the corrector
registry and the assignment history that ties them together.

A metering point (`enterprise`) is a place gas is measured at; a corrector
(`dpd_device`) is the physical instrument addressed on the DPD API by
(ser_num, mf_dev, type_dev, ch_num). Correctors get moved between points, so
the two are separate rows joined by `enterprise_device` — one entry per
"this device stood here from … to …".

The DPD archive is keyed by DEVICE, not by point: a corrector keeps one
continuous archive no matter where it stood, and a point reads slices of it
through its assignment windows. Moving a corrector or correcting an install
date therefore needs no re-poll — only a different slice.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import BigInteger, Column, ForeignKey, Index
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint


# ─── Corrector registry ───────────────────────────────────────────────────────


class DpdDevice(SQLModel, table=True):
    """One corrector channel — the unit the DPD API is addressed by and the
    unit the archive is keyed by.

    `corector_type_id` is the source of truth for mf_dev/type_dev (editing the
    catalog propagates to polling). The legacy mf_dev/type_dev columns are a
    fallback for rows the catalog backfill (migration e8f9a0b1c2d3) could not
    match; the unique constraint deliberately repeats the old
    uq_enterprise_device_ct, NULL looseness included, so the migration could
    seed this table one-to-one out of `enterprise`.
    """

    __tablename__ = "dpd_device"
    __table_args__ = (
        UniqueConstraint(
            "ser_num", "corector_type_id", "ch_num", name="uq_dpd_device_ident"
        ),
        Index("ix_dpd_device_ser", "ser_num"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    ser_num: int = Field(sa_type=BigInteger)
    corector_type_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            BigInteger,
            ForeignKey("corector_type.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    mf_dev: Optional[int] = None    # legacy fallback (corector_type_id IS NULL)
    type_dev: Optional[int] = None  # legacy fallback (corector_type_id IS NULL)
    ch_num: int = 0


# ─── Metering point ───────────────────────────────────────────────────────────


class EnterpriseBase(SQLModel):
    enterprise_name: str = Field(index=True)
    branch_id: Optional[int] = Field(default=None, foreign_key="grmu_branch.id", ondelete="CASCADE", sa_type=BigInteger)
    line_id: Optional[int] = Field(default=None, foreign_key="gas_volume_line.id", ondelete="SET NULL", sa_type=BigInteger)
    # Alternative line link: a DPD line instead of a physical one. At most one
    # of line_id/dpd_line_id may be set (ck_enterprise_single_line). Ids never
    # collide across line kinds (shared_line_id_seq), so consumers group by
    # the effective id COALESCE(line_id, dpd_line_id).
    dpd_line_id: Optional[int] = Field(default=None, foreign_key="dpd_line.id", ondelete="SET NULL", sa_type=BigInteger)
    active: bool = Field(default=True)    # our flag for inclusion in queries
    enabled: bool = Field(default=True)   # metering point status from DPD


class Enterprise(EnterpriseBase, table=True):
    __tablename__ = "enterprise"
    __table_args__ = (
        # Created by a migration but never declared here, which is why
        # `alembic check` kept proposing to drop it.
        Index("ix_enterprise_dpd_line", "dpd_line_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)

    devices: List["EnterpriseDevice"] = Relationship(
        back_populates="enterprise", cascade_delete=True
    )


# ─── Assignment history ───────────────────────────────────────────────────────


class EnterpriseDevice(SQLModel, table=True):
    """"Device D stood at point E from `installed_from` (to `removed_at`)".

    Both stamps are naive Europe/Kyiv at hour precision — DPD's hourly records
    land on the hour, so there is nothing finer to line a replacement up with.

    `removed_at` is what separates this from the DPD-line history, where a
    device simply runs until the next one is installed. Here correctors move
    between points: taken off on the 5th and replaced on the 10th, the chained
    reading would hand those five days to a corrector that was already
    measuring another point's gas. An explicit removal leaves the gap
    unattributed, which is the truth.
    """

    __tablename__ = "enterprise_device"
    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "installed_from", name="uq_enterprise_device_from"
        ),
        Index("ix_enterprise_device_ent", "enterprise_id"),
        Index("ix_enterprise_device_dev", "device_id", "installed_from"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    enterprise_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("enterprise.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    device_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("dpd_device.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    installed_from: datetime
    removed_at: Optional[datetime] = None

    enterprise: "Enterprise" = Relationship(back_populates="devices")


# `installed_from` of a point that has always had the same device: the history
# starts before any archive does, so every existing row keeps its point.
EPOCH_INSTALLED_FROM = datetime(2000, 1, 1)


# ─── API models ───────────────────────────────────────────────────────────────


class EnterpriseDeviceIn(SQLModel):
    ser_num: int
    corector_type_id: Optional[int] = None
    ch_num: int = 0
    installed_from: datetime
    removed_at: Optional[datetime] = None
    # Raw DPD codes, used ONLY when the corrector is not linked to the catalog.
    # The admin panel always picks a model and leaves these out; they exist so
    # a device can still be addressed on the API when no catalog entry matches
    # it — the state migration e8f9a0b1c2d3 left unmatched rows in.
    mf_dev: Optional[int] = None
    type_dev: Optional[int] = None


class EnterpriseDeviceRead(EnterpriseDeviceIn):
    id: int
    device_id: int
    # Resolved through the corector_type → manufacturer catalog, falling back
    # to the device's legacy columns when it is not linked.
    mf_dev: Optional[int] = None
    type_dev: Optional[int] = None
    model_name: Optional[str] = None
    manufacturer_short_name: Optional[str] = None
    # Effective window end: removed_at, else the next entry's installed_from,
    # else None (still installed).
    bound_to: Optional[datetime] = None


class EnterpriseRead(EnterpriseBase):
    id: int
    devices: List[EnterpriseDeviceRead] = []


class EnterpriseCreate(EnterpriseBase):
    devices: List[EnterpriseDeviceIn] = []


class EnterpriseUpdate(SQLModel):
    enterprise_name: Optional[str] = None
    branch_id: Optional[int] = None
    line_id: Optional[int] = None
    dpd_line_id: Optional[int] = None
    active: Optional[bool] = None
    enabled: Optional[bool] = None
    # Absent = leave the history alone; present = replace it wholesale.
    devices: Optional[List[EnterpriseDeviceIn]] = None
