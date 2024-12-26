from backend.db.dao.basic_dao import BasicDao
from backend.db.models import Lumg
from backend.db.models.lumg_model import LumgUpdate


class LumgDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = Lumg


if __name__ == "__main__":
    lumg_db = LumgUpdate(name="LVUMG")
    lumg = LumgDao().update_by_id(1, LumgUpdate(name="LVUMG"))
    pass