from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import BigInteger, Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel


class DpdVolumeCache(SQLModel, table=True):
    """Server-side cache of raw DPD indication records.

    One row = everything the DPD API returned for one device for one calendar
    day (24 hourly records or 1 daily record) — the unit the volume endpoints
    can reassemble any requested range from. Shared by all uvicorn workers via
    Postgres, unlike the per-browser localStorage cache on the frontend.

    Freshness is decided by the reader (enterprise_volume_service): closed gas
    days are trusted for 24h, the current gas day only for minutes. Days the
    API returned no records for are deliberately NOT cached — data may appear
    in DPD later, so absent days are re-polled on every request.
    """

    __tablename__ = "dpd_volume_cache"
    __table_args__ = (
        UniqueConstraint(
            "ser_num", "mf_dev", "type_dev", "ch_num", "period_type", "day",
            name="uq_dpd_cache_device_period_day",
        ),
        Index("ix_dpd_cache_fetched_at", "fetched_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    ser_num: int = Field(sa_type=BigInteger)
    mf_dev: int
    type_dev: int
    ch_num: int
    period_type: str = Field(max_length=8)  # "daily" | "hourly"
    day: date
    payload: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )
    fetched_at: datetime


class DpdActivePoll(SQLModel, table=True):
    """Ephemeral registry of DPD polls currently talking to the API.

    One row per in-flight poll: its request window, period_type and the
    hashes of the devices it covers. A later request whose window AND device
    set are fully contained in a running poll waits on the poll's advisory
    lock (lock_key) and is then served from cache; everything else runs in
    parallel. Rows are deleted on completion; stale leftovers (crashed
    workers) are purged lazily at registration time, and the table is
    UNLOGGED — no WAL cost, auto-truncated on a Postgres restart. Nobody can
    hang on a stale row: the advisory lock dies with its transaction."""

    __tablename__ = "dpd_active_poll"
    __table_args__ = {"prefixes": ["UNLOGGED"]}

    lock_key: str = Field(primary_key=True, max_length=64)
    period_type: str = Field(max_length=8)
    window_from: datetime
    window_to: datetime
    device_hashes: List[int] = Field(
        sa_column=Column(ARRAY(BigInteger), nullable=False)
    )
    started_at: datetime
