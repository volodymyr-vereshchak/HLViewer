from backend.db.models import (
    Lumg, LumgCreate, LumgList,
    GasVolumeCalc, GasVolumeCalcCreate, GasVolumeCalcList,
    GasVolumeCalcType, GasVolumeCalcTypeCreate, GasVolumeCalcTypeList,
    DailyArchive, DailyArchiveCreate, DailyArchiveList,
    HourlyArchive, HourlyArchiveCreate, HourlyArchiveList,
    EditType, EditTypeList, EditTypeCreate, EditTypeUpdate,
    EditArchive, EditArchiveList, EditArchiveCreate,
    SysType, SysTypeList, SysTypeCreate, SysTypeUpdate,
    SysArchive, SysArchiveList, SysArchiveCreate
)
from backend.db.engine import DbEngine
from sqlmodel import SQLModel


def create_db_and_tables():
    engine = DbEngine().create_db_engine()
    SQLModel.metadata.create_all(engine)

if __name__ == "__main__":
   create_db_and_tables()
