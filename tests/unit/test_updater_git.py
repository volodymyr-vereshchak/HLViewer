"""The updater's git side, on real repositories in a temp directory.

The fake git of the pipeline tests decides nothing; this is where the
commands themselves are checked — what a fetch brings, what counts as a local
edit, what the migration heads of a commit are.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from updater import pipeline
from updater.ops import Git, Shell, strip_credentials

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

MIGRATION = 'revision = "{rev}"\ndown_revision = {down}\n'


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repos(tmp_path):
    """An upstream with one commit and a server clone of it."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "master")
    _commit(upstream, {
        "README.md": "v1\n",
        "requirements.txt": "fastapi\n",
        "backend/db/alembic/versions/r1.py": MIGRATION.format(rev="r1", down="None"),
    }, "first")
    server = tmp_path / "server"
    _git(tmp_path, "clone", "-q", str(upstream), str(server))
    return upstream, server


def _config(upstream: Path) -> dict:
    return {"repos": {"backend": {"url": str(upstream), "branch": "master"}}, "token": None}


def _check(server: Path, upstream: Path) -> dict:
    return pipeline.check({"backend": Git(Shell(), server)}, _config(upstream))


def test_nothing_upstream_is_nothing_ahead(repos):
    upstream, server = repos
    result = _check(server, upstream)
    entry = result["repos"]["backend"]
    assert entry["ahead"] == 0 and entry["error"] is None
    assert result["problems"] == []


def test_new_commits_and_what_they_change(repos):
    upstream, server = repos
    _commit(upstream, {"README.md": "v2\n"}, "docs")
    new = _commit(upstream, {
        "requirements.txt": "fastapi\nhttpx\n",
        "backend/db/alembic/versions/r2.py": MIGRATION.format(rev="r2", down='"r1"'),
    }, "add r2")
    entry = _check(server, upstream)["repos"]["backend"]
    assert entry["ahead"] == 2
    assert [c["subject"] for c in entry["commits"]] == ["add r2", "docs"]
    assert entry["remote"]["sha"] == new
    assert entry["migrations"] is True
    assert entry["image_rebuild"] is True
    assert entry["updater_changed"] is False

    git = Git(Shell(), server)
    assert git.migration_heads(new) == {"r2"}
    git.merge_ff(new)
    assert git.head() == new
    assert (server / "backend/db/alembic/versions/r2.py").exists()


def test_local_edits_are_reported(repos):
    upstream, server = repos
    _commit(upstream, {"README.md": "v2\n"}, "docs")
    (server / "requirements.txt").write_text("edited on the server\n")
    result = _check(server, upstream)
    assert result["repos"]["backend"]["dirty"] == ["requirements.txt"]
    assert any("змінено файли" in p for p in result["problems"])


def test_untracked_file_in_the_way_is_reported(repos):
    upstream, server = repos
    _commit(upstream, {"new.txt": "from upstream\n"}, "add new.txt")
    (server / "new.txt").write_text("made by hand\n")
    result = _check(server, upstream)
    assert any("new.txt" in p for p in result["problems"])


def test_local_commit_means_diverged(repos):
    upstream, server = repos
    _commit(upstream, {"README.md": "v2\n"}, "upstream")
    _commit(server, {"local.txt": "x\n"}, "local")
    result = _check(server, upstream)
    assert result["repos"]["backend"].get("diverged") is True
    assert any("власні коміти" in p for p in result["problems"])


def test_unreachable_upstream_is_an_error_not_a_crash(repos, tmp_path):
    _, server = repos
    result = _check(server, tmp_path / "missing")
    assert result["repos"]["backend"]["error"]
    assert result["problems"]


def test_export_is_the_commit_not_the_working_tree(repos, tmp_path):
    upstream, server = repos
    git = Git(Shell(), server)
    head = git.head()
    (server / "README.md").write_text("dirty\n")
    dest = tmp_path / "export"
    dest.mkdir()
    git.export(head, dest)
    assert (dest / "README.md").read_text() == "v1\n"


def test_reset_returns_to_the_old_commit(repos):
    upstream, server = repos
    git = Git(Shell(), server)
    old = git.head()
    new = _commit(upstream, {"README.md": "v2\n"}, "docs")
    git.fetch(str(upstream), "master", None)
    git.merge_ff(new)
    git.reset_hard(old)
    assert git.head() == old
    assert (server / "README.md").read_text() == "v1\n"


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/o/r.git", "https://github.com/o/r.git"),
    ("https://user:tok@github.com/o/r.git", "https://github.com/o/r.git"),
    ("https://tok@git.local:3000/o/r", "https://git.local:3000/o/r"),
])
def test_strip_credentials(url, expected):
    assert strip_credentials(url) == expected
