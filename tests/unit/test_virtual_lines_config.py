"""
Unit tests for virtual_lines_config module.
"""

import os
import json
import pytest
from unittest.mock import patch, mock_open
from datetime import datetime, timedelta

from backend.services import virtual_lines_config


# Sample test configurations
VALID_CONFIG = {
    "virtual_lines": {
        "1001": {
            "name": "Test Ring North",
            "physical_line_ids": [1, 6, 22],
            "active": True,
            "description": "Northern test ring"
        },
        "1002": {
            "name": "Test Ring South",
            "physical_line_ids": [4, 10, 15],
            "active": True,
            "description": "Southern test ring"
        },
        "1003": {
            "name": "Inactive Ring",
            "physical_line_ids": [7, 8],
            "active": False,
            "description": "Inactive test ring"
        }
    }
}

INVALID_CONFIG_LOW_ID = {
    "virtual_lines": {
        "999": {
            "name": "Invalid Ring",
            "physical_line_ids": [1, 2],
            "active": True
        }
    }
}

INVALID_CONFIG_MISSING_FIELD = {
    "virtual_lines": {
        "1001": {
            "physical_line_ids": [1, 2],
            "active": True
        }
    }
}

INVALID_CONFIG_EMPTY_LINES = {
    "virtual_lines": {
        "1001": {
            "name": "Empty Ring",
            "physical_line_ids": [],
            "active": True
        }
    }
}

DUPLICATE_PHYSICAL_CONFIG = {
    "virtual_lines": {
        "1001": {
            "name": "Ring 1",
            "physical_line_ids": [1, 6, 22],
            "active": True
        },
        "1002": {
            "name": "Ring 2",
            "physical_line_ids": [4, 6, 15],  # 6 is duplicate
            "active": True
        }
    }
}


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset cache before each test."""
    virtual_lines_config._config_cache = {
        "data": None,
        "loaded_at": None,
        "file_mtime": None
    }
    yield


class TestLoadVirtualLines:
    """Tests for load_virtual_lines function."""

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_load_valid_config(self, mock_file, mock_mtime, mock_exists):
        """Test loading valid configuration."""
        result = virtual_lines_config.load_virtual_lines()

        assert result is not None
        assert len(result) == 3
        assert "1001" in result
        assert "1002" in result
        assert "1003" in result
        assert result["1001"]["name"] == "Test Ring North"
        assert result["1001"]["physical_line_ids"] == [1, 6, 22]

    @patch("os.path.exists", return_value=False)
    def test_load_missing_file(self, mock_exists):
        """Test loading when config file doesn't exist."""
        result = virtual_lines_config.load_virtual_lines()

        assert result == {}

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(INVALID_CONFIG_LOW_ID))
    def test_load_invalid_id_below_1000(self, mock_file, mock_mtime, mock_exists):
        """Test loading config with virtual line ID < 1000."""
        with pytest.raises(ValueError, match="must be >= 1000"):
            virtual_lines_config.load_virtual_lines()

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(INVALID_CONFIG_MISSING_FIELD))
    def test_load_missing_required_field(self, mock_file, mock_mtime, mock_exists):
        """Test loading config with missing required field."""
        with pytest.raises(ValueError, match="missing 'name' field"):
            virtual_lines_config.load_virtual_lines()

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(INVALID_CONFIG_EMPTY_LINES))
    def test_load_empty_physical_lines(self, mock_file, mock_mtime, mock_exists):
        """Test loading config with empty physical_line_ids."""
        with pytest.raises(ValueError, match="cannot be empty"):
            virtual_lines_config.load_virtual_lines()

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_caching(self, mock_file, mock_mtime, mock_exists):
        """Test that configuration is cached."""
        # First call
        result1 = virtual_lines_config.load_virtual_lines()

        # Second call (should use cache)
        result2 = virtual_lines_config.load_virtual_lines()

        # File should only be opened once
        assert mock_file.call_count == 1
        assert result1 == result2

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_force_reload(self, mock_file, mock_mtime, mock_exists):
        """Test force reload ignores cache."""
        # First call
        virtual_lines_config.load_virtual_lines()

        # Force reload
        virtual_lines_config.load_virtual_lines(force_reload=True)

        # File should be opened twice
        assert mock_file.call_count == 2


class TestGetActiveVirtualLines:
    """Tests for get_active_virtual_lines function."""

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_get_active_only(self, mock_file, mock_mtime, mock_exists):
        """Test getting only active virtual lines."""
        result = virtual_lines_config.get_active_virtual_lines()

        assert len(result) == 2
        assert "1001" in result
        assert "1002" in result
        assert "1003" not in result  # Inactive


class TestResolveVirtualToPhysical:
    """Tests for resolve_virtual_to_physical function."""

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_resolve_virtual_lines(self, mock_file, mock_mtime, mock_exists):
        """Test resolving virtual line IDs to physical."""
        result = virtual_lines_config.resolve_virtual_to_physical([1001, 1002])

        assert set(result) == {1, 6, 22, 4, 10, 15}

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_resolve_mixed_lines(self, mock_file, mock_mtime, mock_exists):
        """Test resolving mix of virtual and physical line IDs."""
        result = virtual_lines_config.resolve_virtual_to_physical([1001, 25, 30])

        # 1001 -> [1, 6, 22], 25 and 30 stay as-is
        assert set(result) == {1, 6, 22, 25, 30}

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_resolve_physical_only(self, mock_file, mock_mtime, mock_exists):
        """Test resolving only physical line IDs."""
        result = virtual_lines_config.resolve_virtual_to_physical([1, 2, 3])

        assert result == [1, 2, 3]

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_resolve_inactive_virtual_line(self, mock_file, mock_mtime, mock_exists):
        """Test resolving inactive virtual line (should not expand)."""
        result = virtual_lines_config.resolve_virtual_to_physical([1003])

        # 1003 is inactive, so it won't be expanded
        assert result == []


class TestGetPhysicalLinesInRings:
    """Tests for get_physical_lines_in_rings function."""

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_get_physical_lines_in_active_rings(self, mock_file, mock_mtime, mock_exists):
        """Test getting physical lines that are in active rings."""
        result = virtual_lines_config.get_physical_lines_in_rings()

        # Only active rings (1001, 1002), not 1003
        assert result == {1, 6, 22, 4, 10, 15}


class TestValidateConfig:
    """Tests for validate_config function."""

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(VALID_CONFIG))
    def test_validate_valid_config(self, mock_file, mock_mtime, mock_exists):
        """Test validating valid configuration."""
        result = virtual_lines_config.validate_config()

        assert result["valid"] is True
        assert result["virtual_lines_count"] == 3
        assert result["active_count"] == 2
        assert set(result["hidden_physical_lines"]) == {1, 6, 22, 4, 10, 15}
        assert len(result["errors"]) == 0

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", return_value=1234567890)
    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps(DUPLICATE_PHYSICAL_CONFIG))
    def test_validate_duplicate_physical_lines(self, mock_file, mock_mtime, mock_exists):
        """Test validating config with duplicate physical lines."""
        result = virtual_lines_config.validate_config()

        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert "Physical line 6 is used in multiple" in result["errors"][0]

    @patch("os.path.exists", return_value=False)
    def test_validate_missing_file(self, mock_exists):
        """Test validating when file is missing."""
        result = virtual_lines_config.validate_config()

        assert result["valid"] is True  # Empty config is valid
        assert result["virtual_lines_count"] == 0
