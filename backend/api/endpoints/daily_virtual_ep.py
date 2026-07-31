"""Daily archives with virtual-line (ring) support. Logic lives in
BaseVirtualArchiveRouter; this module only binds the DAO/path/range cap."""

from backend.api.endpoints.base_virtual_archive_ep import BaseVirtualArchiveRouter
from backend.db.dao.daily_archive_dao import DailyArchiveDao


class DailyVirtualRouter(BaseVirtualArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/daily_virtual/",
            tag="daily_virtual",
            archive_dao=DailyArchiveDao,
            period_type="daily",
        )


daily_virtual_router = DailyVirtualRouter().router
