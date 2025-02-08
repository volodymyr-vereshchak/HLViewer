from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import Lumg, LumgCreate, LumgUpdate


class LumgDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = Lumg

    def get(self, name: str):
        statement = select(self.model).where(self.model.name == name)
        with self.get_session() as session:
            result = session.exec(statement).first()
        return result

    def update_if_exist(self, name: str):
        result = self.get(name)
        if result:
            result = self.update_by_id(result.id, LumgUpdate(name=name))
        return result

    def get_or_create(self, name: str):
        result = self.get(name)
        if not result:
            result = self.create_item(LumgCreate(name=name))

        return result


if __name__ == "__main__":
    # from backend.db.models.lumg_model import LumgUpdate

    lumg_db = LumgUpdate(name="LVUMG")
    lumg = LumgDao().update_by_id(1, LumgUpdate(name="LVUMG"))
    pass
