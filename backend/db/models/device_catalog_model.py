"""
Device catalog: manufacturers and corrector models (previously hardcoded in enterprise_mappings.py).
"""
import uuid
from typing import Optional
from sqlalchemy import BigInteger, Uuid
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
        UniqueConstraint("export_uid", name="uq_manufacturer_export_uid"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    # Identity of the entry ACROSS installations: minted once, written into
    # device_catalog.json and into config bundles, never shown or edited.
    # Neither the name nor the code can do this job. A name gets corrected, and
    # then arrives elsewhere as a second entry; a code is missing altogether
    # for what ДПД does not know, and a model's code is shared by several
    # models on purpose. `id` is local to one database.
    export_uid: uuid.UUID = Field(
        default_factory=uuid.uuid4, sa_type=Uuid, nullable=False,
    )
    short_name: str = Field(index=True)   # display name: РадмирТех, Укргазтех…
    full_name: str                          # DPD name: "РадмирТех ТОВ СП, м. Харків"
    # DPD manufacturer code. NULL for a manufacturer ДПД does not know at all
    # — Изодром: its flow computers are read over a modem and nowhere else.
    # Devices of such a model are simply never asked from ДПД; a made-up code
    # would be sent there and come back as a meter that does not answer.
    mf_dev: Optional[int] = Field(default=None, index=True)


class ManufacturerRead(SQLModel):
    id: int
    short_name: str
    full_name: str
    mf_dev: Optional[int] = None


class ManufacturerCreate(SQLModel):
    short_name: str
    full_name: str
    mf_dev: Optional[int] = None


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
        UniqueConstraint("export_uid", name="uq_corector_type_export_uid"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    # Cross-installation identity — see Manufacturer.export_uid.
    export_uid: uuid.UUID = Field(
        default_factory=uuid.uuid4, sa_type=Uuid, nullable=False,
    )
    # The FK is ON DELETE CASCADE in the database; without saying so here,
    # autogenerate proposed replacing it with a plain one on every run.
    manufacturer_id: int = Field(
        foreign_key="manufacturer.id", ondelete="CASCADE", index=True,
        sa_type=BigInteger,
    )
    model_name: str = Field(index=True)    # e.g. "ВЕГА-1.01"
    # DPD device type code; NULL where ДПД has no such model (see mf_dev).
    type_dev: Optional[int] = Field(default=None)
    # Which Ask2 driver can talk to this model over a modem: 7 Універсал,
    # 33 Флоутек ВР-1, 52 КПЛГ, 54 ВЕГА, 70 Флоутек ВР-2, 77 ПК-В.
    #
    # Here rather than on the poll card, because it is a property of the model
    # and asking an operator to retype it per device is asking for a typo that
    # looks like a dead meter. NULL means no driver is known — and for a large
    # part of this fleet that is the truth rather than an omission: ТКБ,
    # smart104 and ТАНДЕМ appear in none of the Ask2 driver assemblies, so
    # those models cannot be polled this way at all.
    protocol_id: Optional[int] = Field(default=None)


class CorectorTypeRead(SQLModel):
    id: int
    manufacturer_id: int
    model_name: str
    type_dev: Optional[int] = None
    protocol_id: Optional[int] = None


class CorectorTypeCreate(SQLModel):
    manufacturer_id: int
    model_name: str
    type_dev: Optional[int] = None
    protocol_id: Optional[int] = None


class CorectorTypeUpdate(SQLModel):
    manufacturer_id: Optional[int] = None
    model_name: Optional[str] = None
    type_dev: Optional[int] = None
    protocol_id: Optional[int] = None
