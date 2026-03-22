from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.sys_archive_dao import SysArchiveDao
from backend.db.models import SysArchiveEndpointList


class SysArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/sys/",
            archive_list_class=SysArchiveEndpointList,
            tags=["sys"],
            archive_dao=SysArchiveDao,
            max_days=30,
        )


sys_router = SysArchiveRouter().router
