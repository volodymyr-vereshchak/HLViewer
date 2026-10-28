"""Files the updater keeps: its settings, the last check and one file per job.

Files rather than the application database on purpose — the database is one
of the things an update stops, migrates and may restore, and the record of
what happened must survive all three. They live in <project>/update/, which
is gitignored, so a reset of the checkout never touches them.
"""
import copy
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

# The steps every job shows, in order. «Відкат» is added only when it runs.
STEPS = [
    ("prepare", "Підготовка"),
    ("build", "Збирання образів"),
    ("backup", "Копія бази"),
    ("switch", "Перемикання"),
    ("verify", "Перевірка"),
]
ROLLBACK_STEP = ("rollback", "Відкат")

# How long the progress page may still read a finished job by its watch token.
WATCH_GRACE_SECONDS = 30 * 60
MAX_LOG_LINES = 5000


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_private(path: Path, data: dict) -> None:
    """Write JSON readable by the owner only: config.json holds the git token."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Job:
    """One update run. Mutated by the worker thread, read by API requests."""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self._lock = threading.Lock()
        self._saved_at = 0.0

    @classmethod
    def create(cls, jobs_dir: Path, started_by: str, target: dict) -> "Job":
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        data = {
            "id": job_id,
            "status": "running",
            "result": None,
            "message": None,
            "started_at": _now(),
            "finished_at": None,
            "started_by": started_by,
            "target": target,
            "from": {},
            "warnings": [],
            "steps": [{"id": s, "label": label, "state": "pending", "note": None} for s, label in STEPS],
            "log": [],
            "watch_token": secrets.token_urlsafe(24),
        }
        job = cls(jobs_dir / f"{job_id}.json", data)
        job.save(force=True)
        return job

    @property
    def id(self) -> str:
        return self.data["id"]

    def step(self, step_id: str, state: str, note: str | None = None) -> None:
        with self._lock:
            steps = self.data["steps"]
            entry = next((s for s in steps if s["id"] == step_id), None)
            if entry is None:
                label = dict([ROLLBACK_STEP]).get(step_id, step_id)
                entry = {"id": step_id, "label": label, "state": "pending", "note": None}
                steps.append(entry)
            entry["state"] = state
            if note is not None:
                entry["note"] = note
        self.save(force=True)

    def log(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            log = self.data["log"]
            for part in line.splitlines() or [""]:
                log.append(f"{stamp} {part}")
            if len(log) > MAX_LOG_LINES:
                del log[: len(log) - MAX_LOG_LINES]
        self.save()

    def warn(self, text: str) -> None:
        with self._lock:
            if text not in self.data["warnings"]:
                self.data["warnings"].append(text)
        self.save(force=True)

    def set(self, **fields) -> None:
        with self._lock:
            self.data.update(fields)
        self.save(force=True)

    def finish(self, result: str, message: str) -> None:
        with self._lock:
            self.data.update(status="finished", result=result, message=message, finished_at=_now())
            for s in self.data["steps"]:
                if s["state"] == "running":
                    s["state"] = "failed"
        self.log(f"=== {message}")
        self.save(force=True)

    def save(self, force: bool = False) -> None:
        # The log grows a line per command output; writing the whole file for
        # each would make a docker build thrash the disk. Steps and the finish
        # always force a write, so the file is never behind on what matters.
        now = time.monotonic()
        if not force and now - self._saved_at < 1.0:
            return
        with self._lock:
            snapshot = copy.deepcopy(self.data)
        _write_private(self.path, snapshot)
        self._saved_at = now

    def snapshot(self) -> dict:
        with self._lock:
            data = copy.deepcopy(self.data)
        data.pop("watch_token", None)
        return data


def public_view(data: dict) -> dict:
    data = dict(data)
    data.pop("watch_token", None)
    return data


def summary(data: dict) -> dict:
    return {k: data.get(k) for k in (
        "id", "status", "result", "message", "started_at", "finished_at",
        "started_by", "target", "from", "warnings",
    )}


def watch_allowed(data: dict, token: str) -> bool:
    expected = data.get("watch_token") or ""
    if not token or not secrets.compare_digest(expected, token):
        return False
    if data.get("status") == "running":
        return True
    finished = data.get("finished_at")
    if not finished:
        return False
    try:
        age = datetime.now().astimezone() - datetime.fromisoformat(finished)
    except ValueError:
        return False
    return age.total_seconds() <= WATCH_GRACE_SECONDS


class Store:
    def __init__(self, state_dir: Path, keep_jobs: int = 50):
        self.dir = state_dir
        self.jobs_dir = state_dir / "jobs"
        self.keep_jobs = keep_jobs
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    # ── settings ──────────────────────────────────────────────────────────
    def load_config(self) -> dict:
        return _read(self.dir / "config.json") or {}

    def save_config(self, config: dict) -> None:
        _write_private(self.dir / "config.json", config)

    # ── last check ────────────────────────────────────────────────────────
    def load_check(self) -> dict | None:
        return _read(self.dir / "check.json")

    def save_check(self, check: dict) -> None:
        _write_private(self.dir / "check.json", check)

    # ── jobs ──────────────────────────────────────────────────────────────
    def job_ids(self) -> list[str]:
        return sorted((p.stem for p in self.jobs_dir.glob("*.json")), reverse=True)

    def load_job(self, job_id: str) -> dict | None:
        if not job_id.replace("-", "").isdigit():
            return None
        return _read(self.jobs_dir / f"{job_id}.json")

    def history(self, limit: int = 30) -> list[dict]:
        out = []
        for job_id in self.job_ids()[:limit]:
            data = self.load_job(job_id)
            if data:
                out.append(summary(data))
        return out

    def prune(self) -> None:
        for job_id in self.job_ids()[self.keep_jobs:]:
            (self.jobs_dir / f"{job_id}.json").unlink(missing_ok=True)

    def recover_interrupted(self) -> None:
        """A job still 'running' on startup died with the previous process."""
        for job_id in self.job_ids()[:5]:
            data = self.load_job(job_id)
            if data and data.get("status") == "running":
                job = Job(self.jobs_dir / f"{job_id}.json", data)
                job.finish(
                    "failed",
                    "Updater перезапустився під час оновлення — стан сервера треба перевірити вручну",
                )
