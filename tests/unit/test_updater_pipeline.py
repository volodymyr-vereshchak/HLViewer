"""The admin updater's pipeline, against a fake git and a fake Docker host.

What matters is the line between «Не виконано» and a rollback: before the
switch nothing running may be touched, after it every change must be undone —
including the database, by downgrade or, failing that, by the dump.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater import pipeline
from updater.ops import CommandError
from updater.store import Job

OLD_B, NEW_B = "a" * 40, "b" * 40
OLD_F, NEW_F = "c" * 40, "d" * 40

SETTINGS = SimpleNamespace(
    backend_service="fastapi_v2", scheduler_service="scheduler_v2",
    frontend_service="frontend_v2", backend_health_url="http://b", frontend_health_url="http://f",
    health_timeout=5, keep_backups=5,
)


class FakeGit:
    def __init__(self, head, new, changed=(), heads_by_sha=None, dirty=()):
        self._head = head
        self._new = new
        self._changed = list(changed)
        self._heads = heads_by_sha or {}
        self._dirty = list(dirty)
        self.calls = []

    def head(self):
        return self._head

    def has_commit(self, sha):
        return sha in (self._head, self._new)

    def is_ancestor(self, older, newer):
        return older == newer or (older, newer) == (OLD_B, NEW_B) or (older, newer) == (OLD_F, NEW_F)

    def dirty(self):
        return self._dirty

    def untracked_collisions(self, older, newer):
        return []

    def changed_files(self, older, newer, added_only=False):
        return self._changed

    def migration_heads(self, sha):
        return set(self._heads.get(sha, set()))

    def export(self, sha, dest):
        self.calls.append(("export", sha))

    def merge_ff(self, sha):
        self.calls.append(("merge", sha))
        self._head = sha

    def reset_hard(self, sha):
        self.calls.append(("reset", sha))
        self._head = sha


class FakePlatform:
    """A Docker host. The database follows the code the backend starts with:
    the new code migrates to its heads (unless told it cannot start), the
    downgrade puts the old revision back (unless told to fail)."""

    def __init__(self, tmp_path: Path, backend: FakeGit, old_revs=frozenset({"r1"}),
                 new_ok=True, old_ok=True, downgrade_ok=True, build_ok=True, dump_ok=True,
                 data_running=False, migrate=True):
        self.images = {"backend": "hlviewer-fastapi-v2:latest", "frontend": "hlviewer-frontend-v2:latest"}
        self.calls = []
        self.tmp = tmp_path
        self.backend = backend
        self.revs = set(old_revs)
        self.new_ok, self.old_ok = new_ok, old_ok
        self.downgrade_ok, self.build_ok, self.dump_ok = downgrade_ok, build_ok, dump_ok
        self.data_running = data_running
        self.migrate = migrate

    def _call(self, *c):
        self.calls.append(c)

    def image_exists(self, ref):
        return True

    def build(self, kind, context):
        self._call("build", kind)
        if not self.build_ok:
            raise CommandError(["docker", "build"], 1, "pip failed")

    def swap_images(self, kind):
        self._call("swap", kind)

    def restore_image(self, kind):
        self._call("restore_image", kind)

    def drop_next(self, kind):
        self._call("drop_next", kind)

    def prune(self):
        self._call("prune")

    def stop(self, *services):
        self._call("stop", *services)

    def recreate(self, *services):
        self._call("recreate", *services)
        if "fastapi_v2" in services and self.backend.head() == NEW_B and self.migrate:
            self.revs = set(self.backend.migration_heads(NEW_B)) or self.revs

    def wait_ready(self, service, url, timeout):
        if service == "fastapi_v2":
            ok = self.new_ok if self.backend.head() == NEW_B else self.old_ok
            return ok, "працює" if ok else "не відповів"
        return True, "працює"

    def stays_up(self, service, seconds=10):
        return True, "працює"

    def logs(self, service, lines=40):
        pass

    def db_revisions(self):
        return set(self.revs)

    def data_update_running(self):
        return self.data_running

    def db_size(self):
        return 10

    def free_space(self):
        return 10 ** 9

    def backup_dir(self):
        return self.tmp

    def dump(self, path):
        self._call("dump")
        if not self.dump_ok:
            raise CommandError(["pg_dump"], 1, "disk full")
        path.write_bytes(b"dump")

    def restore(self, path):
        self._call("restore", path.name)
        self.revs = {"r1"}
        self.old_ok = True

    def downgrade(self, revision):
        self._call("downgrade", revision)
        # Only the new code has the downgrade() of the migrations it applied.
        assert self.backend.head() == NEW_B, "downgrade must run before the checkout is reset"
        if not self.downgrade_ok:
            raise CommandError(["alembic"], 1, "cannot downgrade")
        self.revs = {revision}

    def prune_backups(self, keep):
        pass


def _run(tmp_path, *, backend=None, frontend=None, target=None, **platform_kw):
    backend = backend or FakeGit(OLD_B, NEW_B, heads_by_sha={OLD_B: {"r1"}, NEW_B: {"r2"}})
    frontend = frontend or FakeGit(OLD_F, NEW_F)
    platform = FakePlatform(tmp_path, backend, **platform_kw)
    job = Job.create(tmp_path, "admin", target or {"backend": NEW_B, "frontend": NEW_F})
    pipeline.Update(job, {"backend": backend, "frontend": frontend}, platform, SETTINGS).run()
    return job.data, platform, backend, frontend


def _states(data):
    return {s["id"]: s["state"] for s in data["steps"]}


def test_updates_both_repositories(tmp_path):
    data, p, backend, frontend = _run(tmp_path)
    assert data["result"] == pipeline.UPDATED
    assert backend.head() == NEW_B and frontend.head() == NEW_F
    # Dependencies unchanged: the backend image is reused, only the frontend is built.
    assert ("build", "frontend") in p.calls and ("build", "backend") not in p.calls
    # The scheduler is stopped for the dump and comes back last.
    assert p.calls.index(("stop", "scheduler_v2")) < p.calls.index(("dump",))
    assert ("recreate", "scheduler_v2") in p.calls
    assert ("recreate", "frontend_v2") in p.calls
    assert set(_states(data).values()) == {"done"}
    assert p.calls[-1] == ("prune",)


def test_nothing_new_is_up_to_date(tmp_path):
    data, p, backend, _ = _run(tmp_path, target={"backend": OLD_B, "frontend": OLD_F})
    assert data["result"] == pipeline.UP_TO_DATE
    assert p.calls == []


def test_dependency_change_rebuilds_backend_image(tmp_path):
    backend = FakeGit(OLD_B, NEW_B, changed=["requirements.txt"],
                      heads_by_sha={OLD_B: {"r1"}, NEW_B: {"r1"}})
    data, p, _, _ = _run(tmp_path, backend=backend)
    assert data["result"] == pipeline.UPDATED
    assert ("build", "backend") in p.calls and ("swap", "backend") in p.calls


def test_failed_build_changes_nothing(tmp_path):
    data, p, backend, frontend = _run(tmp_path, build_ok=False)
    assert data["result"] == pipeline.NOT_DONE
    assert backend.head() == OLD_B and frontend.head() == OLD_F
    # Built before the backup: the scheduler was never stopped.
    assert not any(c[0] in ("stop", "dump", "recreate", "swap", "prune") for c in p.calls)


def test_failed_dump_restarts_scheduler(tmp_path):
    data, p, backend, _ = _run(tmp_path, dump_ok=False)
    assert data["result"] == pipeline.NOT_DONE
    assert backend.head() == OLD_B
    assert ("recreate", "scheduler_v2") in p.calls
    assert ("drop_next", "frontend") in p.calls
    assert list(tmp_path.glob("pre-update-*.dump")) == []


def test_modified_files_block_the_update(tmp_path):
    backend = FakeGit(OLD_B, NEW_B, dirty=["docker-compose.yml"])
    data, p, _, _ = _run(tmp_path, backend=backend)
    assert data["result"] == pipeline.NOT_DONE
    assert "docker-compose.yml" in data["message"]
    assert p.calls == []


def test_running_data_update_blocks_the_update(tmp_path):
    data, p, _, _ = _run(tmp_path, data_running=True)
    assert data["result"] == pipeline.NOT_DONE
    assert "hostlib" in data["message"]


def test_frontend_only_skips_the_backup(tmp_path):
    backend = FakeGit(OLD_B, OLD_B)
    data, p, _, frontend = _run(tmp_path, backend=backend, target={"backend": OLD_B, "frontend": NEW_F})
    assert data["result"] == pipeline.UPDATED
    assert _states(data)["backup"] == "skipped"
    assert not any(c[0] in ("dump", "stop") for c in p.calls)
    assert ("recreate", "fastapi_v2") not in p.calls


def test_new_version_down_is_rolled_back_by_downgrade(tmp_path):
    data, p, backend, frontend = _run(tmp_path, new_ok=False)
    assert data["result"] == pipeline.ROLLED_BACK
    assert backend.head() == OLD_B and frontend.head() == OLD_F
    assert ("downgrade", "r1") in p.calls
    assert ("reset", OLD_B) in backend.calls
    assert backend.calls.index(("merge", NEW_B)) < backend.calls.index(("reset", OLD_B))
    assert ("restore_image", "frontend") in p.calls
    assert not any(c[0] == "restore" for c in p.calls)
    assert p.revs == {"r1"}
    # The failed version's images are cleaned up after the rollback.
    assert p.calls[-1] == ("prune",)


def test_failed_downgrade_falls_back_to_the_dump(tmp_path):
    data, p, backend, _ = _run(tmp_path, new_ok=False, downgrade_ok=False)
    assert data["result"] == pipeline.ROLLED_BACK
    assert "відновленням бази" in data["message"]
    assert any(c[0] == "restore" for c in p.calls)
    assert backend.head() == OLD_B


def test_schema_untouched_needs_no_database_rollback(tmp_path):
    data, p, _, _ = _run(tmp_path, new_ok=False, migrate=False)
    assert data["result"] == pipeline.ROLLED_BACK
    assert not any(c[0] in ("downgrade", "restore") for c in p.calls)


def test_wrong_revision_after_start_is_rolled_back(tmp_path):
    backend = FakeGit(OLD_B, NEW_B, heads_by_sha={OLD_B: {"r1"}, NEW_B: {"r2"}})
    data, p, _, _ = _run(tmp_path, backend=backend, migrate=False, target={"backend": NEW_B})
    # Started and answered, but never migrated to r2.
    assert data["result"] == pipeline.ROLLED_BACK
    assert "r2" in data["message"]


def test_old_version_down_too_is_a_failure(tmp_path):
    class NeverUp(FakePlatform):
        def restore(self, path):
            self._call("restore", path.name)

    backend = FakeGit(OLD_B, NEW_B, heads_by_sha={OLD_B: {"r1"}, NEW_B: {"r2"}})
    platform = NeverUp(tmp_path, backend, new_ok=False, old_ok=False)
    job = Job.create(tmp_path, "admin", {"backend": NEW_B})
    pipeline.Update(job, {"backend": backend, "frontend": FakeGit(OLD_F, OLD_F)}, platform, SETTINGS).run()
    assert job.data["result"] == pipeline.FAILED
    assert "pre-update-" in job.data["message"]
    assert _states(job.data)["rollback"] == "failed"


@pytest.mark.parametrize("source, expected", [
    ('revision = "x1"\ndown_revision = None\n', ("x1", set())),
    ("revision: str = 'x2'\ndown_revision: Union[str, None] = 'x1'\n", ("x2", {"x1"})),
    ('revision = "m"\ndown_revision = ("a", "b")\n', ("m", {"a", "b"})),
    ("print('no migration here')\n", None),
])
def test_parse_revision(source, expected):
    from updater.migrations import parse_revision
    assert parse_revision(source) == expected


def test_heads_of_a_merged_history():
    from updater.migrations import heads
    files = [
        'revision = "a"\ndown_revision = None\n',
        'revision = "b"\ndown_revision = "a"\n',
        'revision = "c"\ndown_revision = "a"\n',
        'revision = "d"\ndown_revision = ("b", "c")\n',
    ]
    assert heads(files) == {"d"}
    assert heads(files[:3]) == {"b", "c"}
