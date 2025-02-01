import struct
from dataclasses import dataclass

from backend.hl_engine.data_classes.base_dataclass import BaseDataclass


@dataclass
class HourStruct(BaseDataclass):
    month: int
    day: int
    year: int
    hour: int
    minutes: int
    volume: float
    unknown: float
    w_volume_dp: float
    pressure: float
    temperature: float
    density: float

    format = "=5B6f"
    size = struct.calcsize(format)
