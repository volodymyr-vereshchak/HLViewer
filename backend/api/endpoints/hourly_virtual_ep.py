"""Hourly archives with virtual-line (ring) support. Logic lives in
BaseVirtualArchiveRouter; this module only binds the DAO/path/range cap."""

from backend.api.endpoints.base_virtual_archive_ep import BaseVirtualArchiveRouter
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao


class HourlyVirtualRouter(BaseVirtualArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/hourly_virtual/",
            tag="hourly_virtual",
            archive_dao=HourlyArchiveDao,
            period_type="hourly",
        )


hourly_virtual_router = HourlyVirtualRouter().router
