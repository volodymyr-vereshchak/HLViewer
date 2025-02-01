import struct
from dataclasses import dataclass

from backend.hl_engine.data_classes.base_dataclass import BaseDataclass


@dataclass
class SysStruct(BaseDataclass):
    month: int
    day: int
    year: int
    hour: int
    minutes: int
    seconds: int
    sys_type_id: int
    unknown: int
    standard_volume: float

    format = "=6BHBf"
    size = struct.calcsize(format)
