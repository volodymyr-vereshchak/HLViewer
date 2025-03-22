from datetime import datetime
from typing import TYPE_CHECKING

from sqlmodel import Field, UniqueConstraint, Relationship

from backend.db.models import HlBaseModel

if TYPE_CHECKING:
    from .line_model import Line


class ParamBase(HlBaseModel):
    period: datetime = Field(index=True)
    density: float
    co2: float
    n2: float
    D20: float
    d20: float
    cutoff: float
    roughness: float
    max_dp: float
    min_dp: float
    A0su: float
    A1su: float
    A2su: float
    A0pipe: float
    A1pipe: float
    A2pipe: float
    radius: float
    su_year: float
    max_p: float
    min_p: float
    max_t: float
    min_t: float


PARAM_CONSTRAINT = ["line_id", "period"]


class Param(ParamBase, table=True):
    __tablename__ = "params"
    __table_args__ = (
        UniqueConstraint(*PARAM_CONSTRAINT, name="param_line_period_constraint"),
    )
    id: int | None = Field(default=None, primary_key=True)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE", index=True
    )
    line: "Line" = Relationship(back_populates="daily_archives")


class ParamList(ParamBase):
    id: int
    line_id: int


class ParamCreate(ParamBase):
    line_id: int
