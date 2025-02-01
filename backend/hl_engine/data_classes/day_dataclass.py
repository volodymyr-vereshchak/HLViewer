import struct
from dataclasses import dataclass

from backend.hl_engine.data_classes.base_dataclass import BaseDataclass


@dataclass
class DayStruct(BaseDataclass):
    month: int
    day: int
    year: int
    volume: float
    unknown: float
    w_volume_dp: float
    pressure: float
    temperature: float
    density: float

    format = "=3B6f"
    size = struct.calcsize(format)
