from sqlmodel import create_engine, Session


class DbEngine:
    def __init__(self):
        self.sqlite_file_name = "D:/Projects/HLViewer/HLViewer/backend/db/ask.db"
        self.sqlite_url = f"sqlite:///{self.sqlite_file_name}"

    def create_db_engine(self):
        connect_args = {"check_same_thread": False}
        engine = create_engine(self.sqlite_url, connect_args=connect_args)
        return engine

    def get_session(self):
        return Session(self.create_db_engine())
