"""Updater settings, all from the environment (the compose service passes .env)."""
import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    token: str
    # Host path of the backend checkout, mounted at the same path in here so
    # the bind mounts compose creates on our behalf resolve on the host. Empty
    # = read it from our own container's compose label.
    project_dir: str
    container_name: str
    backend_service: str
    scheduler_service: str
    frontend_service: str
    db_service: str
    backend_health_url: str
    frontend_health_url: str
    health_timeout: int
    keep_backups: int
    keep_jobs: int


def load() -> Settings:
    return Settings(
        token=os.getenv("UPDATER_TOKEN", ""),
        project_dir=os.getenv("HLV_PROJECT_DIR", ""),
        container_name=os.getenv("UPDATER_CONTAINER", "hlviewer_updater"),
        backend_service=os.getenv("UPDATER_BACKEND_SERVICE", "fastapi_v2"),
        scheduler_service=os.getenv("UPDATER_SCHEDULER_SERVICE", "scheduler_v2"),
        frontend_service=os.getenv("UPDATER_FRONTEND_SERVICE", "frontend_v2"),
        db_service=os.getenv("UPDATER_DB_SERVICE", "db_v2"),
        backend_health_url=os.getenv("UPDATER_BACKEND_HEALTH_URL", "http://fastapi_v2:8001/config"),
        frontend_health_url=os.getenv("UPDATER_FRONTEND_HEALTH_URL", "http://frontend_v2/"),
        health_timeout=_int("UPDATER_HEALTH_TIMEOUT", 240),
        keep_backups=_int("UPDATER_KEEP_BACKUPS", 5),
        keep_jobs=_int("UPDATER_KEEP_JOBS", 50),
    )
