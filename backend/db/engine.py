from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
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

        self.engine = create_async_engine(self.postgres_url, echo=True)
        self.async_session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def get_session(self):
        async with self.async_session_factory() as session:
            try:
                yield session
            except Exception as exc:
                await session.rollback()
                raise exc
            finally:
                await session.close()
