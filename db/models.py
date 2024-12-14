from datetime import datetime
from decimal import Decimal

from sqlmodel import Field, SQLModel, Relationship, UniqueConstraint, create_engine


class Lumg(SQLModel, table=True):
    __tablename__ = "lumg"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=255)
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(back_populates="lumg", cascade_delete=True)

class GasVolumeCalcType(SQLModel, table=True):
    __tablename__ = "gas_vol_calc_type"

    id: int | None = Field(default=None, primary_key=True)
    type_id: int
    type_name: str = Field(max_length=255)
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(back_populates="type", cascade_delete=True)

class GasVolumeCalc(SQLModel, table=True):
    __tablename__ = "gas_volume_calc"
    __table_args__ = (
        UniqueConstraint("lumg_id", "address", name="lumg_adress_constraint"),
    )

    id: int | None = Field(default=None, primary_key=True)
    lumg_id: int | None = Field(default=None, foreign_key="lumg.id", ondelete="CASCADE")
    lumg: Lumg = Relationship(back_populates="gas_volume_calcs")
    address: int
    meter: bool
    type_id: int | None = Field(default=None, foreign_key="gas_vol_calc_type.id")
    type: GasVolumeCalcType = Relationship(back_populates="gas_volume_calcs")
    name: str = Field(max_length=255)
    c_time: int

    daily_archives: list["DailyArchive"] = Relationship(back_populates="gas_volume_calc", cascade_delete=True)

class DailyArchive(SQLModel, table=True):
    __tablename__ = "daily_archive"
    __table_args__ = (UniqueConstraint("gas_vol_calc_id", "line", name="calc_id_line_constraint"), )

    id: int | None = Field(default=None, primary_key=True)
    gas_vol_calc_id: int | None = Field(default=None, foreign_key="gas_volume_calc.id", ondelete="CASCADE")
    gas_volume_calc: GasVolumeCalc = Relationship(back_populates="daily_archives")
    line: int
    period: datetime
    volume: Decimal = Field(max_digits=20, decimal_places=3)
    w_volume_dp: Decimal = Field(max_digits=20, decimal_places=3)
    pressure: Decimal = Field(max_digits=10, decimal_places=3)
    temperature: Decimal = Field(max_digits=10, decimal_places=3)
    density: Decimal = Field(max_digits=4, decimal_places=3)
