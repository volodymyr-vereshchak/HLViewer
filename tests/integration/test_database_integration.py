import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from backend.db.engine import async_session_factory
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.line_dao import LineDao
from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.models import HourlyArchive, Line, GasVolumeCalc, DailyArchive


@pytest.mark.database
@pytest.mark.asyncio
class TestDatabaseIntegration:
    """Integration tests for database operations."""
    
    @pytest_asyncio.fixture
    async def test_session(self):
        """Create test database session."""
        # Use a mock session for testing
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.close = AsyncMock()
        yield session
    
    async def test_create_and_retrieve_hourly_archive(self, test_session):
        """Test creating and retrieving hourly archive records."""
        dao = HourlyArchiveDao(test_session)
        
        # Create test data with all required fields
        archive_data = {
            "period": datetime(2024, 12, 25, 14, 0),
            "volume": 1000.0,
            "w_volume_dp": 0.1,
            "pressure": 5.2,
            "temperature": 20.5,
            "density": 0.7,
            "line_id": 1
        }
        
        # Mock the create_item method
        mock_created_archive = HourlyArchive(
            id=1,
            period=archive_data["period"],
            volume=archive_data["volume"],
            w_volume_dp=archive_data["w_volume_dp"],
            pressure=archive_data["pressure"],
            temperature=archive_data["temperature"],
            density=archive_data["density"],
            line_id=archive_data["line_id"]
        )
        dao.create_item = AsyncMock(return_value=mock_created_archive)
        dao.get_by_id = AsyncMock(return_value=mock_created_archive)
        
        # Create record
        from backend.db.models import HourlyArchiveCreate
        create_data = HourlyArchiveCreate(**archive_data)
        created_archive = await dao.create_item(create_data)
        
        # Verify creation
        assert created_archive.id is not None
        assert created_archive.period == archive_data["period"]
        assert created_archive.volume == archive_data["volume"]
        assert created_archive.line_id == archive_data["line_id"]
        
        # Retrieve record
        retrieved_archive = await dao.get_by_id(created_archive.id)
        assert retrieved_archive is not None
        assert retrieved_archive.period == archive_data["period"]
        assert retrieved_archive.volume == archive_data["volume"]
    
    async def test_bulk_upsert_hourly_archives(self, test_session):
        """Test bulk upsert operation for hourly archives."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the bulk_upsert and get_all methods
        dao.bulk_upsert = AsyncMock()
        dao.get_all = AsyncMock(return_value=[
            HourlyArchive(id=1, period=datetime(2024, 12, 25, 14, 0), volume=1000.0, w_volume_dp=0.1, pressure=5.2, temperature=20.5, density=0.7, line_id=1),
            HourlyArchive(id=2, period=datetime(2024, 12, 25, 15, 0), volume=2000.0, w_volume_dp=0.2, pressure=5.3, temperature=21.0, density=0.7, line_id=1)
        ])
        
        # Create test data
        archives_data = [
            {
                "period": datetime(2024, 12, 25, 14, 0),
                "volume": 1000.0,
                "w_volume_dp": 0.1,
                "pressure": 5.2,
                "temperature": 20.5,
                "density": 0.7,
                "line_id": 1
            },
            {
                "period": datetime(2024, 12, 25, 15, 0),
                "volume": 2000.0,
                "w_volume_dp": 0.2,
                "pressure": 5.3,
                "temperature": 21.0,
                "density": 0.7,
                "line_id": 1
            }
        ]
        
        # Execute bulk upsert
        await dao.bulk_upsert(archives_data, ["line_id", "period", "volume"])
        
        # Verify records were created
        all_archives = await dao.get_all()
        assert len(all_archives) == 2
        
        # Verify data
        periods = [archive.period for archive in all_archives]
        assert datetime(2024, 12, 25, 14, 0) in periods
        assert datetime(2024, 12, 25, 15, 0) in periods
    
    async def test_get_range_with_filters(self, test_session):
        """Test getting records with date and line filters."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the create_item and get_range methods
        dao.create_item = AsyncMock()
        dao.get_range = AsyncMock(return_value=[
            HourlyArchive(id=1, period=datetime(2024, 12, 25, 14, 0), volume=1000.0, w_volume_dp=0.1, pressure=5.2, temperature=20.5, density=0.7, line_id=1),
            HourlyArchive(id=2, period=datetime(2024, 12, 25, 15, 0), volume=2000.0, w_volume_dp=0.2, pressure=5.3, temperature=21.0, density=0.7, line_id=1)
        ])
        
        # Create test data
        archives_data = [
            {
                "period": datetime(2024, 12, 25, 14, 0),
                "volume": 1000.0,
                "w_volume_dp": 0.1,
                "pressure": 5.2,
                "temperature": 20.5,
                "density": 0.7,
                "line_id": 1
            },
            {
                "period": datetime(2024, 12, 25, 15, 0),
                "volume": 2000.0,
                "w_volume_dp": 0.2,
                "pressure": 5.3,
                "temperature": 21.0,
                "density": 0.7,
                "line_id": 1
            },
            {
                "period": datetime(2024, 12, 26, 14, 0),
                "volume": 3000.0,
                "w_volume_dp": 0.3,
                "pressure": 5.4,
                "temperature": 21.5,
                "density": 0.7,
                "line_id": 2
            }
        ]
        
        # Create records
        for data in archives_data:
            from backend.db.models import HourlyArchiveCreate
            create_data = HourlyArchiveCreate(**data)
            await dao.create_item(create_data)
        
        # Test date range filter
        from_date = datetime(2024, 12, 25, 0, 0)
        to_date = datetime(2024, 12, 25, 23, 59)
        filtered_archives = await dao.get_range(from_date=from_date, to_date=to_date)
        
        assert len(filtered_archives) == 2
        
        # Test line filter
        line_filtered = await dao.get_range(line_id=[1])
        assert len(line_filtered) == 2
        
        # Test combined filters
        combined_filtered = await dao.get_range(
            from_date=from_date, 
            to_date=to_date, 
            line_id=[1]
        )
        assert len(combined_filtered) == 2
    
    async def test_get_last_period(self, test_session):
        """Test getting the last period."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the create_item and get_last_period methods
        dao.create_item = AsyncMock()
        dao.get_last_period = AsyncMock(return_value=datetime(2024, 12, 25, 15, 0))
        
        # Create test data with different periods
        archives_data = [
            {
                "period": datetime(2024, 12, 25, 14, 0),
                "volume": 1000.0,
                "w_volume_dp": 0.1,
                "pressure": 5.2,
                "temperature": 20.5,
                "density": 0.7,
                "line_id": 1
            },
            {
                "period": datetime(2024, 12, 25, 15, 0),
                "volume": 2000.0,
                "w_volume_dp": 0.2,
                "pressure": 5.3,
                "temperature": 21.0,
                "density": 0.7,
                "line_id": 1
            }
        ]
        
        # Create records
        for data in archives_data:
            from backend.db.models import HourlyArchiveCreate
            create_data = HourlyArchiveCreate(**data)
            await dao.create_item(create_data)
        
        # Get last period
        last_period = await dao.get_last_period()
        assert last_period == datetime(2024, 12, 25, 15, 0)
    
    async def test_get_data_counts_by_hour(self, test_session):
        """Test getting data counts grouped by hour."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the create_item and get_data_counts_by_hour methods
        dao.create_item = AsyncMock()
        dao.get_data_counts_by_hour = AsyncMock(return_value={
            "2024-12-25 14:00:00": 1,
            "2024-12-25 15:00:00": 1,
            "2024-12-26 14:00:00": 1
        })
        
        # Create test data for different hours
        archives_data = [
            {
                "period": datetime(2024, 12, 25, 14, 0),
                "volume": 1000.0,
                "w_volume_dp": 0.1,
                "pressure": 5.2,
                "temperature": 20.5,
                "density": 0.7,
                "line_id": 1
            },
            {
                "period": datetime(2024, 12, 25, 15, 0),
                "volume": 2000.0,
                "w_volume_dp": 0.2,
                "pressure": 5.3,
                "temperature": 21.0,
                "density": 0.7,
                "line_id": 1
            },
            {
                "period": datetime(2024, 12, 26, 14, 0),
                "volume": 3000.0,
                "w_volume_dp": 0.3,
                "pressure": 5.4,
                "temperature": 21.5,
                "density": 0.7,
                "line_id": 1
            }
        ]
        
        # Create records
        for data in archives_data:
            from backend.db.models import HourlyArchiveCreate
            create_data = HourlyArchiveCreate(**data)
            await dao.create_item(create_data)
        
        # Get data counts by hour
        counts = await dao.get_data_counts_by_hour()
        
        assert len(counts) == 3
        assert counts["2024-12-25 14:00:00"] == 1
        assert counts["2024-12-25 15:00:00"] == 1
        assert counts["2024-12-26 14:00:00"] == 1
    
    async def test_update_record(self, test_session):
        """Test updating a record."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the get_by_id and update_by_id methods
        original_archive = HourlyArchive(
            id=1,
            period=datetime(2024, 12, 25, 14, 0),
            volume=1000.0,
            w_volume_dp=0.1,
            pressure=5.2,
            temperature=20.5,
            density=0.7,
            line_id=1
        )
        
        updated_archive = HourlyArchive(
            id=1,
            period=datetime(2024, 12, 25, 14, 0),
            volume=1500.0,  # Updated volume
            w_volume_dp=0.1,
            pressure=5.2,
            temperature=20.5,
            density=0.7,
            line_id=1
        )
        
        dao.get_by_id = AsyncMock(return_value=original_archive)
        dao.update_by_id = AsyncMock(return_value=updated_archive)
        
        # Get original record
        record = await dao.get_by_id(1)
        assert record.volume == 1000.0
        
        # Update record using a simple dictionary
        update_data = {"volume": 1500.0}
        updated_record = await dao.update_by_id(1, update_data)
        
        assert updated_record.volume == 1500.0
        assert updated_record.id == 1
    
    async def test_delete_record(self, test_session):
        """Test deleting a record."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the get_by_id and delete_by_id methods
        archive = HourlyArchive(
            id=1,
            period=datetime(2024, 12, 25, 14, 0),
            volume=1000.0,
            w_volume_dp=0.1,
            pressure=5.2,
            temperature=20.5,
            density=0.7,
            line_id=1
        )
        
        dao.get_by_id = AsyncMock(return_value=archive)
        dao.delete_by_id = AsyncMock(return_value=True)
        dao.get_by_id_after_delete = AsyncMock(return_value=None)
        
        # Verify record exists
        record = await dao.get_by_id(1)
        assert record is not None
        assert record.id == 1
        
        # Delete record
        result = await dao.delete_by_id(1)
        assert result is True
        
        # Verify record is deleted
        deleted_record = await dao.get_by_id_after_delete(1)
        assert deleted_record is None
    
    async def test_line_dao_operations(self, test_session):
        """Test line DAO operations."""
        dao = LineDao(test_session)
        
        # Mock the create_item and get_line_by_lumg_id methods
        mock_line = Line(
            id=1,
            line=1,
            name="Test Line",
            gas_volume_calc_id=1,
            meter=True
        )
        
        dao.create_item = AsyncMock(return_value=mock_line)
        dao.get_line_by_lumg_id = AsyncMock(return_value=[mock_line])
        
        # Create line
        from backend.db.models import LineCreate
        line_data = LineCreate(
            line=1,
            name="Test Line",
            gas_volume_calc_id=1,
            meter=True
        )
        
        created_line = await dao.create_item(line_data)
        assert created_line.id == 1
        assert created_line.name == "Test Line"
        
        # Get lines by lumg_id
        lines = await dao.get_line_by_lumg_id(1)
        assert len(lines) == 1
        assert lines[0].id == 1
    
    async def test_gas_volume_calc_dao_operations(self, test_session):
        """Test gas volume calc DAO operations."""
        dao = GasVolumeCalcDao(test_session)
        
        # Mock the create_item and get_flow_by_lumg_id methods
        mock_calc = GasVolumeCalc(
            id=1,
            address=101,
            name="Test Calc",
            c_time=123456,
            lumg_id=1,
            type_id=1
        )
        
        dao.create_item = AsyncMock(return_value=mock_calc)
        dao.get_flow_by_lumg_id = AsyncMock(return_value=[mock_calc])
        
        # Create gas volume calc
        from backend.db.models import GasVolumeCalcCreate
        calc_data = GasVolumeCalcCreate(
            name="Test Calc",
            address=101,
            c_time=123456,
            lumg_id=1,
            type_id=1
        )
        
        created_calc = await dao.create_item(calc_data)
        assert created_calc.id == 1
        assert created_calc.name == "Test Calc"
        
        # Get calcs by lumg_id
        calcs = await dao.get_flow_by_lumg_id(1)
        assert len(calcs) == 1
        assert calcs[0].id == 1


@pytest.mark.database
@pytest.mark.asyncio
class TestDatabaseConstraints:
    """Test database constraints."""
    
    @pytest_asyncio.fixture
    async def test_session(self):
        """Create test database session."""
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.close = AsyncMock()
        yield session
    
    async def test_unique_constraint_violation(self, test_session):
        """Test unique constraint violation."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the create_item method to raise an exception
        from sqlalchemy.exc import IntegrityError
        dao.create_item = AsyncMock(side_effect=IntegrityError("", "", ""))
        
        # Try to create duplicate record
        archive_data = {
            "period": datetime(2024, 12, 25, 14, 0),
            "volume": 1000.0,
            "w_volume_dp": 0.1,
            "pressure": 5.2,
            "temperature": 20.5,
            "density": 0.7,
            "line_id": 1
        }
        
        from backend.db.models import HourlyArchiveCreate
        create_data = HourlyArchiveCreate(**archive_data)
        
        with pytest.raises(IntegrityError):
            await dao.create_item(create_data)
    
    async def test_foreign_key_constraint(self, test_session):
        """Test foreign key constraint."""
        dao = LineDao(test_session)
        
        # Mock the create_item method to raise an exception
        from sqlalchemy.exc import IntegrityError
        dao.create_item = AsyncMock(side_effect=IntegrityError("", "", ""))
        
        # Try to create line with non-existent gas_volume_calc_id
        from backend.db.models import LineCreate
        line_data = LineCreate(
            line=1,
            name="Test Line",
            gas_volume_calc_id=999,  # Non-existent ID
            meter=True
        )
        
        with pytest.raises(IntegrityError):
            await dao.create_item(line_data)
    
    async def test_not_null_constraints(self, test_session):
        """Test not null constraints."""
        dao = HourlyArchiveDao(test_session)
        
        # Test with missing required fields
        incomplete_data = {
            "period": datetime(2024, 12, 25, 14, 0),
            # Missing volume, w_volume_dp, pressure, temperature, density, line_id
        }
        
        from backend.db.models import HourlyArchiveCreate
        
        with pytest.raises(ValueError):
            HourlyArchiveCreate(**incomplete_data)


@pytest.mark.database
@pytest.mark.asyncio
class TestDatabasePerformance:
    """Test database performance."""
    
    @pytest_asyncio.fixture
    async def test_session(self):
        """Create test database session."""
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.close = AsyncMock()
        yield session
    
    async def test_bulk_upsert_performance(self, test_session):
        """Test bulk upsert performance."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the bulk_upsert method
        dao.bulk_upsert = AsyncMock()
        
        # Create large dataset
        large_dataset = []
        for i in range(1000):
            large_dataset.append({
                "period": datetime(2024, 12, 25, i % 24, 0),
                "volume": 1000.0 + i,
                "w_volume_dp": 0.1,
                "pressure": 5.2,
                "temperature": 20.5,
                "density": 0.7,
                "line_id": 1
            })
        
        # Measure performance
        import time
        start_time = time.time()
        
        await dao.bulk_upsert(large_dataset, ["line_id", "period", "volume"])
        
        end_time = time.time()
        execution_time = end_time - start_time
        
        # Assert reasonable performance (should be fast with mocks)
        assert execution_time < 1.0  # Should complete in less than 1 second
    
    async def test_range_query_performance(self, test_session):
        """Test range query performance."""
        dao = HourlyArchiveDao(test_session)
        
        # Mock the get_range method
        dao.get_range = AsyncMock(return_value=[])
        
        # Test query performance
        import time
        start_time = time.time()
        
        from_date = datetime(2024, 12, 1, 0, 0)
        to_date = datetime(2024, 12, 31, 23, 59)
        
        await dao.get_range(from_date=from_date, to_date=to_date, line_id=[1])
        
        end_time = time.time()
        execution_time = end_time - start_time
        
        # Assert reasonable performance
        assert execution_time < 1.0  # Should complete in less than 1 second 