import multiprocessing
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


def bulk_upsert_worker(archives_list, dao, constraint_list):
    dao().bulk_upsert(archives_list, constraint_list)


def update_archive(
    archive_gen, dao, constraint_list: list, chunk_size: int, max_processes=5
):
    with multiprocessing.Pool(max_processes) as pool:
        tasks = []

        while True:
            try:
                archives_list = next(archive_gen)
                task = pool.apply_async(
                    bulk_upsert_worker,
                    (archives_list, dao, constraint_list),
                )
                tasks.append(task)

            except StopIteration:
                break

        for task in tasks:
            task.wait()


def update_worker(engine, path: str, archive_dao, constraint, chunk_size: int):
    archive_engine = engine(path=path, chunk_size=chunk_size)
    archives_gen = archive_engine.read()
    update_archive(archives_gen, archive_dao, constraint, chunk_size)


def update_hostlibs():
    current_directory = os.getcwd()
    path = os.path.join(current_directory, backend_settings.get("HOSTLIB_PATH"))
    chunk_size = backend_settings.get("CHUNK_SIZE")

    with UnzipUtils(path) as unzip_utils:
        workers = [
            (DailyEngine, DailyArchiveDao, DAILY_ARCHIVE_CONSTRAINT),
            (HourlyEngine, HourlyArchiveDao, HOURLY_ARCHIVE_CONSTRAINT),
            (EditEngine, EditArchiveDao, EDIT_ARCHIVE_CONSTRAINT),
            (SysEngine, SysArchiveDao, SYS_ARCHIVE_CONSTRAINT),
        ]

        processes = []
        for engine, archive_dao, constraint in workers:
            p = multiprocessing.Process(
                target=update_worker,
                args=(
                    engine,
                    unzip_utils.temp_path,
                    archive_dao,
                    constraint,
                    chunk_size,
                ),
            )
            p.start()
            processes.append(p)

        for p in processes:
            p.join()


if __name__ == "__main__":
    # path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/ASK/hostlib"
    update_hostlibs()
