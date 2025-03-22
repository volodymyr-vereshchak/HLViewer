import struct
from dataclasses import dataclass

from backend.hl_engine.data_classes.base_dataclass import BaseDataclass


@dataclass
class ParamStruct(BaseDataclass):
    name: str
    density: float
    co2: float
    n2: float
    D20: float
    d20: float
    unknown: float
    cutoff: float
    unknown1: float
    unknown2: float
    roughness: float
    max_dp: float
    min_dp: float
    unknown3: str
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
    month: int
    day: int
    year: int
    hour: int
    minutes: int
    seconds: int

    format = "=16B12f5B12f6I"
    size = struct.calcsize(format)
