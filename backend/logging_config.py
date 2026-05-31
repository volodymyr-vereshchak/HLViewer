"""Central logging configuration for the HLViewer backend.

The API runs under `uvicorn --workers 8`, i.e. 8 separate OS processes that all
log to the same file. A plain `RotatingFileHandler` is NOT safe in that setup —
concurrent rollovers race and corrupt/lose lines. We therefore use
`concurrent-log-handler` (file-locked, multiprocess-safe) when available, and
fall back to the stdlib handler with a warning if the package is missing.

Files (under LOG_DIR, default <repo>/logs — bind-mounted to the host in Docker):
  - backend.log        all records at LOG_LEVEL (default INFO) and above
  - backend.error.log  ERROR and above only (quick triage)
  - frontend.log       browser-reported errors (see api/endpoints/logs_ep.py)

Everything also goes to the console so `docker logs` keeps working.

Env knobs: LOG_DIR, LOG_LEVEL, LOG_MAX_BYTES (default 10 MB), LOG_BACKUP_COUNT
(default 5).
"""

import logging
import os
from logging import Logger

try:
    from concurrent_log_handler import ConcurrentRotatingFileHandler as _RotatingHandler
    _MP_SAFE = True
except Exception:  # pragma: no cover - fallback only when dependency is absent
    from logging.handlers import RotatingFileHandler as _RotatingHandler
    _MP_SAFE = False

_LOG_FORMAT = "%(asctime)s %(levelname)s %(processName)s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def log_dir() -> str:
    """Return (creating if needed) the directory where log files are written."""
    default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
    )
    path = os.getenv("LOG_DIR", default)
    os.makedirs(path, exist_ok=True)
    return path


def _make_file_handler(filename: str, level: int) -> logging.Handler:
    max_bytes = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    backups = int(os.getenv("LOG_BACKUP_COUNT", "5"))
    full_path = os.path.join(log_dir(), filename)
    handler = _RotatingHandler(
        full_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    return handler


def setup_logging() -> None:
    """Configure root + library loggers. Idempotent and safe to call per process."""
    global _configured
    if _configured:
        return

    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Drop any handlers attached earlier (old inline handler, uvicorn defaults).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    root.addHandler(_make_file_handler("backend.log", level))
    root.addHandler(_make_file_handler("backend.error.log", logging.ERROR))

    # Quiet down chatty libraries.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)

    # Let uvicorn/fastapi records flow through the root handlers (files + console)
    # instead of uvicorn's own console-only handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    _configured = True

    if not _MP_SAFE:
        logging.getLogger(__name__).warning(
            "concurrent-log-handler not installed; using RotatingFileHandler, which "
            "is NOT safe across multiple uvicorn workers (log lines may be lost on "
            "rotation). Install concurrent-log-handler to fix."
        )


def get_frontend_logger() -> Logger:
    """Logger whose records (browser errors) also land in logs/frontend.log."""
    logger = logging.getLogger("frontend")
    if not getattr(logger, "_frontend_configured", False):
        logger.setLevel(logging.INFO)
        logger.addHandler(_make_file_handler("frontend.log", logging.INFO))
        logger.propagate = True  # also reaches backend.log / backend.error.log / console
        logger._frontend_configured = True
    return logger
