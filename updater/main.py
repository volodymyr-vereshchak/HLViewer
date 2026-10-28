"""HTTP API of the updater.

Two kinds of caller:
  - the backend, for everything that decides or changes something. It has
    already checked that the person is an admin — re-reading the account, as
    every request there does — and proves itself with UPDATER_TOKEN;
  - the progress view in the browser, through nginx, for /updater/watch only.
    It reads one job by the watch token handed out when that job started:
    while the update runs, the backend it would otherwise ask is the thing
    being restarted.
No port is published; both reach this container over the compose network.
"""
import json
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from updater import pipeline
from updater.ops import CommandError, Git, Platform, Shell, strip_credentials
from updater.settings import load
from updater.store import Job, Store, public_view, summary, watch_allowed

settings = load()
app = FastAPI(title="HLViewer updater", docs_url=None, redoc_url=None, openapi_url=None)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


class Runtime:
    """What the process knows about the deployment, resolved on first use."""

    def __init__(self):
        self.lock = threading.Lock()
        self.job: Job | None = None
        self._context: tuple[Path, Platform, Store] | None = None

    def context(self) -> tuple[Path, Platform, Store]:
        if self._context is None:
            shell = Shell()
            project_dir = Path(settings.project_dir or _own_project_dir(shell))
            if not (project_dir / ".git").exists():
                raise RuntimeError(
                    f"каталог проєкту {project_dir} не змонтовано в updater — перевірте HLV_ROOT у .env"
                )
            out = shell.run(["docker", "compose", "config", "--format", "json"],
                            cwd=project_dir, quiet=True, timeout=60)
            platform = Platform(shell, settings, project_dir, json.loads(out))
            store = Store(project_dir / "update", settings.keep_jobs)
            store.recover_interrupted()
            self._context = (project_dir, platform, store)
        return self._context

    def forget(self) -> None:
        # The compose file may have changed with the update just applied.
        self._context = None

    def busy(self) -> bool:
        return self.job is not None and self.job.data["status"] == "running"


runtime = Runtime()


def _own_project_dir(shell: Shell) -> str:
    label = '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
    out = shell.run(["docker", "inspect", "-f", label, settings.container_name], quiet=True)
    if not out.strip():
        raise RuntimeError("не вдалося визначити каталог проєкту — задайте HLV_PROJECT_DIR")
    return out.strip()


def _context():
    try:
        return runtime.context()
    except (RuntimeError, CommandError, OSError, KeyError, ValueError) as exc:
        raise HTTPException(503, f"Updater не налаштований: {exc}") from exc


def _repos(platform: Platform, shell: Shell | None = None) -> dict[str, Git]:
    shell = shell or Shell()
    return {"backend": Git(shell, platform.project_dir), "frontend": Git(shell, platform.frontend_dir)}


def _config(store: Store, repos: dict[str, Git]) -> dict:
    """Stored settings over defaults read from the checkouts themselves."""
    stored = store.load_config()
    config = {"repos": {}, "token": stored.get("token") or None}
    for kind, git in repos.items():
        saved = (stored.get("repos") or {}).get(kind) or {}
        config["repos"][kind] = {
            "url": saved.get("url") or git.origin_url(),
            "branch": saved.get("branch") or git.branch() or ("master" if kind == "backend" else "main"),
        }
    return config


def _public_config(config: dict) -> dict:
    return {"repos": config["repos"], "token_set": bool(config.get("token"))}


def check_is_current(check: dict, installed: dict, config: dict) -> bool:
    """A stored check describes what an update WOULD bring, from a given
    commit and branch. Once either moved — an update installed it, somebody
    pulled by hand, the branch was switched — its "N new commits" is a claim
    about a state that no longer exists, and showing it offers an update to
    what is already running."""
    for kind, entry in (check.get("repos") or {}).items():
        head = (entry.get("head") or {}).get("sha")
        if head and head != (installed.get(kind) or {}).get("sha"):
            return False
        repo = config["repos"].get(kind) or {}
        if (entry.get("url"), entry.get("branch")) != (repo.get("url"), repo.get("branch")):
            return False
    return True


def require_backend(x_updater_token: str | None = Header(default=None)) -> None:
    if not settings.token:
        raise HTTPException(503, "UPDATER_TOKEN не задано в .env")
    if not x_updater_token or not secrets.compare_digest(x_updater_token, settings.token):
        raise HTTPException(401, "Невірний токен updater")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status", dependencies=[Depends(require_backend)])
def status():
    _, platform, store = _context()
    repos = _repos(platform)
    config = _config(store, repos)
    installed = {}
    for kind, git in repos.items():
        try:
            installed[kind] = {"dir": str(git.dir), **git.info(git.head())}
        except CommandError as exc:
            installed[kind] = {"dir": str(git.dir), "error": str(exc)}
    history = store.history(1)
    check = store.load_check()
    if check and not check_is_current(check, installed, config):
        check = None
    return {
        "installed": installed,
        "config": _public_config(config),
        "check": check,
        "busy": runtime.busy(),
        "job": runtime.job.snapshot() if runtime.busy() else None,
        "last_job": history[0] if history else None,
    }


@app.post("/check", dependencies=[Depends(require_backend)])
def check():
    _, platform, store = _context()
    if runtime.busy():
        raise HTTPException(409, "Іде оновлення")
    repos = _repos(platform)
    result = pipeline.check(repos, _config(store, repos))
    store.save_check(result)
    return result


class StartBody(BaseModel):
    target: dict[str, str]
    started_by: str = ""


@app.post("/start", dependencies=[Depends(require_backend)])
def start(body: StartBody):
    _, platform, store = _context()
    target = {k: v for k, v in body.target.items() if k in pipeline.KINDS}
    if not target or not all(_SHA.match(v) for v in target.values()):
        raise HTTPException(422, "Невірна ціль оновлення")
    with runtime.lock:
        if runtime.busy():
            raise HTTPException(409, "Оновлення вже виконується")
        job = Job.create(store.jobs_dir, body.started_by[:100], target)
        runtime.job = job
    shell = Shell(job.log)
    worker = threading.Thread(
        target=_run, args=(job, _repos(platform, shell), platform.with_log(job.log), store),
        name=f"update-{job.id}", daemon=True,
    )
    worker.start()
    return {"job_id": job.id, "watch_token": job.data["watch_token"]}


def _run(job: Job, repos: dict[str, Git], platform: Platform, store: Store) -> None:
    try:
        pipeline.Update(job, repos, platform, settings).run()
    except Exception as exc:  # noqa: BLE001 — the job must never stay 'running'
        job.log(f"Внутрішня помилка updater: {exc!r}")
        job.finish(pipeline.FAILED, f"Збій updater: {exc}")
    finally:
        store.prune()
        runtime.forget()


@app.get("/config", dependencies=[Depends(require_backend)])
def get_config():
    _, platform, store = _context()
    return _public_config(_config(store, _repos(platform)))


class RepoBody(BaseModel):
    url: str
    branch: str


class ConfigBody(BaseModel):
    repos: dict[str, RepoBody]
    # None keeps the stored token, "" removes it.
    token: str | None = None


def _validate_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise HTTPException(422, f"Адреса має бути https://…: {url}")
    if strip_credentials(url) != url:
        raise HTTPException(422, "Не вписуйте логін і токен в адресу — для токена є окреме поле")
    return url


@app.put("/config", dependencies=[Depends(require_backend)])
def put_config(body: ConfigBody):
    _, platform, store = _context()
    if runtime.busy():
        raise HTTPException(409, "Іде оновлення")
    stored = store.load_config()
    repos = {}
    for kind in pipeline.KINDS:
        repo = body.repos.get(kind)
        if repo is None:
            raise HTTPException(422, f"Немає налаштувань для {kind}")
        branch = repo.branch.strip()
        if not _BRANCH.match(branch) or branch.startswith("-"):
            raise HTTPException(422, f"Невірна гілка: {branch}")
        repos[kind] = {"url": _validate_url(repo.url), "branch": branch}
    token = stored.get("token")
    if body.token is not None:
        token = body.token.strip() or None
    store.save_config({"repos": repos, "token": token})
    return _public_config(_config(store, _repos(platform)))


@app.get("/history", dependencies=[Depends(require_backend)])
def history():
    _, _, store = _context()
    return store.history(30)


@app.get("/jobs/{job_id}", dependencies=[Depends(require_backend)])
def get_job(job_id: str):
    _, _, store = _context()
    if runtime.job is not None and runtime.job.id == job_id:
        return runtime.job.snapshot()
    data = store.load_job(job_id)
    if data is None:
        raise HTTPException(404, "Немає такого оновлення")
    return public_view(data)


@app.get("/updater/watch/{job_id}")
def watch(job_id: str, x_watch_token: str | None = Header(default=None)):
    """The progress view. Path as nginx passes it through, unchanged."""
    job = runtime.job
    if job is not None and job.id == job_id:
        # A snapshot: the worker thread appends to the log while this reads.
        data = {**job.snapshot(), "watch_token": job.data["watch_token"]}
    else:
        _, _, store = _context()
        data = store.load_job(job_id) or {}
    if not data or not watch_allowed(data, x_watch_token or ""):
        raise HTTPException(404, "Немає такого оновлення")
    return {**summary(data), "steps": data["steps"], "log": data["log"]}
