from datetime import datetime, date
from decimal import Decimal

from pydantic.v1 import validator
from sqlmodel import SQLModel
from sqlmodel import Field, Relationship, UniqueConstraint

#################LUMG#################
class LumgBase(SQLModel):
    name: str = Field(max_length=255)


class Lumg(LumgBase, table=True):
    __tablename__ = "lumg"

    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(back_populates="lumg", cascade_delete=True)


class LumgList(LumgBase):
    id: int


class LumgCreate(LumgBase):
    pass

#################GasVolumeCalcType#################
class GasVolumeCalcTypeBase(SQLModel):
    type_id: int
    type_name: str = Field(max_length=255)


class GasVolumeCalcType(GasVolumeCalcTypeBase, table=True):
    __tablename__ = "gas_vol_calc_type"

    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(back_populates="type", cascade_delete=True)


class GasVolumeCalcTypeList(GasVolumeCalcTypeBase):
    id: int


class GasVolumeCalcTypeCreate(GasVolumeCalcTypeBase):
    pass

#################GasVolumeCalc#################
class GasVolumeCalcBase(SQLModel):
    address: int
    meter: bool
    name: str = Field(max_length=255)
    c_time: int


GAS_VOLUME_CALC_CONSTRAINT = ["lumg_id", "address"]
class GasVolumeCalc(GasVolumeCalcBase, table=True):
    __tablename__ = "gas_volume_calc"
    __table_args__ = (
        UniqueConstraint(*GAS_VOLUME_CALC_CONSTRAINT, name="lumg_adress_constraint"),
    )

    id: int | None = Field(default=None, primary_key=True)
    lumg_id: int | None = Field(default=None, foreign_key="lumg.id", ondelete="CASCADE")
    lumg: Lumg = Relationship(back_populates="gas_volume_calcs")
    type_id: int | None = Field(default=None, foreign_key="gas_vol_calc_type.id")
    type: GasVolumeCalcType = Relationship(back_populates="gas_volume_calcs")
    daily_archives: list["DailyArchive"] = Relationship(back_populates="gas_volume_calc", cascade_delete=True)
    hourly_archives: list["HourlyArchive"] = Relationship(back_populates="gas_volume_calc", cascade_delete=True)


class GasVolumeCalcList(GasVolumeCalcBase):
    id: int
    lumg_id: int
    type_id: int


class GasVolumeCalcCreate(GasVolumeCalcBase):
    lumg_id: int
    type_id: int

#################DailyArchive#################
class DailyArchiveBase(SQLModel):
    line: int
    period: date = Field(index=True)
    volume: Decimal = Field(max_digits=20, decimal_places=3)
    w_volume_dp: Decimal = Field(max_digits=20, decimal_places=3)
    pressure: Decimal = Field(max_digits=20, decimal_places=3)
    temperature: Decimal = Field(max_digits=20, decimal_places=3)
    density: Decimal = Field(max_digits=20, decimal_places=3)

    @validator("density")
    def validate_density(cls, value: Decimal):
        if value > 1 or value < 0.5:
            value = 0
        return value


DAILY_ARCHIVE_CONSTRAINT = ["gas_vol_calc_id", "line", "period"]
class DailyArchive(DailyArchiveBase, table=True):
    __tablename__ = "daily_archive"
    __table_args__ = (UniqueConstraint(*DAILY_ARCHIVE_CONSTRAINT, name="calc_id_line_period_constraint"),)
    id: int | None = Field(default=None, primary_key=True)
    gas_vol_calc_id: int | None = Field(default=None, foreign_key="gas_volume_calc.id", ondelete="CASCADE")
    gas_volume_calc: GasVolumeCalc = Relationship(back_populates="daily_archives")


class DailyArchiveList(DailyArchiveBase):
    id: int
    gas_vol_calc_id: int


class DailyArchiveCreate(DailyArchiveBase):
    gas_vol_calc_id: int

#################HourlyArchive#################
class HourlyArchiveBase(SQLModel):
    line: int
    period: datetime = Field(index=True)
    volume: Decimal = Field(max_digits=20, decimal_places=3)
    w_volume_dp: Decimal = Field(max_digits=20, decimal_places=3)
    pressure: Decimal = Field(max_digits=20, decimal_places=3)
    temperature: Decimal = Field(max_digits=20, decimal_places=3)
    density: Decimal = Field(max_digits=40, decimal_places=3)

    @validator("density")
    def validate_density(cls, value: Decimal):
        if value > 1 or value < 0.5:
            value = 0
        return value


HOURLY_ARCHIVE_CONSTRAINT = ["gas_vol_calc_id", "line", "period"]
class HourlyArchive(HourlyArchiveBase, table=True):
    __tablename__ = "hourly_archive"
    __table_args__ = (UniqueConstraint(*HOURLY_ARCHIVE_CONSTRAINT, name="calc_id_line_period_constraint"),)
    id: int | None = Field(default=None, primary_key=True)
    gas_vol_calc_id: int | None = Field(default=None, foreign_key="gas_volume_calc.id", ondelete="CASCADE")
    gas_volume_calc: GasVolumeCalc = Relationship(back_populates="hourly_archives")


class HourlyArchiveList(HourlyArchiveBase):
    id: int
    gas_vol_calc_id: int


class HourlyArchiveCreate(HourlyArchiveBase):
    gas_vol_calc_id: int
