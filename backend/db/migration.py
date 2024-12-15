from backend.db.models import SQLModel
from backend.db.engine import DbEngine


def create_db_and_tables():
    engine = DbEngine().create_db_engine()
    SQLModel.metadata.create_all(engine)

if __name__ == "__main__":
   create_db_and_tables()
