"""Whether an agent is the build this server expects.

The rule is equality, not "at least": an agent older than the server's build
lacks the fixes it was given, and one newer speaks of a server that has not
been updated yet. Both are the same mistake with the same remedy — put the
same build on both ends — and treating "newer" as acceptable is how a
workstation ends up the only place a change ever ran.

It is not a formality. The build served on 14.09 read a ВЕГА's display unit and
stored the archive's own number under it: 6.15 кгс/см² filed as 6.15 МПа, a
tenfold error that reads as an ordinary pressure and was found only because
somebody compared two screens. The fix went out an hour later, the workstation
kept running the old .exe, and the next poll wrote the same wrong column again.
A poll by a build we know to be wrong is worth less than no poll at all: a gap
is visible, and a plausible number is not.

The expectation is the build the server hands out — `AGENT_DIST_DIR` — so
there is nothing to configure and nothing to keep in step. A server with no
build expects nothing and blocks nobody: that is a server nobody has published
an agent from, not a fleet that has gone stale.
"""
from __future__ import annotations

from typing import Optional

from backend.services.agent_installer import find_installer
from backend.settings import backend_settings


def expected() -> Optional[str]:
    """The version this server hands out, or None if it hands out nothing."""
    found = find_installer(backend_settings["AGENT_DIST_DIR"])
    return found.version if found else None


def matches(agent_version: Optional[str], expected_version: Optional[str]) -> bool:
    """Is this agent the build to poll with.

    Unknown on either side is not a mismatch. An agent that has never reported
    a version has never been heard from at all — it is refused for being
    silent, which is a different message — and a server with no build has no
    opinion to enforce.
    """
    if expected_version is None or agent_version is None:
        return True
    return agent_version.strip() == expected_version.strip()


def refusal(agent_version: Optional[str], expected_version: Optional[str]) -> str:
    """What to tell whoever is looking, in one line."""
    return (
        f"Агент версії {agent_version or '—'}, а сервер роздає "
        f"{expected_version or '—'}. Опитування зупинено, щоб версія, яка "
        f"вже виправлена, не переписала архів своїми помилками. "
        f"Завантажте збірку з «Опитування → Агенти» й перезапустіть агента."
    )
