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
    daily: WeatherCoverage = WeatherCoverage()
    hourly: WeatherCoverage = WeatherCoverage()


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


class WeatherImport(SQLModel):
    point_id: int
    daily: list[WeatherDailyRow] = []
    hourly: list[WeatherHourlyRow] = []


class WeatherImportResult(SQLModel):
    daily: int
    hourly: int


class WeatherDailyRead(WeatherDailyRow):
    point_id: int


class WeatherHourlyRead(WeatherHourlyRow):
    point_id: int


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
