from sqlmodel import create_engine, Session

from backend.settings import backend_settings


class DbEngine:
    def __init__(self):

        self.db_username = backend_settings.get("POSTGRES_USER")
        self.db_password = backend_settings.get("POSTGRES_PASSWORD")
        self.db_host = backend_settings.get("DB_HOST")
        self.db_port = backend_settings.get("DB_PORT")
        self.db_name = backend_settings.get("POSTGRES_DB")

        self.postgres_url = (
            f"postgresql://{self.db_username}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    def create_db_engine(self):
        engine = create_engine(self.postgres_url)
        return engine

    def get_session(self):
        return Session(self.create_db_engine())
