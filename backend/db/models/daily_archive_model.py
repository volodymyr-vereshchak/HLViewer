from sqlmodel import Field, Relationship, UniqueConstraint
from datetime import date
from decimal import Decimal
from pydantic import field_validator
from typing import TYPE_CHECKING, ClassVar
from sqlalchemy import Index, BigInteger
from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .line_model import Line


class DailyArchiveBase(HlBaseModel):
    period: date = Field(index=True)
    volume: float
    w_volume_dp: float
    pressure: float
    temperature: float
    density: float


DAILY_ARCHIVE_CONSTRAINT = ["line_id", "period", "volume"]


class DailyArchive(DailyArchiveBase, table=True):
    __tablename__: ClassVar[str] = "daily_archive"
    __table_args__ = (
        UniqueConstraint(
            *DAILY_ARCHIVE_CONSTRAINT, name="day_calc_id_line_period_constraint"
        ),
        # Оптимизированные индексы для запросов по диапазону дат и line_id
        Index("idx_daily_line_period", "line_id", "period"),
        Index("idx_daily_period_line", "period", "line_id"),
        # Индекс для агрегации по дням
        Index("idx_daily_period_day", "period"),
    )
    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE",
        index=True, sa_type=BigInteger,
    )
    line: "Line" = Relationship(back_populates="daily_archives")


class DailyArchiveList(DailyArchiveBase):
    id: int
    line_id: int

    @field_validator("density")
    def validate_density(cls, value: float):
        if value > 1 or value < 0.5:
            value = 0
        return value


class DailyArchiveCreate(DailyArchiveBase):
    line_id: int


if __name__ == "__main__":
    d_a = DailyArchiveCreate(
        line_id=1,
        period=date(2024, 1, 1),
        volume=0,
        w_volume_dp=0,
        pressure=0,
        temperature=0,
        density=2,
    )
    pass
