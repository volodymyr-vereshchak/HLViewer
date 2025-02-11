from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.models import HourlyArchiveList


class HourlyArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/hour_archive/",
            archive_list_class=HourlyArchiveList,
            tags=["hourly"],
            archive_dao=HourlyArchiveDao,
        )


hourly_router = HourlyArchiveRouter().router
