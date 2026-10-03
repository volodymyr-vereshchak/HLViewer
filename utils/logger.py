"""The old per-module logger helper, kept as a name and nothing else.

It used to attach its own `RotatingFileHandler` to `./logs/<name>.log` and set
`propagate = False`. Both halves were damaging by the time the central
configuration arrived (see backend/logging_config.py), because they pointed a
SECOND rotating handler at the file that one already owns:

* The central one is a `ConcurrentRotatingFileHandler` — it rotates under a
  file lock, by renaming the live file to `backend.log.rotate.<id>` and then
  shuffling the chain. The stdlib handler knows nothing about that lock and
  rotates the same path on its own, so the shuffle loses its target and the
  temporary file stays behind. Thirty-three of them had piled up in `logs/` by
  October, 330 MB, the oldest from the 6th of August: ten megabytes per
  rotation that `backupCount` does not count and nobody deletes. A disk that
  fills is how Postgres ends up in recovery mode, which is how the whole server
  ends up looking hung (28.09.2026).

* `propagate = False` on the logger named "backend" cut off every module logger
  under it — they are all `backend.something` — from the root handlers, the
  moment any DAO happened to be constructed. Which file a line landed in
  depended on what had been instantiated first.

So this now just hands back the logger. Everything it writes reaches the single
set of handlers the application configures once, in one process-safe place.
"""
import logging


def logger_setup(
    log_name: str, max_file_size: int = 100 * 1024 * 1024, backup_count: int = 3
):
    """The named logger. No handlers: the root has them (`setup_logging`).

    The size arguments are still accepted so the call sites do not have to
    change; they are meaningless now and the rotation limits live with the
    handlers in backend/logging_config.py.
    """
    return logging.getLogger(log_name)
