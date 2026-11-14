"""
Storage for the GRS consumption models (backend/services/grs_model).

What is kept is what an operator uploads from 1С — the daily inflow of every
GRS and the daily non-household consumption by category — and the model
trained on it. The stations here are the GRS as 1С names them, not the lines
of this database: the balance is kept per GRS, a GRS has several metering
lines, and the ring of Zaporizhzhia is three stations under one name. Mapping
a station to lines comes later and is not needed to model it.

Volumes are in m³, per calendar day as 1С reports them.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from .base_model import HlBaseModel


class GrsStation(HlBaseModel, table=True):
    __tablename__ = "grs_model_station"
    __table_args__ = (
        UniqueConstraint("branch_id", "name", name="uq_grs_model_station_branch_name"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    branch_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False,
        )
    )
    #: As the reports name it, normalised (services/grs_model/parse.grs_name).
    name: str = Field(max_length=255)
    #: Off: the station is left out of training and of the comparison — one
    #: that is not a GRS of this branch at all, say.
    enabled: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    #: Whose weather explains it. NULL: the mean of the branch's points.
    weather_point_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            BigInteger, ForeignKey("weather_point.id", ondelete="SET NULL"), nullable=True,
        ),
    )


class GrsVolume(SQLModel, table=True):
    """One figure of a report: a station, a day, a category.

    The inflow is stored under the category «Надходження», next to the
    non-household ones, so that both reports land in one table and a day is
    never held twice: the key is (station, day, category).
    """

    __tablename__ = "grs_model_volume"
    __table_args__ = (
        UniqueConstraint("station_id", "day", "category", name="uq_grs_model_volume_key"),
        Index("ix_grs_model_volume_station_day", "station_id", "day"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    station_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("grs_model_station.id", ondelete="CASCADE"), nullable=False,
        )
    )
    day: date
    category: str = Field(max_length=64)
    volume: float
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)


class GrsCoveredDay(SQLModel, table=True):
    """A day some uploaded report had a column for.

    A report lists only what is not zero, so «nothing stored» has two
    meanings: the day was in a report and the station consumed nothing, or no
    report for that day has been uploaded yet. The first is a zero, the second
    is unknown — and telling them apart is what this table is for.
    """

    __tablename__ = "grs_model_day"

    branch_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("grmu_branch.id", ondelete="CASCADE"),
            primary_key=True, nullable=False,
        )
    )
    kind: str = Field(max_length=16, primary_key=True)      # inflow | consumption
    day: date = Field(primary_key=True)


class GrsUpload(SQLModel, table=True):
    """The journal of uploads: what came in, and what it changed."""

    __tablename__ = "grs_model_upload"
    __table_args__ = (Index("ix_grs_model_upload_branch", "branch_id", "uploaded_at"),)

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    branch_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False,
        )
    )
    filename: str = Field(max_length=255)
    kind: str = Field(max_length=16)
    layout: str = Field(max_length=16)
    title: str = Field(default="", max_length=500)
    first_day: date
    last_day: date
    days: int
    stations: int
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    uploaded_at: datetime = Field(default_factory=datetime.now, nullable=False)
    uploaded_by: Optional[str] = Field(default=None, max_length=255)


class GrsFit(SQLModel, table=True):
    """A trained model of a branch. The newest row is the one in use.

    `summary` is what the screen lists (quality by station); `payload` is the
    model itself — coefficients, the days it was trained on and the
    predictions it made for each month while that month was held out.
    """

    __tablename__ = "grs_model_fit"
    __table_args__ = (Index("ix_grs_model_fit_branch", "branch_id", "trained_at"),)

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    branch_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False,
        )
    )
    trained_at: datetime = Field(default_factory=datetime.now, nullable=False)
    trained_by: Optional[str] = Field(default=None, max_length=255)
    first_day: date
    last_day: date
    summary: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    payload: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))


# ── API shapes ─────────────────────────────────────────────────────────────

class GrsStationUpdate(SQLModel):
    enabled: Optional[bool] = None
    #: 0 clears the mapping (back to the branch mean).
    weather_point_id: Optional[int] = None


class GrsStationPoint(SQLModel):
    """Where a station is: its weather point is made there. Not needed when
    a point of the station's name already exists — it is tied to that one."""
    station_id: int
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
