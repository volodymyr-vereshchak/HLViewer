from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import Lumg


class LumgDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = Lumg

    def get_lumg_by_name(self, name: str):
        statement = select(self.model).where(self.model.name == name)
        with self.get_session() as session:
            return session.exec(statement).first()


if __name__ == "__main__":
    from backend.db.models.lumg_model import LumgUpdate

    lumg_db = LumgUpdate(name="LVUMG")
    lumg = LumgDao().update_by_id(1, LumgUpdate(name="LVUMG"))
    pass
