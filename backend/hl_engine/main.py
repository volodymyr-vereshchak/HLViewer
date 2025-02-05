import os

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
from backend.hl_engine.hourly_engine import HourlyEngine
from backend.hl_engine.sys_engine import SysEngine
from backend.settings import backend_settings
from utils.files_utils import UnzipUtils


def update_archive(archive_gen, dao, constraint_list: list, chunk_size: int):
    while True:
        try:
            archives_list = next(archive_gen)
            dao().bulk_upsert(archives_list, constraint_list, chunk_size=chunk_size)
        except StopIteration:
            break


def update_worker(engine, path: str, archive_dao, constraint, chunk_size: int):
    archive_engine = engine(path=path, chunk_size=chunk_size)
    archives_gen = archive_engine.read()
    update_archive(archives_gen, archive_dao, constraint, chunk_size)


def update_hostlibs():
    current_directory = os.getcwd()
    path = os.path.join(current_directory, backend_settings.get("HOSTLIB_PATH"))
    chunk_size = backend_settings.get("CHUNK_SIZE")
    with UnzipUtils(path) as unzip_utils:
        update_worker(
            DailyEngine,
            unzip_utils.temp_path,
            DailyArchiveDao,
            DAILY_ARCHIVE_CONSTRAINT,
            chunk_size,
        )
        update_worker(
            HourlyEngine,
            unzip_utils.temp_path,
            HourlyArchiveDao,
            HOURLY_ARCHIVE_CONSTRAINT,
            chunk_size,
        )
        update_worker(
            EditEngine,
            unzip_utils.temp_path,
            EditArchiveDao,
            EDIT_ARCHIVE_CONSTRAINT,
            chunk_size,
        )
        update_worker(
            SysEngine,
            unzip_utils.temp_path,
            SysArchiveDao,
            SYS_ARCHIVE_CONSTRAINT,
            chunk_size,
        )


if __name__ == "__main__":
    # path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/ASK/hostlib"
    update_hostlibs()
