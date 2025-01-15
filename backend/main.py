import os

from dotenv import load_dotenv

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.dao.edit_archive_dao import EditArchiveDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.sys_archive_dao import SysArchiveDao
from backend.db.models import (
    DAILY_ARCHIVE_CONSTRAINT,
    HOURLY_ARCHIVE_CONSTRAINT,
    EDIT_ARCHIVE_CONSTRAINT,
    SYS_ARCHIVE_CONSTRAINT,
)
from backend.hl_engine.daily_engine import DailyEngine
from backend.hl_engine.edit_engine import EditEngine
from backend.hl_engine.hl_engine import Hostlib
from backend.hl_engine.hourly_engine import HourlyEngine
from backend.hl_engine.sys_engine import SysEngine
from utils.files_utils import UnzipUtils

load_dotenv()


def update_archive(archive_gen, dao, constraint_list: list, chunk_size: int):
    while True:
        try:
            archives_list = next(archive_gen)
            dao().bulk_upsert(archives_list, constraint_list, chunk_size=chunk_size)
        except StopIteration:
            break


def update_hostlibs():
    path = os.getenv("HOSTLIBS_PATH")
    chunk_size = int(os.getenv("CHUNK_SIZE"))
    with UnzipUtils(path) as unzip_utils:
        daily_engine = DailyEngine(path=unzip_utils.temp_path, chunk_size=chunk_size)
        daily_archives_gen = daily_engine.read()
        update_archive(
            daily_archives_gen, DailyArchiveDao, DAILY_ARCHIVE_CONSTRAINT, chunk_size
        )

        hourly_engine = HourlyEngine(path=unzip_utils.temp_path, chunk_size=chunk_size)
        hourly_archives_gen = hourly_engine.read()
        update_archive(
            hourly_archives_gen, HourlyArchiveDao, HOURLY_ARCHIVE_CONSTRAINT, chunk_size
        )

        edit_engine = EditEngine(path=unzip_utils.temp_path, chunk_size=chunk_size)
        edit_archives_gen = edit_engine.read()
        update_archive(
            edit_archives_gen, EditArchiveDao, EDIT_ARCHIVE_CONSTRAINT, chunk_size
        )

        sys_engine = SysEngine(path=unzip_utils.temp_path, chunk_size=chunk_size)
        sys_archives_gen = sys_engine.read()
        update_archive(
            sys_archives_gen, SysArchiveDao, SYS_ARCHIVE_CONSTRAINT, chunk_size
        )


if __name__ == "__main__":
    # path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/ASK/hostlib"
    update_hostlibs()
