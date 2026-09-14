"""The agent build the server hands out.

The poll agent runs on an operator's workstation, and getting it there used to
mean a folder on a share and a phone call. A download beside the key it needs
is one screen: issue the key, take the .exe, run it.

Nothing is built here. The file is a build artifact — produced by
`hl_poller/build_exe.py` and copied into `backend/data/agent`. So the only
question this module answers is "what is there right now", and it answers
"nothing" without complaint: a server that has never had one is a normal
server, not a broken one.

It is a **zip**, and that is not about size: the .exe inside is already
compressed and the archive saves a tenth of a megabyte. It is about the two
journeys the file makes. To an offline server it travels inside a git bundle,
which is how it gets there at all — a build kept out of git never arrives. And
from the server it travels to an operator through a browser, where a bare .exe
is what proxies and mail filters strike out. Older servers still hold a plain
.exe, so both are recognised.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

#: hlv-poller-0.1.0.zip — the version is in the name because that is the only
#: place a machine holding an old copy can be asked about it.
NAME_RE = re.compile(r"^hlv-poller-(?P<version>[0-9][0-9A-Za-z.\-_]*)\.(?:zip|exe)$")


@dataclass(frozen=True)
class Installer:
    path: Path
    version: str
    size: int
    built_at: datetime

    @property
    def filename(self) -> str:
        return self.path.name


def find_installer(directory: str | Path) -> Optional[Installer]:
    """The newest build in that folder, or None if there is none.

    Newest by file time rather than by version string: a build copied there by
    hand is the one somebody meant to publish, and comparing "0.10" against
    "0.9" correctly is a problem nobody needs to have here.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return None

    best: Optional[Installer] = None
    for path in folder.iterdir():
        match = NAME_RE.match(path.name)
        if match is None or not path.is_file():
            continue
        stat = path.stat()
        found = Installer(
            path=path,
            version=match.group("version"),
            size=stat.st_size,
            built_at=datetime.fromtimestamp(stat.st_mtime),
        )
        if best is None or found.built_at > best.built_at:
            best = found
    return best
