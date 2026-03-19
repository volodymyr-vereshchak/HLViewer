import struct
from dataclasses import asdict


class BaseDataclass:
    @classmethod
    def unpack(cls, data: bytes):
        """Create an object from binary data"""
        unpacked_data = struct.unpack(cls.format, data)
        return cls(*unpacked_data)

    @staticmethod
    def get_string_from_bytes(bytes_str: bytes) -> str:
        """Convert the header bytes to a string"""
        return bytes_str.decode("windows-1251", errors="ignore").strip("\x00")
