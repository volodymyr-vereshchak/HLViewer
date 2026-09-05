"""
Device catalog: manufacturers and corrector models (previously hardcoded in enterprise_mappings.py).
"""
from typing import Optional
from sqlalchemy import BigInteger
from sqlmodel import SQLModel, Field, UniqueConstraint


class Manufacturer(SQLModel, table=True):
    """Завод-виробник коректора. mf_dev — код у системі DPD."""
    __tablename__ = "manufacturer"
    # Names match the constraints the migration created — the endpoint reports
    # a conflict by looking for them in the error text, and alembic must not
    # see these as new. Declared here so a schema built from the models (which
    # is what the tests do) also enforces them: a second manufacturer with the
    # same mf_dev would make device addressing ambiguous, since (mf_dev,
    # type_dev) is exactly what DPD is asked by.
    __table_args__ = (
        UniqueConstraint("short_name", name="uq_manufacturer_short_name"),
        UniqueConstraint("full_name", name="uq_manufacturer_full_name"),
        UniqueConstraint("mf_dev", name="uq_manufacturer_mf_dev"),
    )

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
    __table_args__ = (
        UniqueConstraint(
            "manufacturer_id", "model_name", name="uq_corector_type_mfr_model"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    # The FK is ON DELETE CASCADE in the database; without saying so here,
    # autogenerate proposed replacing it with a plain one on every run.
    manufacturer_id: int = Field(
        foreign_key="manufacturer.id", ondelete="CASCADE", index=True,
        sa_type=BigInteger,
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
