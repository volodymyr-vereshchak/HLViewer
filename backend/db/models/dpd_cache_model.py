from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, ForeignKey, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class DpdDailyArchive(SQLModel, table=True):
    """Daily DPD indication records, one row per corrector per day.

    Keyed by the DEVICE, not by the metering point it stood at: a corrector
    keeps one continuous archive across every point it ever served, and a
    point reads slices of it through its assignment windows
    (enterprise_device). Moving a corrector needs no re-poll.

    The DB is the primary source: the scheduler refreshes the last
    DPD_ARCHIVE_WINDOW_DAYS twice a day, older ranges are backfilled on
    demand (see dpd_device_coverage). Reads never hit the DPD API inside
    the refreshed window. Skeleton records (both volume fields NULL) are
    never stored. accessed_at drives retention: rows with `day` older than
    a year are pruned when not read for 7 days."""

    __tablename__ = "dpd_daily_archive"
    __table_args__ = (
        UniqueConstraint("device_id", "day", name="uq_dpd_daily_dev_day"),
        Index("ix_dpd_daily_day", "day"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    device_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("dpd_device.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    day: date
    dvst_alwrk: Optional[float] = None
    dvwrk_alwrk: Optional[float] = None
    press: Optional[float] = None
    temper: Optional[float] = None
    press_unit: Optional[str] = Field(default=None, max_length=16)
    accessed_at: date


class DpdHourlyArchive(SQLModel, table=True):
    """Hourly DPD indication records, one row per corrector per hour.

    Same lifecycle and same device keying as DpdDailyArchive (daily and hourly
    are independent DPD endpoints); `stamp` is the record's date+time."""

    __tablename__ = "dpd_hourly_archive"
    __table_args__ = (
        UniqueConstraint("device_id", "stamp", name="uq_dpd_hourly_dev_stamp"),
        Index("ix_dpd_hourly_stamp", "stamp"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    device_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("dpd_device.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    stamp: datetime
    dvst_alwrk: Optional[float] = None
    dvwrk_alwrk: Optional[float] = None
    press: Optional[float] = None
    temper: Optional[float] = None
    press_unit: Optional[str] = Field(default=None, max_length=16)
    accessed_at: date


class DpdDeviceCoverage(SQLModel, table=True):
    """How far back a device's archive has ever been fetched from DPD.

    loaded_from = the earliest date ever requested from the API for this
    device+period_type. A request with from_date < loaded_from triggers
    an on-demand backfill of [from_date, loaded_from); everything at or
    after loaded_from is served from the DB only. The scheduler lowers it
    to today−window after each run; retention pruning RAISES it back to the
    prune horizon so pruned ranges become backfillable again.

    Per DEVICE, so a corrector shared by two points over time is backfilled
    once and the second point reads what the first already pulled."""

    __tablename__ = "dpd_device_coverage"

    device_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("dpd_device.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    period_type: str = Field(primary_key=True, max_length=8)  # "daily" | "hourly"
    loaded_from: date


class DpdRefreshJob(SQLModel, table=True):
    """Single-row (id=1) lock/status of the DPD archive refresh, shared by all
    uvicorn workers, the scheduler process and the manual admin trigger —
    same pattern as update_job (backend/hl_engine/update_job_lock.py)."""

    __tablename__ = "dpd_refresh_job"

    id: Optional[int] = Field(default=None, primary_key=True)
    status: str = Field(default="idle", max_length=16)  # idle | running | done | error
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None  # heartbeat for crash detection
    error: Optional[str] = None
    # Per-device progress of a running refresh (each device counts twice:
    # daily + hourly). NULL outside a running refresh.
    progress_done: Optional[int] = None
    progress_total: Optional[int] = None
    # Local times (HH:MM, comma-separated) the scheduler refreshes at, set from
    # the admin panel. NULL = never configured there: DPD_REFRESH_TIMES from the
    # environment applies, so an untouched deployment keeps its schedule.
    refresh_times: Optional[str] = Field(default=None, max_length=255)
