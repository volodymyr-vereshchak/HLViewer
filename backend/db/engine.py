import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from backend.settings import backend_settings


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class DbEngine:
    # ── Pool sizing ──────────────────────────────────────────────────────────
    # IMPORTANT: every uvicorn worker AND the scheduler is a separate process,
    # and each instantiates BOTH pools. Total connections to Postgres is roughly
    #   processes × (API_cap)  +  update_cap (only ONE process updates at a time,
    #                                          guarded by the update_job DB lock)
    # With 4 workers + 1 scheduler and the defaults below:
    #   5 × (10+5) + (25+10) = 110, which needs max_connections >= ~120.
    # The compose files set Postgres max_connections=200 to leave headroom.
    # Tune via env on constrained DBs (e.g. default Postgres max_connections=100).
    API_POOL_SIZE = _int_env("DB_POOL_SIZE", 10)
    API_MAX_OVERFLOW = _int_env("DB_MAX_OVERFLOW", 5)
    UPDATE_POOL_SIZE = _int_env("DB_UPDATE_POOL_SIZE", 25)
    UPDATE_MAX_OVERFLOW = _int_env("DB_UPDATE_MAX_OVERFLOW", 10)

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
            pool_size=self.API_POOL_SIZE,
            max_overflow=self.API_MAX_OVERFLOW,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
            connect_args={"server_settings": {"application_name": "hlviewer_api"}},
        )
        self.async_session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

        # Update pool — dedicated for hostlib workers.
        # Sized for 6 concurrent LUMGs × 5 archive engines = 30 sessions max.
        self.update_engine = create_async_engine(
            self.postgres_url,
            echo=False,
            pool_size=self.UPDATE_POOL_SIZE,
            max_overflow=self.UPDATE_MAX_OVERFLOW,
            pool_timeout=60,
            pool_recycle=1800,
            pool_pre_ping=True,
            connect_args={"server_settings": {"application_name": "hlviewer_update"}},
        )
        self.update_session_factory = async_sessionmaker(
            self.update_engine, expire_on_commit=False
        )


_db = DbEngine()
async_session_factory = _db.async_session_factory
update_session_factory = _db.update_session_factory


async def get_session():
    """FastAPI dependency: one API-pool session per request.

    Prefer `session: AsyncSession = Depends(get_session)` in endpoints over
    opening `async with async_session_factory()` inline — the dependency keeps
    endpoints testable and gives one obvious place to change session policy."""
    async with async_session_factory() as session:
        yield session
