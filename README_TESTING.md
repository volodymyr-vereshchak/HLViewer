# HLViewer Testing Guide

This document provides comprehensive information about the testing setup and how to run tests for the HLViewer project.

## Table of Contents

- [Overview](#overview)
- [Test Structure](#test-structure)
- [Setup](#setup)
- [Running Tests](#running-tests)
- [Test Types](#test-types)
- [Coverage](#coverage)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## Overview

The HLViewer project includes a comprehensive test suite covering:

- **Unit Tests**: Individual component testing
- **Integration Tests**: API endpoints and database operations
- **Data Processing Tests**: Binary file parsing and archive processing
- **Notification Tests**: Telegram bot and email functionality

## Test Structure

```
tests/
├── __init__.py
├── conftest.py                 # Pytest configuration and fixtures
├── unit/                       # Unit tests
│   ├── __init__.py
│   ├── test_data_classes.py   # Data structure tests
│   ├── test_files_utils.py    # File utility tests
│   ├── test_hl_engine.py      # Core engine tests
│   └── test_dao.py            # Data access object tests
└── integration/               # Integration tests
    ├── __init__.py
    ├── test_api_endpoints.py  # API endpoint tests
    ├── test_telegram_notifier.py  # Notification tests
    └── test_database_integration.py  # Database tests
```

## Setup

### 1. Install Dependencies

```bash
# Install testing dependencies
pip install -r requirements.txt
```

### 2. Environment Setup

Create a `.env.test` file for test-specific environment variables:

```env
# Test Database
POSTGRES_USER=test_user
POSTGRES_PASSWORD=test_password
DB_HOST=localhost
DB_PORT=5432
POSTGRES_DB=test_db

# Test Settings
HOSTLIB_PATH=./test_hostlibs
CHUNK_SIZE=100
BOT_TOKEN=test_token
CHAT_ID=123456789
EMAIL_PASSWORD=test_email_password
```

### 3. Database Setup (for integration tests)

For database integration tests, you can use:

- **SQLite** (in-memory, default for tests)
- **PostgreSQL** (for full integration testing)

## Running Tests

### Using the Test Runner Script

```bash
# Run all tests
python run_tests.py all

# Run unit tests only
python run_tests.py unit

# Run integration tests only
python run_tests.py integration

# Run API tests only
python run_tests.py api

# Run database tests only
python run_tests.py database

# Generate coverage report
python run_tests.py coverage

# Verbose output
python run_tests.py all --verbose

# Disable coverage
python run_tests.py all --no-cov
```

### Using Pytest Directly

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_data_classes.py

# Run tests with specific marker
pytest -m unit
pytest -m integration
pytest -m database

# Run tests with verbose output
pytest -v

# Run tests and show coverage
pytest --cov=backend --cov=utils --cov-report=term-missing

# Generate HTML coverage report
pytest --cov=backend --cov=utils --cov-report=html:htmlcov
```

## Test Types

### Unit Tests (`tests/unit/`)

Unit tests focus on individual components in isolation:

#### Data Classes (`test_data_classes.py`)
- Tests binary data structure parsing
- Validates struct format and size calculations
- Tests data conversion and validation

```python
def test_hour_struct_unpack(self):
    """Test unpacking binary data into HourStruct."""
    test_data = struct.pack("=5B6f", 12, 25, 24, 14, 30, 1000.5, 0.0, 0.1, 5.2, 20.5, 0.7)
    result = HourStruct.unpack(test_data)
    assert result.month == 12
    assert result.volume == 1000.5
```

#### File Utilities (`test_files_utils.py`)
- Tests archive file processing
- Tests ZIP file extraction
- Tests file finding and reading

#### HL Engine (`test_hl_engine.py`)
- Tests core data processing engine
- Tests file parameter extraction
- Tests chunked data processing

#### DAO Layer (`test_dao.py`)
- Tests data access objects
- Tests CRUD operations
- Tests bulk operations and constraints

### Integration Tests (`tests/integration/`)

Integration tests verify component interactions:

#### API Endpoints (`test_api_endpoints.py`)
- Tests FastAPI endpoints
- Tests request/response handling
- Tests error scenarios

```python
async def test_update_data_success(self, mock_update_hostlibs, client):
    """Test successful data update endpoint."""
    response = client.post("/update_data/")
    assert response.status_code == 202
    assert response.json() == {"message": "Updated"}
```

#### Database Integration (`test_database_integration.py`)
- Tests database operations with real database
- Tests constraints and relationships
- Tests performance with larger datasets

#### Telegram Notifier (`test_telegram_notifier.py`)
- Tests bot command handling
- Tests message sending
- Tests user subscription management

## Coverage

### Coverage Configuration

Coverage is configured in `pytest.ini`:

```ini
addopts = 
    --cov=backend
    --cov=utils
    --cov-report=term-missing
    --cov-report=html:htmlcov
    --cov-report=xml
    --cov-fail-under=80
```

### Coverage Reports

Generate coverage reports:

```bash
# Terminal coverage report
pytest --cov=backend --cov=utils --cov-report=term-missing

# HTML coverage report
pytest --cov=backend --cov=utils --cov-report=html:htmlcov

# XML coverage report (for CI/CD)
pytest --cov=backend --cov=utils --cov-report=xml
```

### Coverage Targets

- **Minimum coverage**: 80%
- **Target coverage**: 90%+
- **Critical paths**: 100%

## Test Markers

Use markers to run specific test categories:

```bash
# Run unit tests
pytest -m unit

# Run integration tests
pytest -m integration

# Run database tests
pytest -m database

# Run API tests
pytest -m api

# Run slow tests
pytest -m slow
```

## Best Practices

### 1. Test Organization

- Keep tests close to the code they test
- Use descriptive test names
- Group related tests in classes
- Use fixtures for common setup

### 2. Mocking

- Mock external dependencies (databases, APIs)
- Use `unittest.mock` for complex mocking
- Mock at the right level (not too deep)

### 3. Test Data

- Use factories for test data creation
- Keep test data realistic but minimal
- Use fixtures for reusable test data

### 4. Async Testing

- Use `pytest-asyncio` for async tests
- Mark async test functions with `@pytest.mark.asyncio`
- Use `AsyncMock` for async mocks

### 5. Database Testing

- Use in-memory SQLite for unit tests
- Use test database for integration tests
- Clean up after each test
- Use transactions for rollback

## Fixtures

### Common Fixtures

```python
@pytest.fixture
def mock_session():
    """Mock database session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session

@pytest.fixture
def sample_hourly_data():
    """Sample hourly archive data."""
    return {
        "period": datetime(2024, 12, 25, 14, 30),
        "volume": 1000.5,
        "line_id": 1
    }
```

### Database Fixtures

```python
@pytest_asyncio.fixture
async def test_session(test_session_factory):
    """Create test database session."""
    async with test_session_factory() as session:
        yield session
        await session.rollback()
```

## Troubleshooting

### Common Issues

#### 1. Import Errors

```bash
# Ensure you're in the project root
cd /path/to/HLViewer

# Install dependencies
pip install -r requirements.txt

# Set PYTHONPATH
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

#### 2. Database Connection Issues

```bash
# Check database is running
docker compose up -d db_v2

# Check connection settings
python -c "from backend.db.engine import async_session_factory; print('DB OK')"
```

#### 3. Test Failures

```bash
# Run with verbose output
pytest -v

# Run specific failing test
pytest tests/unit/test_specific.py::TestClass::test_method -v

# Run with debug output
pytest --tb=long
```

#### 4. Coverage Issues

```bash
# Check coverage configuration
pytest --cov=backend --cov-report=term-missing

# Generate detailed HTML report
pytest --cov=backend --cov-report=html:htmlcov
open htmlcov/index.html
```

### Debug Mode

Run tests in debug mode:

```bash
# Run with pdb on failures
pytest --pdb

# Run with pdb on all tests
pytest --pdbcls=IPython.terminal.debugger:Pdb
```

## Continuous Integration

### GitHub Actions Example

```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.9
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest --cov=backend --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v1
```

## Performance Testing

For performance-critical components:

```bash
# Run performance tests
pytest tests/integration/test_database_integration.py::TestDatabasePerformance

# Run with timing
pytest --durations=10
```

## Security Testing

Consider adding security tests for:

- Input validation
- SQL injection prevention
- Authentication/authorization
- Data sanitization

## Contributing

When adding new tests:

1. Follow the existing test structure
2. Use appropriate markers
3. Add fixtures for common setup
4. Ensure good coverage
5. Update this documentation if needed

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [Pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [Coverage.py](https://coverage.readthedocs.io/)
- [FastAPI Testing](https://fastapi.tiangolo.com/tutorial/testing/) 