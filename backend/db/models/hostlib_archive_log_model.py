from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Index
from sqlmodel import Field, SQLModel


class HostlibArchiveLog(SQLModel, table=True):
    """Which archives the poller has read, so it does not read them twice.

    Which archive to read used to be decided from the file NAME — it was taken
    apart into a source and a snapshot time, and only the newest snapshot of
    each source was read — and a row was forgotten after a day, so the whole
    history of the share was re-read daily to be safe. The name decides
    nothing now: an archive is read once, written down here, and passed over
    from then on. A row is keyed by where the file lies, what it is called,
    how big it is and when it was written, so the copy that replaces one is
    another file and is read.

    `status` is `ok`, `error` (the update of that path failed — tried again
    after a quarter of an hour) or `broken` (does not open as a zip at all;
    the extraction passes over it). A row lives as long as its file lies in
    the folder, and a week longer; see scheduler_runner.FORGET_AFTER.

    Reading an archive twice is harmless — rows are inserted only where the
    same key is not already there, and a closed record never changes in a
    later snapshot — which is why an update asked for by hand ignores this log
    entirely and reads everything.
    """

    __tablename__ = "hostlib_archive_log"
    __table_args__ = (
        Index("idx_hostlib_archive_log_lookup", "path", "filename", "size"),
        Index("idx_hostlib_archive_log_processed", "processed_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    #: The configured data path, resolved — the folder archives arrive in.
    path: str = Field(max_length=512)
    #: Relative to `path`, as the source names it: "Dnipropetr_2026_09_11_23.zip".
    filename: str = Field(max_length=512)
    size: int = Field(sa_type=BigInteger)
    file_mtime: datetime
    status: str = Field(max_length=16)          # ok | error | broken
    error: Optional[str] = Field(default=None, max_length=2000)
    processed_at: datetime
