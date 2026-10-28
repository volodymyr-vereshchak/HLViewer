"""The commands the updater runs: git on the checkouts, docker on the host.

Everything goes through Shell, which writes each command and the tail of its
output to the job log — the log is what a developer reads when an update was
rolled back, so it has to show what was actually run.
"""
import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from updater import migrations

LogFn = Callable[[str], None]
MIGRATIONS_DIR = "backend/db/alembic/versions"
# Files whose change means the backend image itself must be rebuilt. The code
# is bind-mounted, so anything else takes effect on a plain restart.
IMAGE_INPUTS = ("requirements.txt", "Dockerfile")
OUTPUT_TAIL = 60


class CommandError(Exception):
    def __init__(self, args: list[str], code: int | None, output: str):
        self.cmd = args
        self.code = code
        self.output = output
        tail = output.strip().splitlines()[-1:] or [""]
        super().__init__(f"{' '.join(args[:3])} … завершилась з кодом {code}: {tail[0]}")


class Shell:
    def __init__(self, log: LogFn | None = None):
        self._log = log or (lambda _line: None)

    def with_log(self, log: LogFn) -> "Shell":
        return Shell(log)

    def run(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        env: dict | None = None,
        timeout: float | None = 600,
        quiet: bool = False,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        binary: bool = False,
    ):
        if not quiet:
            self._log(f"$ {' '.join(args)}")
        full_env = {**os.environ, **(env or {})}
        stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        stdout = open(stdout_path, "wb") if stdout_path else subprocess.PIPE
        # Binary output (git archive) and output sent to a file (pg_dump) keep
        # stderr apart, so a warning never ends up inside the data.
        separate = binary or stdout_path is not None
        try:
            proc = subprocess.run(
                args, cwd=cwd, env=full_env, stdin=stdin, stdout=stdout,
                stderr=subprocess.PIPE if separate else subprocess.STDOUT,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(args, None, f"timeout after {timeout}s") from exc
        finally:
            if stdin_path:
                stdin.close()
            if stdout_path:
                stdout.close()
        if binary and proc.returncode == 0:
            return proc.stdout
        raw = proc.stderr if separate else proc.stdout
        text = (raw or b"").decode("utf-8", errors="replace")
        if not quiet or proc.returncode != 0:
            lines = text.rstrip().splitlines()
            if len(lines) > OUTPUT_TAIL:
                self._log(f"  … ({len(lines) - OUTPUT_TAIL} рядків пропущено)")
            for line in lines[-OUTPUT_TAIL:]:
                self._log(f"  {line}")
        if proc.returncode != 0:
            raise CommandError(args, proc.returncode, text)
        return text


def strip_credentials(url: str) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


class Git:
    """One checkout. Fetches land in refs/updater/remote, never in origin/*:
    the server's own remotes are whatever whoever cloned it set up."""

    REF = "refs/updater/remote"

    def __init__(self, shell: Shell, repo_dir: Path):
        self.shell = shell
        self.dir = Path(repo_dir)

    def _git(self, *args: str, quiet: bool = True, **kw) -> str:
        return self.shell.run(["git", "-C", str(self.dir), *args], quiet=quiet, **kw)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def branch(self) -> str:
        try:
            return self._git("symbolic-ref", "--short", "HEAD").strip()
        except CommandError:
            return ""

    def origin_url(self) -> str:
        try:
            return strip_credentials(self._git("remote", "get-url", "origin").strip())
        except CommandError:
            return ""

    def info(self, sha: str) -> dict:
        out = self._git("log", "-1", "--format=%H%x1f%cI%x1f%an%x1f%s", sha)
        full, date, author, subject = (out.strip().split("\x1f") + ["", "", "", ""])[:4]
        return {"sha": full, "short": full[:8], "date": date, "author": author, "subject": subject}

    def fetch(self, url: str, branch: str, token: str | None) -> str:
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if token:
            # A header rather than a token in the URL: the URL ends up in the
            # log and in .git/FETCH_HEAD, an env var in neither.
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                       GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
        self._git("fetch", "--no-tags", "--quiet", url, f"+refs/heads/{branch}:{self.REF}",
                  quiet=False, env=env, timeout=90)
        return self._git("rev-parse", self.REF).strip()

    def has_commit(self, sha: str) -> bool:
        try:
            self._git("cat-file", "-e", f"{sha}^{{commit}}")
            return True
        except CommandError:
            return False

    def is_ancestor(self, older: str, newer: str) -> bool:
        try:
            self._git("merge-base", "--is-ancestor", older, newer)
            return True
        except CommandError as exc:
            if exc.code == 1:
                return False
            raise

    def dirty(self) -> list[str]:
        out = self._git("status", "--porcelain", "--untracked-files=no")
        return [line[3:] for line in out.splitlines() if line.strip()]

    def commits(self, older: str, newer: str, limit: int = 100) -> list[dict]:
        out = self._git("log", f"--max-count={limit}", "--format=%H%x1f%cI%x1f%an%x1f%s",
                        f"{older}..{newer}")
        result = []
        for line in out.splitlines():
            full, date, author, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
            result.append({"sha": full, "short": full[:8], "date": date,
                           "author": author, "subject": subject})
        return result

    def count(self, older: str, newer: str) -> int:
        return int(self._git("rev-list", "--count", f"{older}..{newer}").strip() or 0)

    def changed_files(self, older: str, newer: str, added_only: bool = False) -> list[str]:
        args = ["diff", "--name-only"]
        if added_only:
            args.append("--diff-filter=A")
        return [p for p in self._git(*args, older, newer).splitlines() if p]

    def untracked_collisions(self, older: str, newer: str) -> list[str]:
        """Files the update adds that already sit untracked on disk — the
        fast-forward would refuse to overwrite them, halfway through."""
        return [p for p in self.changed_files(older, newer, added_only=True)
                if (self.dir / p).exists()]

    def export(self, sha: str, dest: Path) -> None:
        data = self._git("archive", "--format=tar", sha, binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")

    def migration_heads(self, sha: str) -> set[str]:
        try:
            data = self._git("archive", "--format=tar", sha, "--", MIGRATIONS_DIR, binary=True)
        except CommandError:
            return set()
        sources = []
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.endswith(".py"):
                    sources.append(tar.extractfile(member).read().decode("utf-8", "replace"))
        return migrations.heads(sources)

    def merge_ff(self, sha: str) -> None:
        self._git("merge", "--ff-only", "--quiet", sha, quiet=False)

    def reset_hard(self, sha: str) -> None:
        self._git("reset", "--hard", "--quiet", sha, quiet=False)


def _image_ref(image: str) -> str:
    """'name' → 'name:latest'; a tag already given is kept."""
    return image if ":" in image.rsplit("/", 1)[-1] else f"{image}:latest"


def _retag(image: str, tag: str) -> str:
    return f"{_image_ref(image).rsplit(':', 1)[0]}:{tag}"


class Platform:
    """Docker, compose and the database container, as the pipeline needs them."""

    def __init__(self, shell: Shell, settings, project_dir: Path, compose: dict):
        self.shell = shell
        self.s = settings
        self.project_dir = Path(project_dir)
        services = compose.get("services", {})
        self._services = services
        self.images = {
            "backend": _image_ref(services[settings.backend_service]["image"]),
            "frontend": _image_ref(services[settings.frontend_service]["image"]),
        }
        build = services[settings.frontend_service].get("build") or {}
        self.frontend_dir = Path(build.get("context", self.project_dir.parent / "hl_frontend"))
        self.db_container = self.container(settings.db_service)

    def with_log(self, log: LogFn) -> "Platform":
        clone = object.__new__(Platform)
        clone.__dict__.update(self.__dict__)
        clone.shell = self.shell.with_log(log)
        return clone

    def container(self, service: str) -> str:
        return self._services.get(service, {}).get("container_name") or service

    # ── images ────────────────────────────────────────────────────────────
    def image_exists(self, ref: str) -> bool:
        try:
            self.shell.run(["docker", "image", "inspect", ref], quiet=True)
            return True
        except CommandError:
            return False

    def build(self, kind: str, context: Path) -> None:
        self.shell.run(["docker", "build", "--quiet", "-t", _retag(self.images[kind], "next"),
                        str(context)], timeout=1800)

    def swap_images(self, kind: str) -> None:
        image = self.images[kind]
        self.shell.run(["docker", "tag", image, _retag(image, "prev")])
        self.shell.run(["docker", "tag", _retag(image, "next"), image])

    def restore_image(self, kind: str) -> None:
        image = self.images[kind]
        self.shell.run(["docker", "tag", _retag(image, "prev"), image])

    def drop_next(self, kind: str) -> None:
        try:
            self.shell.run(["docker", "rmi", _retag(self.images[kind], "next")], quiet=True)
        except CommandError:
            pass

    def prune(self) -> None:
        """Reclaim what updates leave behind, and nothing else.

        Dangling images only — the versions that lost their tag to a newer
        build or a rollback. Never `-a`: that would take the `:prev` image the
        next rollback returns to, and images of other stacks on this host.
        Build cache older than a week: rebuilding with an unchanged
        requirements.txt stays fast, and the cache does not grow forever.
        """
        for args in (["docker", "image", "prune", "-f"],
                     ["docker", "builder", "prune", "-f", "--filter", "until=168h"]):
            try:
                self.shell.run(args)
            except CommandError:
                pass

    # ── services ──────────────────────────────────────────────────────────
    def compose(self, *args: str, timeout: float = 600) -> str:
        return self.shell.run(["docker", "compose", *args], cwd=self.project_dir, timeout=timeout)

    def stop(self, *services: str) -> None:
        self.compose("stop", *services)

    def recreate(self, *services: str) -> None:
        # --force-recreate: with the code bind-mounted, a new commit changes
        # nothing compose can see, and a plain `up` would keep the old process.
        self.compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", *services)

    def _state(self, service: str) -> dict:
        try:
            out = self.shell.run(["docker", "inspect", "-f", "{{json .State}}",
                                  self.container(service)], quiet=True)
            return json.loads(out)
        except (CommandError, ValueError):
            return {}

    @staticmethod
    def _probe(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return 200 <= resp.status < 400
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def wait_ready(self, service: str, url: str | None, timeout: int) -> tuple[bool, str]:
        """Running and answering — or how it failed. A container that keeps
        restarting is given up on early instead of waiting out the timeout."""
        deadline = time.monotonic() + timeout
        first = self._state(service).get("RestartCount", 0)
        while time.monotonic() < deadline:
            state = self._state(service)
            if state.get("RestartCount", 0) - first >= 3:
                return False, f"{service} перезапускається знову й знову"
            if state.get("Running") and (url is None or self._probe(url)):
                return True, "працює"
            time.sleep(3)
        return False, f"{service} не відповів за {timeout} с"

    def stays_up(self, service: str, seconds: int = 10) -> tuple[bool, str]:
        first = self._state(service).get("RestartCount", 0)
        time.sleep(seconds)
        state = self._state(service)
        if not state.get("Running") or state.get("RestartCount", 0) != first:
            return False, f"{service} не тримається запущеним"
        return True, "працює"

    def logs(self, service: str, lines: int = 40) -> None:
        try:
            self.compose("logs", "--no-color", f"--tail={lines}", service, timeout=30)
        except CommandError:
            pass

    # ── database ──────────────────────────────────────────────────────────
    def _psql(self, sql: str) -> str:
        script = 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tAc "$1"'
        return self.shell.run(["docker", "exec", self.db_container, "sh", "-c", script, "sh", sql],
                              quiet=True, timeout=60)

    def db_revisions(self) -> set[str]:
        return {r.strip() for r in self._psql("SELECT version_num FROM alembic_version").splitlines()
                if r.strip()}

    def data_update_running(self) -> bool:
        """The hostlib ingest holds update_job; an update must not cut it off."""
        try:
            out = self._psql(
                "SELECT 1 FROM update_job WHERE id = 1 AND status = 'running' "
                "AND updated_at > now() - interval '300 seconds'"
            )
        except CommandError:
            return False
        return out.strip() == "1"

    def db_size(self) -> int:
        return int(self._psql("SELECT pg_database_size(current_database())").strip() or 0)

    def backup_dir(self) -> Path:
        path = self.project_dir / "backups"
        path.mkdir(exist_ok=True)
        return path

    def free_space(self) -> int:
        return shutil.disk_usage(self.backup_dir()).free

    def dump(self, path: Path) -> None:
        script = 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'
        self.shell.run(["docker", "exec", self.db_container, "sh", "-c", script],
                       stdout_path=path, timeout=3600)

    def restore(self, path: Path) -> None:
        script = ('pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                  '--clean --if-exists --no-owner --single-transaction')
        self.shell.run(["docker", "exec", "-i", self.db_container, "sh", "-c", script],
                       stdin_path=path, timeout=7200)

    def downgrade(self, revision: str) -> None:
        """Alembic downgrade with the code now on disk — the new code, the only
        one that still has the downgrade() of the migrations it applied."""
        if not revision.replace("_", "").isalnum():
            raise ValueError(f"unexpected revision id: {revision!r}")
        self.compose("run", "--rm", "--no-deps", "-T", self.s.backend_service,
                     "sh", "-c", f"cd backend && alembic downgrade {revision}", timeout=1800)

    def prune_backups(self, keep: int) -> None:
        dumps = sorted(self.backup_dir().glob("pre-update-*.dump"), reverse=True)
        for old in dumps[keep:]:
            old.unlink(missing_ok=True)
