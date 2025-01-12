from sqlmodel import create_engine, Session
from dotenv import load_dotenv
import os


class DbEngine:
    def __init__(self):
        load_dotenv()

        self.db_username = os.getenv("DB_USERNAME")
        self.db_password = os.getenv("DB_PASSWORD")
        self.db_host = os.getenv("DB_HOST")
        self.db_port = os.getenv("DB_PORT")
        self.db_name = os.getenv("DB_NAME")

        self.postgres_url = (
            f"postgresql://{self.db_username}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    def create_db_engine(self):
        engine = create_engine(self.postgres_url)
        return engine

    def get_session(self):
        return Session(self.create_db_engine())
