"""
Weather for the analytics: actual air weather at the GRS, for models that
explain consumption by temperature.

A weather POINT is a place on the map (a GRS, taken from Google Maps) and the
lines whose consumption it explains. One point per GRS rather than per line: a
GRS has several metering lines and one sky.

The data comes from the Open-Meteo archive (ERA5 reanalysis) — downloaded by
the browser when it has internet, or as a file fetched elsewhere and uploaded
when the server is offline. Either way the frontend parses it and posts rows
here; the backend never talks to the internet.

Times are Europe/Kyiv local, like every other stamp in this database.
"""
from datetime import date, datetime
from typing import Optional

from pydantic import field_validator
from sqlalchemy import BigInteger, Column, ForeignKey, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from .base_model import HlBaseModel

LINE_KINDS = ("physical", "virtual", "dpd")


class WeatherPoint(HlBaseModel, table=True):
    __tablename__ = "weather_point"
    __table_args__ = (
        UniqueConstraint("branch_id", "name", name="uq_weather_point_branch_name"),
        Index("ix_weather_point_branch", "branch_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    branch_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False,
        )
    )
    name: str = Field(max_length=255)
    latitude: float
    longitude: float


class WeatherPointLine(SQLModel, table=True):
    """Which lines a point's weather belongs to. Line ids come from one shared
    sequence for all three kinds (shared_line_id_seq), so `line_id` alone is
    unique; `line_kind` says which table to read the archive from. No foreign
    key — it would have to point at three tables; reads drop ids whose line is
    gone."""

    __tablename__ = "weather_point_line"
    __table_args__ = (
        UniqueConstraint("line_id", name="uq_weather_point_line_line"),
        Index("ix_weather_point_line_point", "point_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    point_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("weather_point.id", ondelete="CASCADE"), nullable=False,
        )
    )
    line_kind: str = Field(max_length=16)
    line_id: int = Field(sa_type=BigInteger)


class WeatherDaily(SQLModel, table=True):
    """One calendar day at a point. Units: °C, mm, m/s, MJ/m²."""

    __tablename__ = "weather_daily"
    __table_args__ = (UniqueConstraint("point_id", "day", name="uq_weather_daily_point_day"),)

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    point_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("weather_point.id", ondelete="CASCADE"), nullable=False,
        )
    )
    day: date
    t_mean: Optional[float] = None
    t_min: Optional[float] = None
    t_max: Optional[float] = None
    precipitation: Optional[float] = None
    wind_max: Optional[float] = None
    radiation: Optional[float] = None
    # 'archive' — ERA5 reanalysis (what happened), 'forecast' — a forecast
    # that the archive replaces once the day is published. A forecast never
    # overwrites an archive row (see WeatherDao._upsert).
    source: str = Field(default="archive", max_length=8, sa_column_kwargs={"server_default": "archive"})
    # Application clock (Europe/Kyiv), like every stamp here; updated_at says
    # when the values were last written — for a forecast, how fresh it is.
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WeatherHourly(SQLModel, table=True):
    """One hour at a point, stamped with the hour's start (local time).
    Units: °C, %, m/s, W/m², mm, %."""

    __tablename__ = "weather_hourly"
    __table_args__ = (
        UniqueConstraint("point_id", "stamp", name="uq_weather_hourly_point_stamp"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    point_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("weather_point.id", ondelete="CASCADE"), nullable=False,
        )
    )
    stamp: datetime
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    wind_speed: Optional[float] = None
    radiation: Optional[float] = None
    precipitation: Optional[float] = None
    cloud_cover: Optional[float] = None
    # 'archive' — ERA5 reanalysis (what happened), 'forecast' — a forecast
    # that the archive replaces once the day is published. A forecast never
    # overwrites an archive row (see WeatherDao._upsert).
    source: str = Field(default="archive", max_length=8, sa_column_kwargs={"server_default": "archive"})
    # Application clock (Europe/Kyiv), like every stamp here; updated_at says
    # when the values were last written — for a forecast, how fresh it is.
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ── API schemas ──────────────────────────────────────────────────────────────

class WeatherLineRef(SQLModel):
    kind: str
    id: int


class WeatherCoverage(SQLModel):
    first: Optional[date] = None
    last: Optional[date] = None
    count: int = 0


class WeatherPointRead(SQLModel):
    id: int
    branch_id: int
    name: str
    latitude: float
    longitude: float
    lines: list[WeatherLineRef] = []
    # Archive coverage only; a forecast does not make a day "have weather".
    daily: WeatherCoverage = WeatherCoverage()
    hourly: WeatherCoverage = WeatherCoverage()
    # Last day the stored forecast reaches, and when it was fetched.
    forecast_until: Optional[date] = None
    forecast_updated_at: Optional[datetime] = None


class WeatherPointCreate(SQLModel):
    branch_id: int
    name: str = Field(min_length=1, max_length=255)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    lines: list[WeatherLineRef] = []


class WeatherPointUpdate(SQLModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    # None = leave the lines alone; a list (even empty) replaces them.
    lines: Optional[list[WeatherLineRef]] = None


class WeatherDailyRow(SQLModel):
    day: date
    t_mean: Optional[float] = None
    t_min: Optional[float] = None
    t_max: Optional[float] = None
    precipitation: Optional[float] = None
    wind_max: Optional[float] = None
    radiation: Optional[float] = None


class WeatherHourlyRow(SQLModel):
    stamp: datetime
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    wind_speed: Optional[float] = None
    radiation: Optional[float] = None
    precipitation: Optional[float] = None
    cloud_cover: Optional[float] = None


WEATHER_SOURCES = ("archive", "forecast")


class WeatherImport(SQLModel):
    point_id: int
    source: str = "archive"
    daily: list[WeatherDailyRow] = []
    hourly: list[WeatherHourlyRow] = []


class WeatherImportResult(SQLModel):
    daily: int
    hourly: int


class WeatherDailyRead(WeatherDailyRow):
    point_id: int
    source: str
    updated_at: Optional[datetime] = None


class WeatherHourlyRead(WeatherHourlyRow):
    point_id: int
    source: str
    updated_at: Optional[datetime] = None


class DateRange(SQLModel):
    first: date
    last: date


class WeatherMissing(SQLModel):
    point_id: int
    daily: list[DateRange] = []
    hourly: list[DateRange] = []


class WeatherPlan(SQLModel):
    """What to download for a branch: the span the consumption archives cover
    and, per point, the days inside it that have no weather yet."""

    archive_first: Optional[date] = None
    archive_last: Optional[date] = None
    points: list[WeatherMissing] = []


class WeatherJob(SQLModel, table=True):
    """Single-row (id=1) settings, lock and status of the automatic weather
    update: the scheduler downloads the missing days from Open-Meteo itself,
    once a day at `refresh_time`, when `auto_enabled`. Needs the SERVER to
    reach the internet; an offline server keeps using the manual paths.
    Same lock pattern as dpd_refresh_job."""

    __tablename__ = "weather_job"

    id: Optional[int] = Field(default=None, primary_key=True)
    auto_enabled: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    refresh_time: str = Field(default="12:00", max_length=5, sa_column_kwargs={"server_default": "12:00"})
    status: str = Field(default="idle", max_length=16)  # idle | running | done | error
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None  # heartbeat, database clock
    error: Optional[str] = None
    # What the last run brought, e.g. "248 / 5952" (daily / hourly rows).
    result: Optional[str] = Field(default=None, max_length=255)


class WeatherAutoRead(SQLModel):
    auto_enabled: bool
    refresh_time: str
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[str] = None


class WeatherAutoUpdate(SQLModel):
    auto_enabled: Optional[bool] = None
    refresh_time: Optional[str] = None

    # A validator, not Field(regex=…): SQLModel drops that silently, and a
    # stored "25:00" would break the scheduler's slot arithmetic every tick.
    @field_validator("refresh_time")
    @classmethod
    def _hhmm(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        parts = v.strip().split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
        raise ValueError("Час у форматі ГГ:ХХ")
