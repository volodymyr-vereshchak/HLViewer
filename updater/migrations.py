"""Alembic heads read straight from the migration files of a commit.

The updater has to know which revision a commit expects before that commit is
checked out — to tell the admin an update migrates the database, and to verify
afterwards that the database actually reached it. Running `alembic heads`
would need the target code installed; the files are enough.
"""
import re

_REVISION = re.compile(r"^revision\s*(?::[^=]*)?=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN_REVISION = re.compile(r"^down_revision\s*(?::[^=]*)?=\s*(.+)$", re.MULTILINE)
_QUOTED = re.compile(r"['\"]([^'\"]+)['\"]")


def parse_revision(source: str) -> tuple[str, set[str]] | None:
    """(revision, down revisions) of one migration file, None if it is not one."""
    rev = _REVISION.search(source)
    if not rev:
        return None
    down = _DOWN_REVISION.search(source)
    parents = set(_QUOTED.findall(down.group(1))) if down else set()
    return rev.group(1), parents


def heads(sources: list[str]) -> set[str]:
    """Revisions nobody names as their parent — what `alembic heads` prints."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for source in sources:
        parsed = parse_revision(source)
        if parsed:
            revisions.add(parsed[0])
            parents |= parsed[1]
    return revisions - parents
