from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.models import DailyArchiveList


class DailyArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/daily/",
            archive_list_class=DailyArchiveList,
            tags=["daily"],
            archive_dao=DailyArchiveDao,
        )


daily_router = DailyArchiveRouter().router
