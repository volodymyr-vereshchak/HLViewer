from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
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
