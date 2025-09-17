"""
Basic test to verify the testing setup is working correctly.
"""

import pytest
from datetime import datetime


def test_basic_imports():
    """Test that basic imports work."""
    try:
        from backend.hl_engine.data_classes.hour_dataclass import HourStruct
        from backend.hl_engine.data_classes.day_dataclass import DayStruct
        from utils.files_utils import find_files_by_mask
        assert True
    except ImportError as e:
        pytest.fail(f"Import failed: {e}")


def test_datetime_operations():
    """Test basic datetime operations."""
    now = datetime.now()
    assert now.year > 2020
    assert now.month >= 1 and now.month <= 12
    assert now.day >= 1 and now.day <= 31


def test_pytest_working():
    """Test that pytest is working correctly."""
    assert True


@pytest.mark.asyncio
async def test_async_support():
    """Test that async/await support is working."""
    import asyncio
    await asyncio.sleep(0.001)  # Minimal async operation
    assert True


def test_mock_support():
    """Test that mocking is working."""
    from unittest.mock import Mock, AsyncMock
    
    # Test regular mock
    mock_obj = Mock()
    mock_obj.some_method.return_value = "test"
    assert mock_obj.some_method() == "test"
    
    # Test async mock
    async_mock = AsyncMock()
    assert hasattr(async_mock, 'await_args_list')


def test_fixture_support():
    """Test that fixtures are working."""
    # This test will use fixtures from conftest.py
    assert True


class TestBasicClass:
    """Test that test classes work correctly."""
    
    def test_class_method(self):
        """Test method within a test class."""
        assert True
    
    def test_another_method(self):
        """Test another method within the same class."""
        assert 2 + 2 == 4


@pytest.mark.unit
def test_unit_marker():
    """Test that unit marker works."""
    assert True


@pytest.mark.integration
def test_integration_marker():
    """Test that integration marker works."""
    assert True


@pytest.mark.slow
def test_slow_marker():
    """Test that slow marker works."""
    assert True


def test_environment_variables():
    """Test that environment variables can be accessed."""
    import os
    
    # Test that we can access environment variables
    # (even if they're not set, this should not fail)
    test_var = os.getenv("TEST_VAR", "default_value")
    assert test_var == "default_value"


def test_file_operations():
    """Test basic file operations."""
    import tempfile
    import os
    
    # Test creating and writing to a temporary file
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write("test content")
        temp_file = f.name
    
    try:
        # Verify file was created and contains content
        assert os.path.exists(temp_file)
        with open(temp_file, 'r') as f:
            content = f.read()
        assert content == "test content"
    finally:
        # Clean up
        os.unlink(temp_file)


def test_json_operations():
    """Test JSON operations."""
    import json
    
    # Test JSON serialization/deserialization
    test_data = {"key": "value", "number": 42, "list": [1, 2, 3]}
    json_str = json.dumps(test_data)
    parsed_data = json.loads(json_str)
    
    assert parsed_data == test_data
    assert parsed_data["key"] == "value"
    assert parsed_data["number"] == 42
    assert parsed_data["list"] == [1, 2, 3]


def test_math_operations():
    """Test basic math operations."""
    import math
    
    # Test various math operations
    assert math.sqrt(4) == 2.0
    assert math.pow(2, 3) == 8.0
    assert math.ceil(3.1) == 4
    assert math.floor(3.9) == 3


def test_string_operations():
    """Test string operations."""
    test_string = "Hello, World!"
    
    assert test_string.upper() == "HELLO, WORLD!"
    assert test_string.lower() == "hello, world!"
    assert test_string.split(", ") == ["Hello", "World!"]
    assert len(test_string) == 13


def test_list_operations():
    """Test list operations."""
    test_list = [1, 2, 3, 4, 5]
    
    assert len(test_list) == 5
    assert sum(test_list) == 15
    assert test_list[0] == 1
    assert test_list[-1] == 5
    
    # Test list comprehension
    doubled = [x * 2 for x in test_list]
    assert doubled == [2, 4, 6, 8, 10]


def test_dict_operations():
    """Test dictionary operations."""
    test_dict = {"a": 1, "b": 2, "c": 3}
    
    assert len(test_dict) == 3
    assert test_dict["a"] == 1
    assert "b" in test_dict
    assert "d" not in test_dict
    
    # Test dict comprehension
    squared = {k: v**2 for k, v in test_dict.items()}
    assert squared == {"a": 1, "b": 4, "c": 9}


def test_exception_handling():
    """Test exception handling."""
    # Test that exceptions are raised correctly
    with pytest.raises(ValueError):
        int("not_a_number")
    
    with pytest.raises(KeyError):
        empty_dict = {}
        _ = empty_dict["missing_key"]
    
    with pytest.raises(IndexError):
        empty_list = []
        _ = empty_list[0]


def test_assertions():
    """Test various assertion types."""
    # Test basic assertions
    assert True
    assert not False
    assert 1 == 1
    assert 1 != 2
    assert 1 < 2
    assert 2 > 1
    assert 1 <= 1
    assert 1 >= 1
    
    # Test membership assertions
    assert 1 in [1, 2, 3]
    assert 4 not in [1, 2, 3]
    
    # Test string assertions
    assert "hello" in "hello world"
    assert "goodbye" not in "hello world"


if __name__ == "__main__":
    # This allows running the file directly for debugging
    pytest.main([__file__, "-v"]) 