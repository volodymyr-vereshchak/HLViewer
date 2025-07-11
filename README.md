# HLViewer

A Python-based backend system for managing, processing, and serving gas volume, line, and archive data, with integration for Telegram notifications and hostlib file updates. Features a RESTful API, database access via SQLModel/SQLAlchemy, and robust test coverage.

## Features

- **RESTful API**: FastAPI-based endpoints for CRUD operations
- **Database Integration**: SQLModel/SQLAlchemy with PostgreSQL support
- **Telegram Notifications**: Real-time updates via Telegram bot
- **Email Notifications**: SMTP-based email notifications
- **Hostlib Processing**: Automated processing of hostlib archive files
- **Comprehensive Testing**: Unit and integration tests with 100% pass rate
- **Docker Support**: Containerized deployment with Docker Compose

## Project Structure

```
HLViewer/
├── backend/
│   ├── api/                    # FastAPI app and endpoints
│   │   ├── endpoints/          # API route handlers
│   │   └── main.py            # FastAPI application
│   ├── db/                    # Database layer
│   │   ├── models/            # SQLModel database models
│   │   ├── dao/               # Data Access Objects
│   │   ├── alembic/           # Database migrations
│   │   └── engine.py          # Database connection
│   ├── hl_engine/             # Business logic
│   │   ├── data_classes/      # Domain data structures
│   │   ├── hostlib_updater.py # Hostlib file processing
│   │   └── main.py           # Core processing logic
│   ├── telegram_notifier/     # Notification system
│   │   ├── telegram_norifier.py # Telegram bot
│   │   └── email_notifier.py  # Email notifications
│   └── settings.py            # Configuration
├── tests/                     # Test suite
│   ├── integration/           # Integration tests
│   └── unit/                  # Unit tests
├── hostlibs/                  # Hostlib archive files
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Docker build file
├── docker-compose.yml         # Docker Compose config
└── README.md                  # This file
```

## Prerequisites

- Python 3.12+
- PostgreSQL (for production)
- Docker (optional, for containerized deployment)

## Installation & Setup

### 1. Clone the Repository

```bash
git clone <your-repo-url>
cd HLViewer
```

### 2. Create Virtual Environment

```bash
python -m venv .venv

# On Windows
.venv\Scripts\activate

# On macOS/Linux
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Database Configuration

Create a `.env` file in the root directory:

```env
DATABASE_URL=postgresql+asyncpg://username:password@localhost/hlviewer
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_app_password
```

### 5. Database Setup

```bash
# Run database migrations
cd backend/db
alembic upgrade head
```

### 6. Run the Application

#### Development Mode

```bash
cd backend/api
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

#### Using Docker

```bash
docker-compose up --build
```

The API will be available at `http://localhost:8000`

## API Documentation

Once the server is running, you can access:

- **Interactive API Docs**: `http://localhost:8000/docs`
- **ReDoc Documentation**: `http://localhost:8000/redoc`

### Main Endpoints

- `GET /hourly-archives/` - Get hourly archive data
- `GET /lines/` - Get line information
- `GET /gas-volume-calcs/` - Get gas volume calculations
- `GET /lumgs/` - Get LUMG (Logical Units) data
- `POST /update-data/` - Trigger hostlib update process

## Testing

The project includes comprehensive test coverage:

### Run All Tests

```bash
python -m pytest tests/ -v
```

### Run Specific Test Categories

```bash
# Integration tests only
python -m pytest tests/integration/ -v

# Unit tests only
python -m pytest tests/unit/ -v

# API endpoint tests
python -m pytest tests/integration/test_api_endpoints.py -v

# Database tests
python -m pytest tests/integration/test_database_integration.py -v
```

### Test Coverage

The test suite covers:
- ✅ API endpoints and error handling
- ✅ Database operations and constraints
- ✅ Telegram bot functionality
- ✅ Email notifications
- ✅ Hostlib file processing
- ✅ Data validation and models

## Development

### Code Style

The project follows PEP 8 standards. Use a linter like `flake8` or `black`:

```bash
pip install black flake8
black backend/ tests/
flake8 backend/ tests/
```

### Adding New Features

1. Create a feature branch
2. Add tests for new functionality
3. Implement the feature
4. Ensure all tests pass
5. Submit a pull request

### Database Migrations

When modifying database models:

```bash
cd backend/db
alembic revision --autogenerate -m "Description of changes"
alembic upgrade head
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | Required |
| `TELEGRAM_BOT_TOKEN` | Telegram bot API token | Required |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications | Required |
| `SMTP_HOST` | SMTP server hostname | Required |
| `SMTP_PORT` | SMTP server port | 587 |
| `SMTP_USERNAME` | SMTP username | Required |
| `SMTP_PASSWORD` | SMTP password/app password | Required |

### Logging

Logs are stored in the `logs/` directory:
- `backend.log` - Application logs
- `telegram_notifier.log` - Notification logs

## Deployment

### Docker Deployment

```bash
# Build and run with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f
```

### Production Considerations

1. Use environment variables for sensitive data
2. Configure proper logging levels
3. Set up monitoring and health checks
4. Use a reverse proxy (nginx) in front of the API
5. Configure SSL/TLS certificates

## Troubleshooting

### Common Issues

1. **Database Connection Errors**
   - Verify `DATABASE_URL` is correct
   - Ensure PostgreSQL is running
   - Check network connectivity

2. **Telegram Bot Not Working**
   - Verify `TELEGRAM_BOT_TOKEN` is valid
   - Check `TELEGRAM_CHAT_ID` is correct
   - Ensure bot has permission to send messages

3. **Email Notifications Failing**
   - Verify SMTP credentials
   - Check firewall settings
   - Use app passwords for Gmail

### Logs

Check the logs in the `logs/` directory for detailed error information:

```bash
tail -f logs/backend.log
tail -f logs/telegram_notifier.log
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Setup

```bash
# Install development dependencies
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov

# Run tests with coverage
python -m pytest tests/ --cov=backend --cov-report=html
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For support and questions:
- Create an issue in the repository
- Contact the development team
- Check the documentation at `/docs` endpoint

## Changelog

### Version 1.0.0
- Initial release
- Complete API implementation
- Database integration
- Telegram and email notifications
- Comprehensive test suite
- Docker support 