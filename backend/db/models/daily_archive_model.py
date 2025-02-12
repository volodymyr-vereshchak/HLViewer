from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint
from datetime import date
from decimal import Decimal
from pydantic import field_validator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .line_model import Line


class DailyArchiveBase(SQLModel):
    period: date = Field(index=True)
    volume: Decimal = Field(decimal_places=3)
    w_volume_dp: Decimal = Field(decimal_places=3)
    pressure: Decimal = Field(decimal_places=3)
    temperature: Decimal = Field(decimal_places=3)
    density: Decimal = Field(decimal_places=3)


DAILY_ARCHIVE_CONSTRAINT = ["line_id", "period", "volume"]


class DailyArchive(DailyArchiveBase, table=True):
    __tablename__ = "daily_archive"
    __table_args__ = (
        UniqueConstraint(
            *DAILY_ARCHIVE_CONSTRAINT, name="day_calc_id_line_period_constraint"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    line_id: int | None = Field(
        default=None, foreign_key="gas_volume_line.id", ondelete="CASCADE", index=True
    )
    line: "Line" = Relationship(back_populates="daily_archives")


class DailyArchiveList(DailyArchiveBase):
    id: int
    line_id: int

    @field_validator("density")
    def validate_density(cls, value: Decimal):
        if value > 1 or value < 0.5:
            value = Decimal(0)
        return value


class DailyArchiveCreate(DailyArchiveBase):
    line_id: int


if __name__ == "__main__":
    d_a = DailyArchiveCreate(
        line=1,
        period=date(2024, 1, 1),
        volume=0,
        w_volume_dp=0,
        pressure=0,
        temperature=0,
        density=2,
    )
    pass
