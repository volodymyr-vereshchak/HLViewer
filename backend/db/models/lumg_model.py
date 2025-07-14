from sqlmodel import Field, Relationship
from typing import TYPE_CHECKING
from sqlalchemy import Index

from .base_model import HlBaseModel

if TYPE_CHECKING:
    from .gas_volume_calc_model import GasVolumeCalc


class LumgBase(HlBaseModel):
    name: str = Field(max_length=255, unique=True)


class Lumg(LumgBase, table=True):
    __tablename__ = "lumg"
    __table_args__ = (
        # Индекс для поиска по имени
        Index("idx_lumg_name", "name"),
    )

    id: int | None = Field(default=None, primary_key=True)
    gas_volume_calcs: list["GasVolumeCalc"] = Relationship(
        back_populates="lumg", cascade_delete=True
    )


class LumgList(LumgBase):
    id: int


class LumgCreate(LumgBase):
    pass


class LumgUpdate(LumgBase):
    name: str | None = None
