"""Response shape of «Звірка ФХП».

Columnar on purpose: an hourly report over 31 days and 10 lines is ~22000 data
points, and a list of six-field objects costs about four times the JSON of
parallel arrays. Every array in a block is aligned with `periods`.
"""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel


class FhpLineStats(BaseModel):
    n: int
    mean_delta: float
    mean_abs_delta: float
    max_abs_delta: float
    max_abs_delta_at: str
    mean_abs_delta_pct: Optional[float] = None
    max_abs_delta_pct: Optional[float] = None
    max_abs_delta_pct_at: Optional[str] = None
    out_of_tolerance: int
    out_of_tolerance_share: float


class FhpLineSeries(BaseModel):
    line_id: int
    line_name: str
    calc_name: Optional[str] = None
    is_reference: bool
    status: Literal["ok", "no_data"]
    values: list[Optional[float]] = []
    # Absent for reference lines and for routes with no reference at all.
    deltas: Optional[list[Optional[float]]] = None
    delta_pcts: Optional[list[Optional[float]]] = None
    # The value in force is older than `stale_after_hours` at this period.
    stale: list[bool] = []
    stats: Optional[FhpLineStats] = None


class FhpParamBlock(BaseModel):
    param: str
    label: str
    unit: str
    decimals: int
    tolerance: float
    tolerance_mode: str
    has_reference: bool
    periods: list[str] = []
    # Daily granularity only: fewer than 24 means the day is incomplete.
    hours_present: Optional[list[int]] = None
    reference: Optional[list[Optional[float]]] = None
    # How many reference lines backed the reference at each period. Not
    # diagnostics: when it changes mid-range the reference itself jumps.
    reference_count: Optional[list[int]] = None
    spread_min: list[Optional[float]] = []
    spread_max: list[Optional[float]] = []
    spread: list[Optional[float]] = []
    lines: list[FhpLineSeries] = []
    rejected_changes: int = 0


class RouteFhpReport(BaseModel):
    route_id: int
    route_number: str
    route_name: Optional[str] = None
    branch_id: int
    granularity: str
    date_from: date
    date_to: date
    contract_hour: int
    stale_after_hours: int
    # The moment the archive reaches for this route's lines — the same value
    # the overview screen shows. Nothing can be compared past it.
    data_until: Optional[str] = None
    # Set when the range was cut back to that moment (exclusive end).
    range_clipped_at: Optional[str] = None
    params: list[FhpParamBlock] = []
    volume: Optional["FhpVolumeBlock"] = None
    warnings: list[str] = []


class FhpVolumeLine(BaseModel):
    """One line's volume as reported, and what it would have been on the
    reference gas."""

    line_id: int
    line_name: str
    # Лічильник vs діафрагма — they answer to a wrong composition differently.
    is_meter: bool
    status: Literal["ok", "no_data"]
    volume: list[Optional[float]] = []
    delta: list[Optional[float]] = []
    delta_pct: list[Optional[float]] = []
    total_volume: Optional[float] = None
    total_delta: Optional[float] = None
    total_delta_pct: Optional[float] = None


class FhpVolumeBlock(BaseModel):
    """ΔV = V_reference − V_reported. Negative means the reported volume was
    overstated. Absent when the route has no reference line — without one
    there is nothing to correct towards."""

    periods: list[str] = []
    lines: list[FhpVolumeLine] = []
    total_delta: Optional[float] = None
    unit: str = "м³"
