from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import field_validator
from sqlmodel import Field, UniqueConstraint, Relationship

from .base_model import HlBaseModel

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


PARAM_CONSTRAINT = [field for field in ParamBase.__annotations__ if field != "period"]

PARAM_CONSTRAINT.append("line_id")


class Param(ParamBase, table=True):
    __tablename__ = "params"
    __table_args__ = (UniqueConstraint(*PARAM_CONSTRAINT, name="param_all_constraint"),)
    id: int | None = Field(default=None, primary_key=True)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE", index=True
    )
    line: "Line" = Relationship(back_populates="params")


class ParamList(ParamBase):
    id: int
    line_id: int

    @field_validator("A0su", "A0pipe")
    def validate_a0(cls, value: float):
        return value * 1e6

    @field_validator("A1su", "A1pipe")
    def validate_a1(cls, value: float):
        return value * 1e9

    @field_validator("A2su", "A2pipe")
    def validate_a2(cls, value: float):
        return value * 1e12


class ParamCreate(ParamBase):
    line_id: int
