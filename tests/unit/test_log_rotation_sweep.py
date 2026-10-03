"""The one thing in this system that grew without a limit.

`logs/` held 33 files named `backend.log.rotate.<id>`, 330 MB, the oldest from
the 6th of August — ten megabytes per rotation that nothing counted and nothing
deleted. They are what ConcurrentRotatingFileHandler renames the live file to
while it shuffles the chain under its lock; a second rotating handler on the
same path (the old `utils.logger.logger_setup`) rotated without that lock, the
shuffle lost its target, and the temporary file stayed.

A disk that fills is how Postgres ends up in recovery mode, which is how a
server ends up looking hung — as this one did on 28.09.2026.
"""
import logging
import os
import time

from backend import logging_config
from utils.logger import logger_setup


def _leftover(folder, name: str, age_seconds: float, size: int = 1024) -> str:
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


class TestTheSweep:
    def test_a_stale_rotation_temp_is_deleted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logging_config, "log_dir", lambda: str(tmp_path))
        orphan = _leftover(str(tmp_path), "backend.log.rotate.123456", 7200)

        logging_config._sweep_rotation_leftovers()

        assert not os.path.exists(orphan)

    def test_a_fresh_one_is_left_alone(self, tmp_path, monkeypatch):
        """It may be mid-rollover in another process this very second."""
        monkeypatch.setattr(logging_config, "log_dir", lambda: str(tmp_path))
        busy = _leftover(str(tmp_path), "backend.log.rotate.999", 5)

        logging_config._sweep_rotation_leftovers()

        assert os.path.exists(busy)

    def test_the_logs_themselves_are_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logging_config, "log_dir", lambda: str(tmp_path))
        kept = [
            _leftover(str(tmp_path), "backend.log", 7200),
            _leftover(str(tmp_path), "backend.log.1", 90000),
            _leftover(str(tmp_path), "backend.error.log.3", 900000),
        ]

        logging_config._sweep_rotation_leftovers()

        assert all(os.path.exists(path) for path in kept)

    def test_a_missing_folder_is_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            logging_config, "log_dir", lambda: str(tmp_path / "nope"))
        logging_config._sweep_rotation_leftovers()      # must simply return


class TestTheLegacyHelper:
    """It used to attach a second rotating handler to the same file and cut the
    whole `backend.*` tree off the root handlers."""

    def test_it_adds_no_handlers(self):
        logger = logger_setup("backend-test-no-handlers")
        assert logger.handlers == []

    def test_it_keeps_propagation(self):
        """Every module logger is `backend.something`. With propagation off at
        "backend", none of them reached the root's files — from whenever the
        first DAO happened to be constructed."""
        logger = logger_setup("backend-test-propagates")
        assert logger.propagate is True

    def test_it_is_the_same_logger_the_module_name_gives(self):
        assert logger_setup("backend") is logging.getLogger("backend")
