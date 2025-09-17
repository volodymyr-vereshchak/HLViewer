import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy.exc import IntegrityError

from backend.db.dao.basic_dao import BasicDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.line_dao import LineDao
from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.models import HourlyArchive, Line, GasVolumeCalc
from backend.db.dao.custom_exceptions import DatabaseIntegrityError


class TestBasicDao:
    """Test basic DAO functionality."""
    
    @pytest.fixture
    def mock_session(self):
        """Mock database session."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.add = AsyncMock()
        session.refresh = AsyncMock()
        session.get = AsyncMock()
        session.delete = AsyncMock()
        return session
    
    @pytest.fixture
    def basic_dao(self, mock_session):
        """Create BasicDao instance with mock session."""
        dao = BasicDao(mock_session)
        dao.model = HourlyArchive  # Use HourlyArchive as test model
        return dao
    
    def test_basic_dao_initialization(self, mock_session):
        """Test BasicDao initialization."""
        dao = BasicDao(mock_session)
        assert dao.session == mock_session
        assert dao.model is None
        assert dao.logger is not None
    
    async def test_bulk_upsert_success(self, basic_dao):
        """Test successful bulk upsert operation."""
        # Mock data
        test_data = [
            {"period": datetime.now(), "volume": 1000.0, "line_id": 1},
            {"period": datetime.now(), "volume": 2000.0, "line_id": 2}
        ]
        constraints = ["line_id", "period"]
        
        # Mock session execute
        basic_dao.session.execute.return_value = None
        
        # Execute bulk upsert
        await basic_dao.bulk_upsert(test_data, constraints)
        
        # Verify session methods were called
        basic_dao.session.execute.assert_called_once()
        basic_dao.session.commit.assert_called_once()
        basic_dao.session.rollback.assert_not_called()
    
    async def test_bulk_upsert_with_update_success(self, basic_dao):
        """Test successful bulk upsert with update operation."""
        # Mock data
        test_data = [
            {"period": datetime.now(), "volume": 1000.0, "line_id": 1},
            {"period": datetime.now(), "volume": 2000.0, "line_id": 2}
        ]
        constraints = ["line_id", "period"]
        
        # Mock session execute
        basic_dao.session.execute.return_value = None
        
        # Execute bulk upsert with update
        await basic_dao.bulk_upsert_with_update(test_data, constraints)
        
        # Verify session methods were called
        basic_dao.session.execute.assert_called_once()
        basic_dao.session.commit.assert_called_once()
        basic_dao.session.rollback.assert_not_called()
    
    async def test_bulk_upsert_exception_handling(self, basic_dao):
        """Test bulk upsert exception handling."""
        # Mock data
        test_data = [{"period": datetime.now(), "volume": 1000.0, "line_id": 1}]
        constraints = ["line_id", "period"]
        
        # Mock session execute to raise exception
        basic_dao.session.execute.side_effect = Exception("Database error")
        
        # Execute bulk upsert and expect exception
        with pytest.raises(Exception):
            await basic_dao.bulk_upsert(test_data, constraints)
        
        # Verify rollback was called
        basic_dao.session.rollback.assert_called_once()
        basic_dao.session.commit.assert_not_called()
    
    async def test_get_all(self, basic_dao):
        """Test get_all method."""
        # Mock result
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            HourlyArchive(id=1, volume=1000.0),
            HourlyArchive(id=2, volume=2000.0)
        ]
        basic_dao.session.execute.return_value = mock_result
        
        # Execute get_all
        result = await basic_dao.get_all()
        
        # Verify result
        assert len(result) == 2
        assert result[0].id == 1
        assert result[1].id == 2
    
    async def test_get_last_period(self, basic_dao):
        """Test get_last_period method."""
        # Mock result
        mock_result = MagicMock()
        mock_result.scalar.return_value = datetime(2024, 12, 25, 14, 30)
        basic_dao.session.execute.return_value = mock_result
        
        # Execute get_last_period
        result = await basic_dao.get_last_period()
        
        # Verify result
        assert result == datetime(2024, 12, 25, 14, 30)
    
    async def test_get_range_with_all_filters(self, basic_dao):
        """Test get_range method with all filters."""
        from_date = datetime(2024, 12, 25, 0, 0)
        to_date = datetime(2024, 12, 25, 23, 59)
        line_ids = [1, 2, 3]
        
        # Mock result
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            HourlyArchive(id=1, line_id=1, volume=1000.0),
            HourlyArchive(id=2, line_id=2, volume=2000.0)
        ]
        basic_dao.session.execute.return_value = mock_result
        
        # Execute get_range
        result = await basic_dao.get_range(from_date, to_date, line_ids)
        
        # Verify result
        assert len(result) == 2
        assert result[0].line_id == 1
        assert result[1].line_id == 2
    
    async def test_get_range_with_no_filters(self, basic_dao):
        """Test get_range method with no filters."""
        # Mock result
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            HourlyArchive(id=1, volume=1000.0),
            HourlyArchive(id=2, volume=2000.0)
        ]
        basic_dao.session.execute.return_value = mock_result
        
        # Execute get_range with no filters
        result = await basic_dao.get_range()
        
        # Verify result
        assert len(result) == 2
    
    async def test_get_last_for_to_date(self, basic_dao):
        """Test get_last_for_to_date method."""
        to_date = datetime(2024, 12, 25, 23, 59)
        line_ids = [1, 2]
        
        # Mock result
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = HourlyArchive(
            id=1, line_id=1, volume=1000.0
        )
        basic_dao.session.execute.return_value = mock_result
        
        # Execute get_last_for_to_date
        result = await basic_dao.get_last_for_to_date(to_date, line_ids)
        
        # Verify result
        assert result.id == 1
        assert result.line_id == 1
    
    async def test_get_by_id_found(self, basic_dao):
        """Test get_by_id method when record is found."""
        # Mock session get
        mock_record = HourlyArchive(id=1, volume=1000.0)
        basic_dao.session.get.return_value = mock_record
        
        # Execute get_by_id
        result = await basic_dao.get_by_id(1)
        
        # Verify result
        assert result == mock_record
        basic_dao.session.get.assert_called_once_with(HourlyArchive, 1)
    
    async def test_get_by_id_not_found(self, basic_dao):
        """Test get_by_id method when record is not found."""
        # Mock session get to return None
        basic_dao.session.get.return_value = None
        
        # Execute get_by_id
        result = await basic_dao.get_by_id(999)
        
        # Verify result
        assert result is None
    
    async def test_update_by_id_success(self, basic_dao):
        """Test update_by_id method success."""
        # Mock existing record
        existing_record = HourlyArchive(id=1, volume=1000.0, line_id=1)
        basic_dao.session.get.return_value = existing_record

        # Create update data with model_dump method
        class MockUpdateData:
            def model_dump(self, exclude_unset=True):
                return {"volume": 2000.0}
        
        update_data = MockUpdateData()
        
        # Execute update_by_id
        result = await basic_dao.update_by_id(1, update_data)
        
        # Verify result
        assert result == existing_record
        assert result.volume == 2000.0
        basic_dao.session.add.assert_called_once_with(existing_record)
        basic_dao.session.commit.assert_called_once()
    
    async def test_update_by_id_not_found(self, basic_dao):
        """Test update_by_id method when record is not found."""
        # Mock session get to return None
        basic_dao.session.get.return_value = None

        # Create update data with model_dump method
        class MockUpdateData:
            def model_dump(self, exclude_unset=True):
                return {"volume": 2000.0}
        
        update_data = MockUpdateData()
        
        # Execute update_by_id
        result = await basic_dao.update_by_id(999, update_data)
        
        # Verify result
        assert result is None
        basic_dao.session.add.assert_not_called()
        basic_dao.session.commit.assert_not_called()
    
    async def test_create_item_success(self, basic_dao):
        """Test create_item method success."""
        # Create test data
        from backend.db.models import HourlyArchiveCreate
        create_data = HourlyArchiveCreate(
            period=datetime.now(),
            volume=1000.0,
            w_volume_dp=0.1,
            pressure=5.0,
            temperature=20.0,
            density=0.7,
            line_id=1
        )
        
        # Mock session operations
        basic_dao.session.add.return_value = None
        basic_dao.session.refresh.return_value = None
        
        # Execute create_item
        result = await basic_dao.create_item(create_data)
        
        # Verify result
        assert result is not None
        basic_dao.session.add.assert_called_once()
        basic_dao.session.commit.assert_called_once()
        basic_dao.session.refresh.assert_called_once()
    
    async def test_create_item_integrity_error(self, basic_dao):
        """Test create_item method with integrity error."""
        from sqlalchemy.exc import IntegrityError
        from backend.db.dao.custom_exceptions import DatabaseIntegrityError
        from backend.db.models import HourlyArchive
        from datetime import datetime
        create_data = HourlyArchive(
            period=datetime.now(),
            volume=1000.0,
            w_volume_dp=0.1,
            pressure=5.0,
            temperature=20.0,
            density=0.7,
            line_id=1
        )
        # Mock session commit to raise IntegrityError
        basic_dao.session.commit.side_effect = IntegrityError("", "", "")
        # Execute create_item and expect DatabaseIntegrityError
        with pytest.raises(DatabaseIntegrityError):
            await basic_dao.create_item(create_data)
        # Verify rollback was called
        basic_dao.session.rollback.assert_called_once()
    
    async def test_delete_item_success(self, basic_dao):
        """Test delete_item method success."""
        from backend.db.models import HourlyArchive
        # Mock existing record
        existing_record = HourlyArchive(id=1, volume=1000.0, period=datetime.now())
        def get_by_id(_):
            return existing_record
        basic_dao.get_by_id = get_by_id
        # Execute delete_item
        result = await basic_dao.delete_item(1)
        # Verify result
        assert result is True
        basic_dao.session.delete.assert_called_once_with(existing_record)
        basic_dao.session.commit.assert_called_once()
    
    async def test_delete_item_not_found(self, basic_dao):
        """Test delete_item method when record is not found."""
        def get_by_id(_):
            return None
        basic_dao.get_by_id = get_by_id
        # Execute delete_item
        result = await basic_dao.delete_item(999)
        # Verify result
        assert result is False
        basic_dao.session.delete.assert_not_called()
        basic_dao.session.commit.assert_not_called()
    
    async def test_get_data_counts_by_hour(self, basic_dao):
        """Test get_data_counts_by_hour method."""
        from_date = datetime(2024, 12, 25, 0, 0)
        to_date = datetime(2024, 12, 25, 23, 59)
        line_ids = [1, 2]
        
        # Mock result
        mock_result = MagicMock()
        mock_result.all.return_value = [
            MagicMock(hour_group=datetime(2024, 12, 25, 14, 0), record_count=24),
            MagicMock(hour_group=datetime(2024, 12, 25, 15, 0), record_count=24)
        ]
        basic_dao.session.execute.return_value = mock_result
        
        # Execute get_data_counts_by_hour
        result = await basic_dao.get_data_counts_by_hour(from_date, to_date, line_ids)
        
        # Verify result
        assert len(result) == 2
        assert result[0]['hour_group'] == datetime(2024, 12, 25, 14, 0)
        assert result[0]['record_count'] == 24
        assert result[1]['hour_group'] == datetime(2024, 12, 25, 15, 0)
        assert result[1]['record_count'] == 24


class TestHourlyArchiveDao:
    """Test HourlyArchiveDao specific functionality."""
    
    @pytest.fixture
    def mock_session(self):
        """Mock database session."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        return session
    
    def test_hourly_archive_dao_initialization(self, mock_session):
        """Test HourlyArchiveDao initialization."""
        dao = HourlyArchiveDao(mock_session)
        assert dao.model == HourlyArchive
        assert dao.session == mock_session


class TestLineDao:
    """Test LineDao specific functionality."""
    
    @pytest.fixture
    def mock_session(self):
        """Mock database session."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        return session
    
    def test_line_dao_initialization(self, mock_session):
        """Test LineDao initialization."""
        dao = LineDao(mock_session)
        assert dao.model == Line
        assert dao.session == mock_session
    
    async def test_get_or_create_new(self, mock_session):
        """Test get_or_create for new line."""
        dao = LineDao(mock_session)
        # Patch get to return None
        dao.get = AsyncMock(return_value=None)
        # Mock create_item
        mock_line = Line(id=1, line=1, name="Test Line", gas_volume_calc_id=1)
        dao.create_item = AsyncMock(return_value=mock_line)
        # Execute get_or_create
        result = await dao.get_or_create(1, 1)
        # Verify result
        assert result == mock_line
        dao.get.assert_called_once_with(gas_volume_calc_id=1, line=1)
        dao.create_item.assert_called_once()
    
    async def test_get_or_create_existing(self, mock_session):
        """Test get_or_create for existing line."""
        dao = LineDao(mock_session)
        # Patch get to return existing_line
        existing_line = Line(id=1, line=1, name="Test Line", gas_volume_calc_id=1)
        dao.get = AsyncMock(return_value=existing_line)
        # Execute get_or_create
        result = await dao.get_or_create(1, 1)
        # Verify result
        assert result == existing_line
        dao.get.assert_called_once_with(gas_volume_calc_id=1, line=1)


class TestGasVolumeCalcDao:
    """Test GasVolumeCalcDao specific functionality."""
    
    @pytest.fixture
    def mock_session(self):
        """Mock database session."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        return session
    
    def test_gas_volume_calc_dao_initialization(self, mock_session):
        """Test GasVolumeCalcDao initialization."""
        dao = GasVolumeCalcDao(mock_session)
        assert dao.model == GasVolumeCalc
        assert dao.session == mock_session
    
    async def test_get_or_create_new(self, mock_session):
        """Test get_or_create for new gas volume calc."""
        dao = GasVolumeCalcDao(mock_session)
        # Patch get to return None
        dao.get = AsyncMock(return_value=None)
        # Mock create_item
        mock_calc = GasVolumeCalc(id=1, address=101, lumg_id=1)
        dao.create_item = AsyncMock(return_value=mock_calc)
        # Execute get_or_create
        result = await dao.get_or_create(address=101, lumg_id=1)
        # Verify result
        assert result == mock_calc
        dao.get.assert_called_once_with(address=101, lumg_id=1)
        dao.create_item.assert_called_once()
    
    async def test_get_or_create_existing(self, mock_session):
        """Test get_or_create for existing gas volume calc."""
        dao = GasVolumeCalcDao(mock_session)
        # Patch get to return existing_calc
        existing_calc = GasVolumeCalc(id=1, address=101, lumg_id=1)
        dao.get = AsyncMock(return_value=existing_calc)
        # Execute get_or_create
        result = await dao.get_or_create(address=101, lumg_id=1)
        # Verify result
        assert result == existing_calc
        dao.get.assert_called_once_with(address=101, lumg_id=1) 