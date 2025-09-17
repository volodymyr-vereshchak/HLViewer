import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, date

from backend.hl_engine.hl_engine import Hostlib
from backend.hl_engine.data_classes.hour_dataclass import HourStruct
from backend.hl_engine.data_classes.day_dataclass import DayStruct
from backend.db.models import HourlyArchive, DailyArchive


class TestHostlib:
    """Test Hostlib engine functionality."""
    
    @pytest.fixture
    def mock_session(self):
        """Mock database session."""
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        return session
    
    @pytest.fixture
    def mock_gas_volume_dao(self):
        """Mock gas volume DAO."""
        dao = AsyncMock()
        dao.get_or_create = AsyncMock()
        return dao
    
    @pytest.fixture
    def mock_line_dao(self):
        """Mock line DAO."""
        dao = AsyncMock()
        dao.get_or_create = AsyncMock()
        return dao
    
    def test_hostlib_initialization(self, mock_session):
        """Test Hostlib initialization."""
        hostlib = Hostlib(
            session=mock_session,
            mask="*.bin",
            struct=HourStruct,
            create_class=HourlyArchive,
            path="./test_path",
            chunk_size=500,
            lumg_id=1,
            date_flag=True
        )
        
        assert hostlib.session == mock_session
        assert hostlib.mask == "*.bin"
        assert hostlib.struct == HourStruct
        assert hostlib.create_class == HourlyArchive
        assert hostlib.path == "./test_path"
        assert hostlib.chunk_size == 500
        assert hostlib.lumg_id == 1
        assert hostlib.date_flag is True
    
    def test_get_params_from_file_name_hourly(self):
        """Test extracting parameters from hourly file name."""
        file_path = "/path/to/0101_1_20241225.bin"
        params = Hostlib.get_params_from_file_name(file_path)
        
        assert params["address"] == 101
        assert params["line"] == 1
    
    def test_get_params_from_file_name_daily(self):
        """Test extracting parameters from daily file name."""
        file_path = "/path/to/0102_2_20241225.bin"
        params = Hostlib.get_params_from_file_name(file_path)
        
        assert params["address"] == 102
        assert params["line"] == 2
    
    def test_get_params_from_file_name_complex_path(self):
        """Test extracting parameters from complex file path."""
        file_path = "/very/complex/path/with/many/levels/0103_3_20241225.bin"
        params = Hostlib.get_params_from_file_name(file_path)
        
        assert params["address"] == 103
        assert params["line"] == 3
    
    @patch('backend.hl_engine.hl_engine.find_files_by_mask')
    @patch('backend.hl_engine.hl_engine.read_archive_file')
    @patch('backend.hl_engine.hl_engine.GasVolumeCalcDao')
    @patch('backend.hl_engine.hl_engine.LineDao')
    async def test_read_hourly_archives(
        self, 
        mock_line_dao_class, 
        mock_gas_volume_dao_class, 
        mock_read_archive, 
        mock_find_files, 
        mock_session
    ):
        """Test reading hourly archives."""
        # Setup mocks
        mock_find_files.return_value = ["/path/to/0101_1_20241225.bin"]
        
        mock_gas_volume_dao = AsyncMock()
        mock_gas_volume_dao.get_or_create.return_value = MagicMock(id=1)
        mock_gas_volume_dao_class.return_value = mock_gas_volume_dao
        
        mock_line_dao = AsyncMock()
        mock_line_dao.get_or_create.return_value = MagicMock(id=1)
        mock_line_dao_class.return_value = mock_line_dao
        
        # Mock archive data
        mock_archive_data = [
            {
                'month': 12, 'day': 25, 'year': 24, 'hour': 14, 'minutes': 30,
                'volume': 1000.5, 'unknown': 0.0, 'w_volume_dp': 0.1,
                'pressure': 5.2, 'temperature': 20.5, 'density': 0.7
            }
        ]
        mock_read_archive.return_value = iter(mock_archive_data)
        
        # Create Hostlib instance
        hostlib = Hostlib(
            session=mock_session,
            mask="*.bin",
            struct=HourStruct,
            create_class=HourlyArchive,
            path="./test_path",
            chunk_size=1,
            lumg_id=1,
            date_flag=True
        )
        
        # Test reading
        chunks = []
        async for chunk in hostlib.read():
            chunks.append(chunk)
        
        # Verify results
        assert len(chunks) == 1
        assert len(chunks[0]) == 1
        
        record = chunks[0][0]
        assert record['period'] == date(2024, 12, 25)
        assert record['line_id'] == 1
        assert abs(record['volume'] - 1000.5) < 1e-6
        assert abs(record['pressure'] - 5.2) < 1e-6
    
    @patch('backend.hl_engine.hl_engine.find_files_by_mask')
    @patch('backend.hl_engine.hl_engine.read_archive_file')
    @patch('backend.hl_engine.hl_engine.GasVolumeCalcDao')
    @patch('backend.hl_engine.hl_engine.LineDao')
    async def test_read_daily_archives(
        self, 
        mock_line_dao_class, 
        mock_gas_volume_dao_class, 
        mock_read_archive, 
        mock_find_files, 
        mock_session
    ):
        """Test reading daily archives."""
        # Setup mocks
        mock_find_files.return_value = ["/path/to/0101_1_20241225.bin"]
        
        mock_gas_volume_dao = AsyncMock()
        mock_gas_volume_dao.get_or_create.return_value = MagicMock(id=1)
        mock_gas_volume_dao_class.return_value = mock_gas_volume_dao
        
        mock_line_dao = AsyncMock()
        mock_line_dao.get_or_create.return_value = MagicMock(id=1)
        mock_line_dao_class.return_value = mock_line_dao
        
        # Mock archive data
        mock_archive_data = [
            {
                'month': 12, 'day': 25, 'year': 24,
                'volume': 24000.0, 'unknown': 0.0, 'w_volume_dp': 2.4,
                'pressure': 5.2, 'temperature': 20.5, 'density': 0.7
            }
        ]
        mock_read_archive.return_value = iter(mock_archive_data)
        
        # Create Hostlib instance for daily archives
        hostlib = Hostlib(
            session=mock_session,
            mask="*.bin",
            struct=DayStruct,
            create_class=DailyArchive,
            path="./test_path",
            chunk_size=1,
            lumg_id=1,
            date_flag=True
        )
        
        # Test reading
        chunks = []
        async for chunk in hostlib.read():
            chunks.append(chunk)
        
        # Verify results
        assert len(chunks) == 1
        assert len(chunks[0]) == 1
        
        record = chunks[0][0]
        assert record['period'] == date(2024, 12, 25)
        assert record['line_id'] == 1
        assert abs(record['volume'] - 24000.0) < 1e-6
        assert abs(record['pressure'] - 5.2) < 1e-6
    
    @patch('backend.hl_engine.hl_engine.find_files_by_mask')
    @patch('backend.hl_engine.hl_engine.read_archive_file')
    @patch('backend.hl_engine.hl_engine.GasVolumeCalcDao')
    @patch('backend.hl_engine.hl_engine.LineDao')
    async def test_read_with_datetime_flag(
        self, 
        mock_line_dao_class, 
        mock_gas_volume_dao_class, 
        mock_read_archive, 
        mock_find_files, 
        mock_session
    ):
        """Test reading with datetime flag (not date flag)."""
        # Setup mocks
        mock_find_files.return_value = ["/path/to/0101_1_20241225.bin"]
        
        mock_gas_volume_dao = AsyncMock()
        mock_gas_volume_dao.get_or_create.return_value = MagicMock(id=1)
        mock_gas_volume_dao_class.return_value = mock_gas_volume_dao
        
        mock_line_dao = AsyncMock()
        mock_line_dao.get_or_create.return_value = MagicMock(id=1)
        mock_line_dao_class.return_value = mock_line_dao
        
        # Mock archive data
        mock_archive_data = [
            {
                'month': 12, 'day': 25, 'year': 24, 'hour': 14, 'minutes': 30,
                'volume': 1000.5, 'unknown': 0.0, 'w_volume_dp': 0.1,
                'pressure': 5.2, 'temperature': 20.5, 'density': 0.7
            }
        ]
        mock_read_archive.return_value = iter(mock_archive_data)
        
        # Create Hostlib instance with date_flag=False
        hostlib = Hostlib(
            session=mock_session,
            mask="*.bin",
            struct=HourStruct,
            create_class=HourlyArchive,
            path="./test_path",
            chunk_size=1,
            lumg_id=1,
            date_flag=False
        )
        
        # Test reading
        chunks = []
        async for chunk in hostlib.read():
            chunks.append(chunk)
        
        # Verify results
        assert len(chunks) == 1
        assert len(chunks[0]) == 1
        
        record = chunks[0][0]
        expected_datetime = datetime(2024, 12, 25, 14, 30)
        assert record['period'] == expected_datetime
        assert record['line_id'] == 1
    
    @patch('backend.hl_engine.hl_engine.find_files_by_mask')
    async def test_read_no_files_found(self, mock_find_files, mock_session):
        """Test reading when no files are found."""
        # Setup mocks
        mock_find_files.return_value = []
        
        # Create Hostlib instance
        hostlib = Hostlib(
            session=mock_session,
            mask="*.bin",
            struct=HourStruct,
            create_class=HourlyArchive,
            path="./test_path",
            chunk_size=1,
            lumg_id=1,
            date_flag=True
        )
        
        # Test reading
        chunks = []
        async for chunk in hostlib.read():
            chunks.append(chunk)
        
        # Verify no chunks are returned
        assert len(chunks) == 0
    
    @patch('backend.hl_engine.hl_engine.find_files_by_mask')
    @patch('backend.hl_engine.hl_engine.read_archive_file')
    @patch('backend.hl_engine.hl_engine.GasVolumeCalcDao')
    @patch('backend.hl_engine.hl_engine.LineDao')
    async def test_read_chunk_size_handling(
        self, 
        mock_line_dao_class, 
        mock_gas_volume_dao_class, 
        mock_read_archive, 
        mock_find_files, 
        mock_session
    ):
        """Test handling of chunk size."""
        # Setup mocks
        mock_find_files.return_value = ["/path/to/0101_1_20241225.bin"]
        
        mock_gas_volume_dao = AsyncMock()
        mock_gas_volume_dao.get_or_create.return_value = MagicMock(id=1)
        mock_gas_volume_dao_class.return_value = mock_gas_volume_dao
        
        mock_line_dao = AsyncMock()
        mock_line_dao.get_or_create.return_value = MagicMock(id=1)
        mock_line_dao_class.return_value = mock_line_dao
        
        # Mock archive data - 5 records
        mock_archive_data = []
        for i in range(5):
            mock_archive_data.append({
                'month': 12, 'day': 25, 'year': 24, 'hour': i, 'minutes': 30,
                'volume': 1000.0 + i, 'unknown': 0.0, 'w_volume_dp': 0.1,
                'pressure': 5.2, 'temperature': 20.5, 'density': 0.7
            })
        mock_read_archive.return_value = iter(mock_archive_data)
        
        # Create Hostlib instance with chunk_size=2
        hostlib = Hostlib(
            session=mock_session,
            mask="*.bin",
            struct=HourStruct,
            create_class=HourlyArchive,
            path="./test_path",
            chunk_size=2,
            lumg_id=1,
            date_flag=True
        )
        
        # Test reading
        chunks = []
        async for chunk in hostlib.read():
            chunks.append(chunk)
        
        # Verify results - should have 3 chunks: 2, 2, 1
        assert len(chunks) == 3
        assert len(chunks[0]) == 2
        assert len(chunks[1]) == 2
        assert len(chunks[2]) == 1
    
    @patch('backend.hl_engine.hl_engine.find_files_by_mask')
    @patch('backend.hl_engine.hl_engine.read_archive_file')
    @patch('backend.hl_engine.hl_engine.GasVolumeCalcDao')
    @patch('backend.hl_engine.hl_engine.LineDao')
    async def test_read_with_value_error_handling(
        self, 
        mock_line_dao_class, 
        mock_gas_volume_dao_class, 
        mock_read_archive, 
        mock_find_files, 
        mock_session
    ):
        """Test handling of ValueError during reading."""
        # Setup mocks
        mock_find_files.return_value = ["/path/to/0101_1_20241225.bin"]
        
        mock_gas_volume_dao = AsyncMock()
        mock_gas_volume_dao.get_or_create.return_value = MagicMock(id=1)
        mock_gas_volume_dao_class.return_value = mock_gas_volume_dao
        
        mock_line_dao = AsyncMock()
        mock_line_dao.get_or_create.return_value = MagicMock(id=1)
        mock_line_dao_class.return_value = mock_line_dao
        
        # Mock archive data with some invalid records
        mock_archive_data = [
            {
                'month': 12, 'day': 25, 'year': 24, 'hour': 14, 'minutes': 30,
                'volume': 1000.5, 'unknown': 0.0, 'w_volume_dp': 0.1,
                'pressure': 5.2, 'temperature': 20.5, 'density': 0.7
            }
        ]
        
        # Create generator that raises ValueError for some records
        def mock_generator():
            for i, record in enumerate(mock_archive_data):
                if i == 0:
                    yield record
                else:
                    raise ValueError("Invalid data")
        
        mock_read_archive.return_value = mock_generator()
        
        # Create Hostlib instance
        hostlib = Hostlib(
            session=mock_session,
            mask="*.bin",
            struct=HourStruct,
            create_class=HourlyArchive,
            path="./test_path",
            chunk_size=1,
            lumg_id=1,
            date_flag=True
        )
        
        # Test reading - should handle ValueError gracefully
        chunks = []
        async for chunk in hostlib.read():
            chunks.append(chunk)
        
        # Should still process valid records
        assert len(chunks) == 1
        assert len(chunks[0]) == 1 