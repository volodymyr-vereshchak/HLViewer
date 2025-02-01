import struct
from dataclasses import asdict


class BaseDataclass:
    @classmethod
    def unpack(cls, data: bytes):
        """Create an object from binary data"""
        unpacked_data = struct.unpack(cls.format, data)
        return asdict(cls(*unpacked_data))
