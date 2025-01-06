from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.models import DAILY_ARCHIVE_CONSTRAINT, HOURLY_ARCHIVE_CONSTRAINT
from backend.hl_engine.hl_engine import Hostlib
from utils.files_utils import UnzipUtils


def update_hostlibs(path: str):
    with UnzipUtils(path) as unzip_utils:
        hostlib = Hostlib(path=unzip_utils.temp_path)
        daily_archives_list = hostlib.read_daily_archive()
        DailyArchiveDao().bulk_upsert(daily_archives_list, DAILY_ARCHIVE_CONSTRAINT)

        hourly_archives_list = hostlib.read_hourly_archive()
        HourlyArchiveDao().bulk_upsert(hourly_archives_list, HOURLY_ARCHIVE_CONSTRAINT)

if __name__ == "__main__":
    path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/11Листопад/"
    update_hostlibs(path_dir)
