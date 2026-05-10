import struct
from dataclasses import dataclass

from backend.hl_engine.data_classes.base_dataclass import BaseDataclass


@dataclass
class HeaderStruct(BaseDataclass):
    header: bytes
    unk: bytes

    format = "=6s42s"
    size = struct.calcsize(format)

    @staticmethod
    def get_string_from_bytes(bytes_str: bytes) -> str:
        return bytes_str.decode("ascii").strip("\x00")


@dataclass
class GroupStruct(BaseDataclass):
    name_length: int
    name: bytes
    zero_byte: int
    gis_num: int
    zero_byte1: int

    format = "=B32sBBB"
    size = struct.calcsize(format)


@dataclass
class GisStruct(BaseDataclass):
    unk: bytes
    gis_name_length: int
    gis_name: bytes
    zero_byte: int
    flow_num: int
    zero_byte1: int

    format = "=2sB32sBBB"
    size = struct.calcsize(format)


@dataclass
class FlowStruct(BaseDataclass):
    unk: bytes
    flow_name_length: int
    flow_name: bytes
    zero_byte: int
    line_num: int
    zero_byte1: int

    format = "=2sB32sBBB"
    size = struct.calcsize(format)


@dataclass
class LineStruct(BaseDataclass):
    unk: bytes
    line_name_length: int
    line_name: bytes
    zero_byte: int
    line_num: int
    unk1: bytes
    meter_type: int
    unk2: bytes

    format = "=2sB32sBB124sB3s"
    size = struct.calcsize(format)


@dataclass
class GasVolumeCalcStruct(BaseDataclass):
    flow_address: int
    zero_byte: int
    c_time: int
    flow_id: int
    unk: bytes

    format = "=BBBB234s"
    size = struct.calcsize(format)
