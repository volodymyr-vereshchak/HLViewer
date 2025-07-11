import struct
import pytest
from datetime import datetime

from backend.hl_engine.data_classes.hour_dataclass import HourStruct
from backend.hl_engine.data_classes.day_dataclass import DayStruct
from backend.hl_engine.data_classes.base_dataclass import BaseDataclass


class TestBaseDataclass:
    """Test base dataclass functionality."""
    
    def test_get_string_from_bytes(self):
        """Test converting bytes to string."""
        # Test with Windows-1251 encoded string
        test_bytes = b'Test String\x00\x00\x00'
        result = BaseDataclass.get_string_from_bytes(test_bytes)
        assert result == "Test String"
    
    def test_get_string_from_bytes_with_null_padding(self):
        """Test string conversion with null padding."""
        test_bytes = b'Hello\x00\x00\x00\x00'
        result = BaseDataclass.get_string_from_bytes(test_bytes)
        assert result == "Hello"


class TestHourStruct:
    """Test hourly archive data structure."""
    
    def test_hour_struct_format(self):
        """Test that the format string is correct."""
        expected_format = "=5B6f"
        assert HourStruct.format == expected_format
    
    def test_hour_struct_size(self):
        """Test that the size calculation is correct."""
        expected_size = struct.calcsize("=5B6f")
        assert HourStruct.size == expected_size
    
    def test_hour_struct_unpack(self):
        """Test unpacking binary data into HourStruct."""
        # Create sample binary data
        test_data = struct.pack("=5B6f", 12, 25, 24, 14, 30, 1000.5, 0.0, 0.1, 5.2, 20.5, 0.7)
        
        # Unpack the data
        result = HourStruct.unpack(test_data)
        
        # Verify the unpacked data
        assert result.month == 12
        assert result.day == 25
        assert result.year == 24
        assert result.hour == 14
        assert result.minutes == 30
        assert abs(result.volume - 1000.5) < 1e-6
        assert abs(result.unknown - 0.0) < 1e-6
        assert abs(result.w_volume_dp - 0.1) < 1e-6
        assert abs(result.pressure - 5.2) < 1e-6
        assert abs(result.temperature - 20.5) < 1e-6
        assert abs(result.density - 0.7) < 1e-6
    
    def test_hour_struct_asdict(self):
        """Test converting HourStruct to dictionary."""
        test_data = struct.pack("=5B6f", 12, 25, 24, 14, 30, 1000.5, 0.0, 0.1, 5.2, 20.5, 0.7)
        result = HourStruct.unpack(test_data)
        
        # Convert to dictionary
        result_dict = result.__dict__
        
        expected_keys = {
            'month', 'day', 'year', 'hour', 'minutes', 
            'volume', 'unknown', 'w_volume_dp', 'pressure', 
            'temperature', 'density'
        }
        assert set(result_dict.keys()) == expected_keys
    
    def test_hour_struct_invalid_data(self):
        """Test handling of invalid binary data."""
        # Test with data that's too short
        invalid_data = b'invalid'
        
        with pytest.raises(struct.error):
            HourStruct.unpack(invalid_data)


class TestDayStruct:
    """Test daily archive data structure."""
    
    def test_day_struct_format(self):
        """Test that the format string is correct."""
        expected_format = "=3B6f"
        assert DayStruct.format == expected_format
    
    def test_day_struct_size(self):
        """Test that the size calculation is correct."""
        expected_size = struct.calcsize("=3B6f")
        assert DayStruct.size == expected_size
    
    def test_day_struct_unpack(self):
        """Test unpacking binary data into DayStruct."""
        # Create sample binary data
        test_data = struct.pack("=3B6f", 12, 25, 24, 24000.0, 0.0, 2.4, 5.2, 20.5, 0.7)
        
        # Unpack the data
        result = DayStruct.unpack(test_data)
        
        # Verify the unpacked data
        assert result.month == 12
        assert result.day == 25
        assert result.year == 24
        assert abs(result.volume - 24000.0) < 1e-6
        assert abs(result.unknown - 0.0) < 1e-6
        assert abs(result.w_volume_dp - 2.4) < 1e-6
        assert abs(result.pressure - 5.2) < 1e-6
        assert abs(result.temperature - 20.5) < 1e-6
        assert abs(result.density - 0.7) < 1e-6
    
    def test_day_struct_asdict(self):
        """Test converting DayStruct to dictionary."""
        test_data = struct.pack("=3B6f", 12, 25, 24, 24000.0, 0.0, 2.4, 5.2, 20.5, 0.7)
        result = DayStruct.unpack(test_data)
        
        # Convert to dictionary
        result_dict = result.__dict__
        
        expected_keys = {
            'month', 'day', 'year', 'volume', 'unknown', 
            'w_volume_dp', 'pressure', 'temperature', 'density'
        }
        assert set(result_dict.keys()) == expected_keys
    
    def test_day_struct_invalid_data(self):
        """Test handling of invalid binary data."""
        # Test with data that's too short
        invalid_data = b'invalid'
        
        with pytest.raises(struct.error):
            DayStruct.unpack(invalid_data)


class TestDataStructureComparison:
    """Test comparison between different data structures."""
    
    def test_hour_vs_day_size_difference(self):
        """Test that hour structure is larger than day structure."""
        assert HourStruct.size > DayStruct.size
    
    def test_hour_has_time_fields(self):
        """Test that hour structure has time-related fields."""
        test_data = struct.pack("=5B6f", 12, 25, 24, 14, 30, 1000.5, 0.0, 0.1, 5.2, 20.5, 0.7)
        result = HourStruct.unpack(test_data)
        
        # Hour structure should have hour and minutes
        assert hasattr(result, 'hour')
        assert hasattr(result, 'minutes')
    
    def test_day_has_no_time_fields(self):
        """Test that day structure doesn't have time-related fields."""
        test_data = struct.pack("=3B6f", 12, 25, 24, 24000.0, 0.0, 2.4, 5.2, 20.5, 0.7)
        result = DayStruct.unpack(test_data)
        
        # Day structure should not have hour and minutes
        assert not hasattr(result, 'hour')
        assert not hasattr(result, 'minutes') 