import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

# Load environment variables from .env file
load_dotenv()

from backend.api.main import app
from backend.db.engine import async_session_factory
from backend.settings import backend_settings


# Remove custom event_loop fixture to avoid conflicts with pytest-asyncio
# pytest-asyncio will handle the event loop automatically


@pytest.fixture
def test_settings():
    """Test settings with in-memory database."""
    original_settings = backend_settings.copy()
    
    # Override settings for testing
    test_settings = {
        **original_settings,
        "POSTGRES_DB": "test_db",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "POSTGRES_USER": "test_user",
        "POSTGRES_PASSWORD": "test_password",
        "HOSTLIB_PATH": "./test_hostlibs",
        "CHUNK_SIZE": 100,
        "BOT_TOKEN": "test_token",
        "CHAT_ID": "123456789",
        "EMAIL_PASSWORD": "test_email_password",
    }
    
    return test_settings


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def sample_binary_data():
    """Sample binary data for testing archive parsing."""
    # Sample data for HourStruct: month, day, year, hour, minutes, volume, unknown, w_volume_dp, pressure, temperature, density
    import struct
    data = struct.pack("=5B6f", 12, 25, 24, 14, 30, 1000.5, 0.0, 0.1, 5.2, 20.5, 0.7)
    return data


@pytest.fixture
def sample_archive_file(temp_dir, sample_binary_data):
    """Create a sample archive file for testing."""
    import os
    
    # Create test directory structure
    test_file_path = os.path.join(temp_dir, "test_archive.bin")
    
    # Write sample data multiple times to simulate archive file
    with open(test_file_path, "wb") as f:
        for _ in range(10):  # 10 records
            f.write(sample_binary_data)
    
    return test_file_path


@pytest.fixture
def mock_session():
    """Mock database session."""
    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def sample_hourly_data():
    """Sample hourly archive data."""
    return {
        "period": datetime(2024, 12, 25, 14, 30),
        "volume": 1000.5,
        "w_volume_dp": 0.1,
        "pressure": 5.2,
        "temperature": 20.5,
        "density": 0.7,
        "line_id": 1
    }


@pytest.fixture
def sample_daily_data():
    """Sample daily archive data."""
    return {
        "period": datetime(2024, 12, 25),
        "volume": 24000.0,
        "w_volume_dp": 2.4,
        "pressure": 5.2,
        "temperature": 20.5,
        "density": 0.7,
        "line_id": 1
    }


@pytest.fixture
def sample_line_data():
    """Sample line data."""
    return {
        "line": 1,
        "meter": True,
        "name": "Test Line 1",
        "gas_volume_calc_id": 1
    }


@pytest.fixture
def sample_gas_volume_calc_data():
    """Sample gas volume calculation data."""
    return {
        "address": 101,
        "lumg_id": 1
    }


@pytest.fixture
def datetime_range():
    """Sample datetime range for testing."""
    start = datetime(2024, 12, 25, 0, 0)
    end = datetime(2024, 12, 25, 23, 59)
    return start, end


@pytest.fixture
def mock_telegram_bot():
    """Mock Telegram bot."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()
    return bot


@pytest.fixture
def mock_email_notifier():
    """Mock email notifier."""
    notifier = MagicMock()
    notifier.send_message = MagicMock()
    return notifier


# Database fixtures for integration tests
@pytest.fixture(scope="session")
def test_engine():
    """Create test database engine (PostgreSQL)."""
    test_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:password@localhost:5432/hlviewer_test"
    )
    engine = create_async_engine(test_url, echo=False)
    return engine

@pytest_asyncio.fixture(scope="session")
async def test_session_factory(test_engine):
    """Create test session factory."""
    async_session = sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    # Create tables
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_session
    # Cleanup
    await test_engine.dispose()

@pytest_asyncio.fixture
async def test_session(test_session_factory) -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    async with test_session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def client():
    """Create test client for FastAPI."""
    return TestClient(app)


# Factory fixtures for creating test data
@pytest.fixture
def line_factory():
    """Factory for creating line test data."""
    def _create_line(**kwargs):
        default_data = {
            "line": 1,
            "meter": True,
            "name": "Test Line",
            "gas_volume_calc_id": 1
        }
        default_data.update(kwargs)
        return default_data
    return _create_line


@pytest.fixture
def hourly_archive_factory():
    """Factory for creating hourly archive test data."""
    def _create_hourly_archive(**kwargs):
        default_data = {
            "period": datetime.now(),
            "volume": 1000.0,
            "w_volume_dp": 0.1,
            "pressure": 5.0,
            "temperature": 20.0,
            "density": 0.7,
            "line_id": 1
        }
        default_data.update(kwargs)
        return default_data
    return _create_hourly_archive


@pytest.fixture
def daily_archive_factory():
    """Factory for creating daily archive test data."""
    def _create_daily_archive(**kwargs):
        default_data = {
            "period": datetime.now().date(),
            "volume": 24000.0,
            "w_volume_dp": 2.4,
            "pressure": 5.0,
            "temperature": 20.0,
            "density": 0.7,
            "line_id": 1
        }
        default_data.update(kwargs)
        return default_data
    return _create_daily_archive 