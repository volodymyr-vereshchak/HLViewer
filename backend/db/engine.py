from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from backend.settings import backend_settings


class DbEngine:
    def __init__(self):
        self.db_username = backend_settings.get("POSTGRES_USER")
        self.db_password = backend_settings.get("POSTGRES_PASSWORD")
        self.db_host = backend_settings.get("DB_HOST")
        self.db_port = backend_settings.get("DB_PORT")
        self.db_name = backend_settings.get("POSTGRES_DB")

        self.postgres_url = (
            f"postgresql+asyncpg://{self.db_username}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

        # Frontend / API pool — reserved for user-facing requests
        self.engine = create_async_engine(
            self.postgres_url,
            echo=False,
            pool_size=20,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            connect_args={"server_settings": {"application_name": "hlviewer_api"}},
        )
        self.async_session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

        # Update pool — dedicated for hostlib workers, capped so it never starves the API
        self.update_engine = create_async_engine(
            self.postgres_url,
            echo=False,
            pool_size=8,
            max_overflow=2,
            pool_timeout=60,
            pool_recycle=1800,
            connect_args={"server_settings": {"application_name": "hlviewer_update"}},
        )
        self.update_session_factory = async_sessionmaker(
            self.update_engine, expire_on_commit=False
        )


_db = DbEngine()
async_session_factory = _db.async_session_factory
update_session_factory = _db.update_session_factory
