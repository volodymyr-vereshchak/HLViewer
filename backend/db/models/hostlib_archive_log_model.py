from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Index
from sqlmodel import Field, SQLModel


class HostlibArchiveLog(SQLModel, table=True):
    """Which hostlib archives the poller has already taken in, for a day.

    The poller used to remember the last archive it processed in its own
    memory, so a restart forgot it and nothing outside the process could see
    it. Kept here instead — and only for a day: the source folders gain new
    snapshots every hour, and an archive older than that is superseded by the
    ones that followed it.

    Processing an archive twice is harmless — rows are inserted only where the
    same key is not already there, and a closed record never changes in a later
    snapshot, only new ones are appended — so a pruned entry costs at most one
    redundant run, never wrong data.
    """

    __tablename__ = "hostlib_archive_log"
    __table_args__ = (
        Index("idx_hostlib_archive_log_lookup", "path", "filename", "size"),
        Index("idx_hostlib_archive_log_processed", "processed_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    #: The configured data path, resolved — one folder, possibly several sources.
    path: str = Field(max_length=512)
    #: Relative to `path`, as the source names it: "Dnipropetr_2026_09_11_23.zip".
    filename: str = Field(max_length=512)
    size: int = Field(sa_type=BigInteger)
    file_mtime: datetime
    status: str = Field(max_length=16)          # ok | error
    error: Optional[str] = Field(default=None, max_length=2000)
    processed_at: datetime
