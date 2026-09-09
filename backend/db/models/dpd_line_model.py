"""
DPD lines: lines whose archive data comes from the DPD API.

A DPD line is a third line kind next to physical (gas_volume_line) and
virtual (virtual_line) lines; its id is drawn from shared_line_id_seq so
the three kinds never collide. Each line has a device (corrector) history:
a device's data-validity window is [installed_from, next device's
installed_from), the last device's window is open-ended. Windows are
derived from the ordered history, never stored — so a replacement can
never let the new corrector's archive overwrite periods that belong to
the old one.
"""

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import BigInteger, Column, ForeignKey, Index, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

from .base_model import HlBaseModel


# ─── DpdLine ──────────────────────────────────────────────────────────────────


class DpdLineBase(HlBaseModel):
    name: str = Field(max_length=255)
    description: Optional[str] = None
    active: bool = Field(default=True)
    include_in_trends: bool = Field(default=False)
    include_in_report: bool = Field(default=False)


class DpdLine(DpdLineBase, table=True):
    __tablename__ = "dpd_line"
    __table_args__ = (
        UniqueConstraint("branch_id", "name", name="uq_dpd_line_branch_name"),
        Index("ix_dpd_line_branch", "branch_id"),
        # Created by a migration but never declared here, which is why
        # `alembic check` kept proposing to drop it.
        Index("ix_dpd_line_include_in_report", "include_in_report"),
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    branch_id: int = Field(
        foreign_key="grmu_branch.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    lumg_id: int | None = Field(
        default=None, foreign_key="lumg.id", ondelete="SET NULL", sa_type=BigInteger,
    )

    devices: List["DpdLineDevice"] = Relationship(
        back_populates="dpd_line", cascade_delete=True
    )


# ─── DpdLineDevice (history entry) ────────────────────────────────────────────


class DpdLineDevice(HlBaseModel, table=True):
    __tablename__ = "dpd_line_device"
    __table_args__ = (
        UniqueConstraint("dpd_line_id", "installed_from",
                         name="uq_dpd_line_device_from"),
        Index("ix_dpd_line_device_line", "dpd_line_id"),
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    dpd_line_id: int = Field(
        foreign_key="dpd_line.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    ser_num: int = Field(sa_type=BigInteger)
    corector_type_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("corector_type.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    ch_num: int
    # Naive Europe/Kyiv, hour precision (minutes/seconds forced to 0).
    installed_from: datetime

    dpd_line: "DpdLine" = Relationship(back_populates="devices")


# ─── Archives (permanent, unlike the pruned enterprise DPD cache) ─────────────


class DpdLineDailyArchive(SQLModel, table=True):
    """One row per DPD line per commercial day. `volume` is already the
    resolved commercial volume (volume_field_for_device applied at store
    time). Skeleton records (null volume field) are never stored."""

    __tablename__ = "dpd_line_daily_archive"
    __table_args__ = (
        UniqueConstraint("dpd_line_id", "day", name="uq_dpd_line_daily"),
        Index("ix_dpd_line_daily_day", "day"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    dpd_line_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("dpd_line.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    day: date
    volume: Optional[float] = None
    pressure: Optional[float] = None
    temperature: Optional[float] = None
    press_unit: Optional[str] = Field(default=None, max_length=16)


class DpdLineHourlyArchive(SQLModel, table=True):
    __tablename__ = "dpd_line_hourly_archive"
    __table_args__ = (
        UniqueConstraint("dpd_line_id", "stamp", name="uq_dpd_line_hourly"),
        Index("ix_dpd_line_hourly_stamp", "stamp"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    dpd_line_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("dpd_line.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    stamp: datetime
    volume: Optional[float] = None
    pressure: Optional[float] = None
    temperature: Optional[float] = None
    press_unit: Optional[str] = Field(default=None, max_length=16)


# ─── DpdLineJob (per-line init/update lock + progress) ────────────────────────


class DpdLineJob(SQLModel, table=True):
    """One row per line: lock/status shared by uvicorn workers (manual init)
    and the scheduler (incremental update) — same pattern as dpd_refresh_job
    but per line, so an init and an update of the same line never overlap."""

    __tablename__ = "dpd_line_job"

    dpd_line_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("dpd_line.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    kind: str = Field(default="init", max_length=8)  # init | update
    status: str = Field(default="idle", max_length=16)  # idle|running|done|error
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None  # heartbeat for crash detection
    error: Optional[str] = None
    # Progress of a running job: each device counts twice (daily + hourly).
    progress_done: Optional[int] = None
    progress_total: Optional[int] = None


# ─── API models ───────────────────────────────────────────────────────────────


class DpdLineDeviceIn(SQLModel):
    ser_num: int
    corector_type_id: int
    ch_num: int = 0
    installed_from: datetime


class DpdLineDeviceRead(DpdLineDeviceIn):
    id: int
    # Resolved through the corector_type → manufacturer catalog.
    mf_dev: Optional[int] = None
    type_dev: Optional[int] = None
    model_name: Optional[str] = None
    manufacturer_short_name: Optional[str] = None
    # Derived window end: next device's installed_from, None = open-ended.
    bound_to: Optional[datetime] = None


class DpdLineCreate(DpdLineBase):
    branch_id: int
    lumg_id: Optional[int] = None
    devices: list[DpdLineDeviceIn] = []


class DpdLineList(DpdLineBase):
    id: int
    branch_id: int
    lumg_id: Optional[int] = None
    devices: list[DpdLineDeviceRead] = []
