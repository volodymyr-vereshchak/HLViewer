from sqlmodel import create_engine


class DbEngine:
    def __init__(self):
        self.sqlite_file_name = "D:/Projects/HLViewer/HLViewer/backend/db/ask.db"
        self.sqlite_url = f"sqlite:///{self.sqlite_file_name}"

    def create_db_engine(self):
        engine = create_engine(self.sqlite_url, echo=True)
        return engine
