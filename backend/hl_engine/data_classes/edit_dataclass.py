import struct
from dataclasses import dataclass

from backend.hl_engine.data_classes.base_dataclass import BaseDataclass


@dataclass
class EditStruct(BaseDataclass):
    month: int
    day: int
    year: int
    hour: int
    minutes: int
    seconds: int
    edit_type_id: int
    unknown: int
    old_value: int
    new_value: float

    format = "=8B2i"
    size = struct.calcsize(format)
