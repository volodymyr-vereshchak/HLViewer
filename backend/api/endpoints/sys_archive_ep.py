from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.sys_archive_dao import SysArchiveDao
from backend.db.models import SysArchiveEndpointList


class SysArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/sys_archive/",
            archive_list_class=SysArchiveEndpointList,
            tags=["sys"],
            archive_dao=SysArchiveDao,
        )


sys_router = SysArchiveRouter().router
