"""
Device catalog: manufacturers and corrector models (previously hardcoded in enterprise_mappings.py).
"""
from typing import Optional
from sqlalchemy import BigInteger
from sqlmodel import SQLModel, Field


class Manufacturer(SQLModel, table=True):
    """Завод-виробник коректора. mf_dev — код у системі DPD."""
    __tablename__ = "manufacturer"

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    short_name: str = Field(index=True)   # display name: РадмирТех, Укргазтех…
    full_name: str                          # DPD name: "РадмирТех ТОВ СП, м. Харків"
    mf_dev: int = Field(index=True)        # DPD manufacturer code


class ManufacturerRead(SQLModel):
    id: int
    short_name: str
    full_name: str
    mf_dev: int


class ManufacturerCreate(SQLModel):
    short_name: str
    full_name: str
    mf_dev: int


class ManufacturerUpdate(SQLModel):
    short_name: Optional[str] = None
    full_name: Optional[str] = None
    mf_dev: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────

class CorectorType(SQLModel, table=True):
    """Тип/модель коректора. type_dev — код у системі DPD."""
    __tablename__ = "corector_type"

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    manufacturer_id: int = Field(
        foreign_key="manufacturer.id", index=True, sa_type=BigInteger,
    )
    model_name: str = Field(index=True)    # e.g. "ВЕГА-1.01"
    type_dev: int                           # DPD device type code


class CorectorTypeRead(SQLModel):
    id: int
    manufacturer_id: int
    model_name: str
    type_dev: int


class CorectorTypeCreate(SQLModel):
    manufacturer_id: int
    model_name: str
    type_dev: int


class CorectorTypeUpdate(SQLModel):
    manufacturer_id: Optional[int] = None
    model_name: Optional[str] = None
    type_dev: Optional[int] = None
