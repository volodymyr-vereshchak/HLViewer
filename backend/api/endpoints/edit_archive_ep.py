from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.edit_archive_dao import EditArchiveDao
from backend.db.models import EditArchiveList


class EditArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/edit_archive/",
            archive_list_class=EditArchiveList,
            tags=["edit"],
            archive_dao=EditArchiveDao,
        )


edit_router = EditArchiveRouter().router
